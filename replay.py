#!/usr/bin/env python3
"""
MYTHOS — Replay / Backtest Harness (the keystone).

Re-runs a RECORDED session through the REAL decision engine (same OIEngine,
FlowStack, VolEngine, SignalEngine, PaperTrader classes the live system uses —
nothing is re-implemented, so there is zero strategy drift), measuring the
trades and expectancy that policy would have produced on that exact tape.

Because it drives the real classes, you can change a config value and replay
the SAME recorded day to see the true effect — ending the "tune from memory of
the last bad session" curve-fit cycle the audits flagged.

    python replay.py                       list recorded days
    python replay.py 2026-06-16            replay that day, print expectancy
    python replay.py 2026-06-16 --sim      replay from the sim DB
    python replay.py 2026-06-16 --set BREAKEVEN_GUARD_PEAK=8   A/B vs baseline
    python replay.py 2026-06-16 --set SL_POINTS=12 --set EVIDENCE_NEED=6

HONEST LIMITATIONS (stated, not hidden):
  * Frames are 1 Hz snapshots, not raw ticks — replay CVD/premium-velocity is
    coarser than live (one synthetic tick per frame). Directional behaviour is
    faithful; sub-second microstructure is not.
  * Replays a session as it was RECORDED — it cannot show how the market would
    have reacted differently to a different trade (no market-impact model).
    That's the universal limit of every backtest, not a MYTHOS defect.
"""
import sys
import time as _time
from datetime import datetime, timedelta, timezone

try:  # Windows consoles default to cp1252 — force UTF-8 (run7 lesson)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))

# ── virtual clock: every engine time-source follows the recorded frame ───────
_VCLOCK = [0.0]
_real_time, _real_mono = _time.time, _time.monotonic


def _patch_clock():
    _time.time = lambda: _VCLOCK[0]
    _time.monotonic = lambda: _VCLOCK[0]


def _restore_clock():
    _time.time, _time.monotonic = _real_time, _real_mono


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_VCLOCK[0], tz or IST)


def main():
    args = sys.argv[1:]
    sim = "--sim" in args
    dump = "--dump" in args            # per-trade rows + win-concentration check
    args = [a for a in args if a not in ("--sim", "--dump")]
    overrides = {}
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--set" and i + 1 < len(args):
            k, _, v = args[i + 1].partition("=")
            overrides[k.strip()] = v.strip()
            i += 2
        else:
            rest.append(args[i])
            i += 1
    day = rest[0] if rest else None

    from mythos import config
    if sim:
        config.DB_PATH = config.DB_PATH.replace("mythos.db", "mythos_sim.db")
        config.TRADES_JSON = config.TRADES_JSON.replace(
            "trades_today.json", "trades_today_sim.json")
    # never clobber the real trade file during replay
    config.TRADES_JSON = config.TRADES_JSON + ".replay"

    from mythos.store import Store
    store = Store(config.DB_PATH)
    days = store.frame_days()
    if not day:
        print("\n  Recorded days with replayable frames:")
        if not days:
            print("    (none yet — run a live or --sim session first; the "
                  "flight recorder writes frames automatically)")
        for d in days:
            n = len(store.load_frames(d))
            print(f"    {d}   ({n} frames)")
        print("\n  Usage: python replay.py <day> [--sim] [--set KEY=VAL ...]\n")
        store.stop()
        return 0

    frames = store.load_frames(day)
    store.stop()
    if not frames:
        print(f"  No frames recorded for {day}. Available: {days}")
        return 1

    print(f"\n  REPLAY {day} — {len(frames)} frames "
          f"({(frames[-1][0]-frames[0][0])/60:.0f} min of tape)"
          f"{'  [SIM DB]' if sim else ''}")

    # baseline run (current config)
    base = _run(frames, {})
    _print_stats("BASELINE (current config)", base)

    if overrides:
        var = _run(frames, overrides)
        _print_stats(f"VARIANT  ({', '.join(f'{k}={v}' for k,v in overrides.items())})", var)
        _print_delta(base, var)
        if dump:
            _print_dump("VARIANT", var)
    elif dump:
        _print_dump("BASELINE", base)
    return 0


