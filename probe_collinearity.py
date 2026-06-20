#!/usr/bin/env python3
"""Adversarial probe: drive the real engine through a recorded tape and sample
all four panel votes every N frames, then compute pairwise correlation.
Tests whether the panels are independent or collinear (all = 'price went up')."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run(day, sim=False, step=120):
    from mythos import config
    if sim:
        config.DB_PATH = config.DB_PATH.replace("mythos.db", "mythos_sim.db")
    import consensus_core as cc
    import replay
    from mythos.store import Store
    from mythos import clk

    store = Store(config.DB_PATH)
    frames = store.load_frames(day)
    store.stop()
    if not frames:
        print("no frames", day); return

    # mimic replay._run setup but sample panels
    replay._patch_clock()
    import mythos.signals as signals, mythos.trader as trader_mod
    osig, otr = signals.datetime, trader_mod.datetime
    signals.datetime = replay._FakeDatetime
    trader_mod.datetime = replay._FakeDatetime
    from mythos.feed import PriceStore
    from mythos.oi_engine import OIEngine
    from mythos.flow import FlowStack
    from mythos.vol import VolEngine
    from mythos.heavyweights import HeavyweightBasket
    from mythos.signals import SignalEngine
    import mythos.greeks as gk
    import numpy as np

    prices, oi, flow, vol, basket = (PriceStore(), OIEngine(), FlowStack(),
                                     VolEngine(), HeavyweightBasket())
    sig = SignalEngine(oi, flow, vol, basket, prices)
    idx_hist = {}
    samples = {"FLOW": [], "STRUCTURE": [], "BREADTH": [], "TREND": [], "C": [],
               "spot": []}
    i = 0
    for ts, fr in frames:
        replay._VCLOCK[0] = ts
        replay._load_frame(prices, fr)
        spot, futp, atm, ce, pe = prices.freeze_core()
        if spot <= 0 or atm <= 0:
            continue
        while prices.fut_ticks:
            pr, qty, bid, ask, foi = prices.fut_ticks.popleft()
            flow.vwap.update(pr, qty); flow.avwap.update(pr, qty)
            flow.swings.update(pr); flow.cvd.on_tick(pr, qty, bid, ask)
            if foi > 0:
                flow.fut_oi.update(pr, foi)
            closed = flow.candles_1m.update(pr, qty)
            if closed:
                flow.rsi.on_candle(closed); flow.atr.on_candle(closed)
                flow.supertrend.on_candle(closed); flow.adx.on_candle(closed)
        strikes = prices.snapshot_strikes(atm_override=atm)
        for (k, right), d in strikes.items():
            if d["oi"] > 0:
                oi.update_strike(k, right, d["oi"], ts)
        oi.note_spot(spot, ts); oi.recompute(atm, spot)
        for sym, ltp in list(prices.hw_ltp.items()):
            basket.on_tick(sym, ltp)
        basket.recompute(spot)
        i += 1
        if i % step != 0:
            continue
        votes = cc.panel_votes(spot, atm, oi, flow, basket, prices, sig,
                               idx_hist, clk.now())
        cons = cc.fuse(votes)
        for k in ("FLOW", "STRUCTURE", "BREADTH", "TREND"):
            samples[k].append(votes[k]["vote"])
        samples["C"].append(cons["C"])
        samples["spot"].append(spot)

    signals.datetime, trader_mod.datetime = osig, otr
    replay._restore_clock()

    n = len(samples["spot"])
    print(f"\n=== {day}{' [SIM]' if sim else ''} — {n} samples (every {step} frames) ===")
    import statistics as st

    def corr(a, b):
        if len(a) < 3:
            return float("nan")
        ma, mb = st.fmean(a), st.fmean(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
        if va <= 1e-12 or vb <= 1e-12:
            return float("nan")
        return cov / (va * vb) ** 0.5

    # per-panel activity
    print("  panel       mean    stdev   |nonzero%|")
    for k in ("FLOW", "STRUCTURE", "BREADTH", "TREND", "C"):
        v = samples[k]
        nz = 100.0 * sum(1 for x in v if abs(x) > 0.05) / max(1, len(v))
        sd = st.pstdev(v) if len(v) > 1 else 0.0
        print(f"  {k:<10} {st.fmean(v):+6.3f}  {sd:6.3f}   {nz:5.1f}%")
    print("\n  pairwise vote correlation (independence test):")
    panels = ["FLOW", "STRUCTURE", "BREADTH", "TREND"]
    for a in range(len(panels)):
        for b in range(a + 1, len(panels)):
            print(f"    {panels[a]:<10} vs {panels[b]:<10} "
                  f"r = {corr(samples[panels[a]], samples[panels[b]]):+.3f}")
    # spot-direction collinearity: do panels just track price slope?
    spot = samples["spot"]
    dspot = [spot[j] - spot[max(0, j - 5)] for j in range(len(spot))]
    print("\n  correlation of each panel vote with recent spot change (Δspot/5-samp):")
    for k in panels + ["C"]:
        print(f"    {k:<10} r(vote, Δspot) = {corr(samples[k], dspot):+.3f}")


if __name__ == "__main__":
    a = sys.argv[1:]
    sim = "--sim" in a
    a = [x for x in a if x != "--sim"]
    run(a[0], sim)