def _coerce(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return {"true": True, "false": False}.get(v.lower(), v)


def _run(frames, overrides: dict) -> dict:
    """Drive the real engine through the recorded frames with virtual time."""
    # fresh import state per run so config overrides + engine state are clean
    from mythos import config
    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, _coerce(v))

    _patch_clock()
    import mythos.signals as signals
    import mythos.trader as trader_mod
    _orig_sig_dt, _orig_tr_dt = signals.datetime, trader_mod.datetime
    signals.datetime = _FakeDatetime
    trader_mod.datetime = _FakeDatetime

    try:
        from mythos.feed import PriceStore
        from mythos.oi_engine import OIEngine
        from mythos.flow import FlowStack
        from mythos.vol import VolEngine
        from mythos.heavyweights import HeavyweightBasket
        from mythos.signals import SignalEngine
        from mythos.trader import PaperTrader
        from mythos import greeks as gk
        import numpy as np

        prices = PriceStore()
        oi = OIEngine()
        flow = FlowStack()
        vol = VolEngine()
        basket = HeavyweightBasket()
        sig = SignalEngine(oi, flow, vol, basket, prices)
        trader = PaperTrader(prices, store=None)
        trader._save_today = lambda: None     # no disk writes in replay

        for ts, fr in frames:
            _VCLOCK[0] = ts
            _load_frame(prices, fr)
            spot, futp, atm, ce, pe = prices.freeze_core()
            if spot <= 0 or atm <= 0:
                continue
            # drain the single futures tick into the flow stack
            while prices.fut_ticks:
                pr, qty, bid, ask, foi = prices.fut_ticks.popleft()
                flow.vwap.update(pr, qty)
                flow.avwap.update(pr, qty)
                flow.swings.update(pr)
                flow.cvd.on_tick(pr, qty, bid, ask)
                if foi > 0:
                    flow.fut_oi.update(pr, foi)
                closed = flow.candles_1m.update(pr, qty)
                if closed:
                    flow.rsi.on_candle(closed)
                    flow.atr.on_candle(closed)
                    flow.supertrend.on_candle(closed)
                    flow.adx.on_candle(closed)
            # OI + vol engines
            strikes = prices.snapshot_strikes(atm_override=atm)
            for (k, right), d in strikes.items():
                if d["oi"] > 0:
                    oi.update_strike(k, right, d["oi"], ts)
                if d["vol"] > 0:
                    oi.update_volume_baseline(k, right, d["vol"])
            oi.note_spot(spot, ts)
            oi.recompute(atm, spot)
            T = gk.years_to_expiry(config.expiry_dt_ist(), _FakeDatetime.now(IST))
            vol.update_chain(spot, strikes, T, atm)
            vol.update_spot(spot)
            for sym, ltp in list(prices.hw_ltp.items()):
                basket.on_tick(sym, ltp)
            basket.recompute(spot)
            # gamma heat (drives exit gamma-ride)
            gamma_heat = 0.0
            iv = vol.chain_iv.get((atm, "call")) or vol.atm_iv
            if iv and spot > 0:
                g = gk.greeks(spot, np.array([atm]), T, np.array([iv]), "call")
                gamma_heat = float(np.nan_to_num(g["gamma"][0])) * config.STRIKE_STEP
            # decide + trade (the real engine)
            decision = sig.evaluate()
            if decision.allowed and not trader.open:
                t = trader.try_enter(decision, vol.expected_move, oi)
                if t:
                    sig.note_entry(t.direction)
            for t in trader.snapshot_open():
                _dyn_target(t, spot, T, vol, oi, gk)
            if trader.open:
                d = trader.open[0].direction
                conv = sig.position_conviction(d)
                slope = flow.cvd.slope(60)
                trend_ok = slope > 0 if d == "CE" else slope < 0
                gamma_ride = gamma_heat >= 0.18 and trend_ok
                # live context for the pending-fill knife guard (mirror app)
                try:
                    _pv = sig.prem.velocity(d)
                    _ld = getattr(sig.last, "direction", "")
                except Exception:
                    _pv, _ld = 0.0, ""
                closed_trades = trader.check_exits(
                    sig.live_score(d), trend_ok, gamma_ride, _pv, _ld)
                for ct in closed_trades:
                    if ct.pnl_pts < 0:
                        sig.note_stop(ct.direction)
                    sig.note_exit()

        return _stats(trader)
    finally:
        signals.datetime, trader_mod.datetime = _orig_sig_dt, _orig_tr_dt
        _restore_clock()
        for k, v in saved.items():
            setattr(config, k, v)


def _load_frame(prices, fr: dict):
    from mythos import config
    prices.spot = fr["spot"]
    prices.spot_ts = _VCLOCK[0]
    prices.futures = fr["fut"]
    prices.futures_oi = fr.get("fut_oi", 0.0)
    prices.fut_bqty = fr.get("fut_bq", 0.0)
    prices.fut_aqty = fr.get("fut_aq", 0.0)
    prices.vix = fr.get("vix", 0.0)
    # feed the recorded traded volume as the tick qty (older frames lack "fut_vol"
    # and fall back to 0.0 = the prior behaviour, so old tapes replay identically;
    # newer frames revive a real CVD so the flow/consensus signals are testable)
    prices.fut_ticks.append((fr["fut"], fr.get("fut_vol", 0.0), fr.get("fut_bq", 0.0),
                             fr.get("fut_aq", 0.0), fr.get("fut_oi", 0.0)))
    for key, vals in fr.get("opts", {}).items():
        strike = float(key[:-1])
        right = "call" if key[-1] == "c" else "put"
        ltp, oi_v, vol_v, bid, ask, bq, aq = vals
        prices.opt_ltp[(strike, right)] = ltp
        prices.opt_ts[(strike, right)] = _VCLOCK[0]
        prices.opt_oi[(strike, right)] = oi_v
        prices.opt_vol[(strike, right)] = vol_v
        prices.opt_bid[(strike, right)] = bid
        prices.opt_ask[(strike, right)] = ask
        prices.opt_bqty[(strike, right)] = bq
        prices.opt_aqty[(strike, right)] = aq
    chain = {}
    for key, v in fr.get("chain", {}).items():
        strike = float(key[:-1])
        right = "call" if key[-1] == "c" else "put"
        chain[(strike, right)] = v
    prices.chain_oi = chain
    for sym, ltp in fr.get("idx", {}).items():
        prices.idx_ltp[sym] = ltp
    for sym, prev in fr.get("idx_prev", {}).items():     # revive sister %-change (breadth)
        prices.idx_prev[sym] = prev
        prices.idx_ts[sym] = _VCLOCK[0]
    for sym, ltp in fr.get("hw", {}).items():
        prices.hw_ltp[sym] = ltp
        prices.hw_ts[sym] = _VCLOCK[0]
    # revive heavyweight order-flow (task #37 — previously DROPPED on replay, so a
    # flow-tilt lead was untestable). hwf triple = [bid_qty, ask_qty, staleness].
    for sym, triple in fr.get("hwf", {}).items():
        try:
            bq, aq, _age = triple
            prices.hw_bqty[sym] = bq
            prices.hw_aqty[sym] = aq
        except (ValueError, TypeError):
            continue
    # revive sister-index OI digest (task #37). fr.get(...,{}) => OLD tapes (no
    # sister OI) replay byte-identically — same idiom as fut_vol backward-compat.
    for sym, v in fr.get("idx_pcr", {}).items():
        prices.idx_pcr[sym] = v
        prices.idx_chain_ts[sym] = _VCLOCK[0]
    for sym, v in fr.get("idx_pw", {}).items():
        prices.idx_put_wall[sym] = v
    for sym, v in fr.get("idx_cw", {}).items():
        prices.idx_call_wall[sym] = v


def _dyn_target(t, spot, T, vol, oi, gk):
    from mythos import config
    try:
        if t.direction == "CE":
            z = oi.nearest_resistance(spot)
            wall = z.level if z else 0.0
            if wall <= spot:
                return
            if vol.expected_move > 0:
                wall = min(wall, spot + vol.expected_move)
        else:
            z = oi.nearest_support(spot)
            wall = z.level if z else 0.0
            if wall <= 0 or wall >= spot:
                return
            if vol.expected_move > 0:
                wall = max(wall, spot - vol.expected_move)
        iv = vol.chain_iv.get((t.strike, t.right)) or vol.atm_iv or 0.13
        proj = float(gk.bs_price(wall, t.strike, T, iv, t.right))
        new_tgt = max(t.entry_price + config.TARGET_POINTS, round(proj, 2))
        if new_tgt > t.target:
            t.target = new_tgt
    except Exception:
        pass


def _stats(trader) -> dict:
    from mythos import config
    cl = trader.closed
    n = len(cl)
    wins = [t for t in cl if t.pnl_pts >= 0]
    losses = [t for t in cl if t.pnl_pts < 0]
    pts = sum(t.pnl_pts for t in cl)
    cash = sum(t.pnl_cash for t in cl)
    aw = sum(t.pnl_cash for t in wins) / len(wins) if wins else 0.0
    al = sum(t.pnl_cash for t in losses) / len(losses) if losses else 0.0
    wr = len(wins) / n if n else 0.0
    gp = sum(t.pnl_cash for t in wins)
    gl = -sum(t.pnl_cash for t in losses)
    # equity curve / max DD
    e = config.STARTING_CAPITAL
    peak = e
    mdd = 0.0
    for t in cl:
        e += t.pnl_cash
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    detail = [{"id": t.id, "dir": t.direction, "strike": t.strike,
               "entry": t.entry_price, "lots": t.lots, "qty": t.qty,
               "peak": round(t.peak_price - t.entry_price, 1),
               "exit": t.exit_reason, "pnl_pts": t.pnl_pts,
               "dused": getattr(t, "strike_delta_used", 0.0),
               "dgot": getattr(t, "strike_delta_achieved", 0.0),
               "pnl_cash": t.pnl_cash} for t in cl]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": wr * 100, "pts": pts, "cash": cash,
        "avg_win": aw, "avg_loss": al,
        "expectancy": wr * aw + (1 - wr) * al,
        "profit_factor": (gp / gl) if gl > 0 else float("inf"),
        "max_dd": mdd,
        "exits": _exit_breakdown(cl),
        "detail": detail,
    }


def _exit_breakdown(cl) -> dict:
    out = {}
    for t in cl:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


def _print_dump(title: str, s: dict):
    """Per-trade rows (P&L-sorted) + the win-concentration check: how much of net
    do the top 1-2 wins carry? A robust edge spreads net across many trades; an
    outlier-carried one collapses when the top 2 are removed."""
    d = s["detail"]
    net = s["cash"]
    print(f"\n  ── PER-TRADE DUMP ({title}) — {len(d)} trades, sorted by ₹ ──")
    for r in sorted(d, key=lambda r: -r["pnl_cash"]):
        dgot = r.get("dgot", 0.0)
        moneytag = "OTM" if 0.0 < dgot < 0.50 else ("Δ%.2f" % dgot if dgot else "  · ")
        print(f"    #{r['id']:>3} {r['dir']} {r['strike']:.0f}  "
              f"entry {r['entry']:>6.1f}  lots {r['lots']:>3}  {moneytag:<6} "
              f"peak +{r['peak']:>4.0f}  {r['exit']:<11} "
              f"{r['pnl_pts']:+6.1f}pt  ₹{r['pnl_cash']:>+10,.0f}")
    wins = sorted([r for r in d if r["pnl_cash"] >= 0],
                  key=lambda r: -r["pnl_cash"])
    if wins and net > 0:
        top1 = wins[0]["pnl_cash"]
        top2 = sum(r["pnl_cash"] for r in wins[:2])
        net_ex2 = net - top2
        print(f"    → top-1 win ₹{top1:+,.0f} = {100*top1/net:.0f}% of net; "
              f"top-2 wins ₹{top2:+,.0f} = {100*top2/net:.0f}% of net; "
              f"net without top-2 = ₹{net_ex2:+,.0f}")
        print(f"    → VERDICT: {'OUTLIER-CARRIED (top-2 > 60% of net)' if top2 > 0.6*net else 'broad (top-2 ≤ 60% of net)'}")


def _print_stats(title: str, s: dict):
    print(f"\n  ── {title} ──")
    print(f"    trades {s['trades']}   win-rate {s['win_rate']:.1f}%  "
          f"({s['wins']}W / {s['losses']}L)")
    print(f"    net    {s['pts']:+.1f} pts   ₹{s['cash']:+,.0f}")
    print(f"    avg win ₹{s['avg_win']:+,.0f}   avg loss ₹{s['avg_loss']:+,.0f}"
          f"   expectancy ₹{s['expectancy']:+,.0f}/trade")
    pf = s["profit_factor"]
    print(f"    profit factor {pf:.2f}" if pf != float("inf") else
          "    profit factor ∞ (no losses)")
    print(f"    max drawdown ₹{s['max_dd']:,.0f}")
    print(f"    exits: {s['exits']}")


def _print_delta(base: dict, var: dict):
    dc = var["cash"] - base["cash"]
    dw = var["win_rate"] - base["win_rate"]
    print(f"\n  ── DELTA (variant − baseline) ──")
    print(f"    cash {dc:+,.0f}   win-rate {dw:+.1f}pp   "
          f"trades {var['trades']-base['trades']:+d}   "
          f"maxDD {var['max_dd']-base['max_dd']:+,.0f}")
    verdict = ("variant BETTER" if dc > 0 and var["max_dd"] <= base["max_dd"] * 1.1
               else "variant WORSE" if dc < 0
               else "mixed — judge by your risk preference")
    print(f"    verdict: {verdict}")
    print("    (one recorded session — confirm across several before adopting)\n")


if __name__ == "__main__":
    sys.exit(main())
