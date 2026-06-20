#!/usr/bin/env python3
"""
MYTHOS — ENTRY-EDGE FORENSIC (temporary diagnostic).

First-principles question (AgentInstructions): WHY do entries lose on the
now-realistic, non-tautological tape? Two hypotheses, one decisive test:

    H1 (management):  the FIRE signal predicts the favourable direction, but
                      the exit/stall logic gives the move back.
    H2 (entry):       the FIRE signal does NOT predict direction (no edge), or
                      predicts the WRONG direction (inverted / chasing tops).

The decisive measurement is the SIGNED FORWARD SPOT RETURN after each FIRE,
independent of the trader's one-position constraint and independent of premium:

    CE fire is "right" if spot rises afterwards; PE fire if spot falls.
        fwd(H) = (+1 if CE else -1) * (spot[t+H] - spot[t])

If mean fwd is reliably > 0 and beats a RANDOM-entry baseline, the entry has
directional edge -> fix management.  If fwd ~ 0, no edge.  If fwd < 0, the
engine is buying the wrong side (inversion / chasing).

Runs the REAL engine (OIEngine/FlowStack/VolEngine/SignalEngine) on the REAL
RealisticSimFeed, driven headless under a virtual clock (replay.py pattern) so
many synthetic days run in seconds instead of real-time.

    python forensic_entry.py [days] [minutes]
"""
import sys
import time as _time
import math
import random
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))

# ── virtual clock (same trick replay.py uses) ────────────────────────────────
_VCLOCK = [0.0]
_real_time, _real_mono = _time.time, _time.monotonic


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_VCLOCK[0], tz or IST)


def _patch_all():
    _time.time = lambda: _VCLOCK[0]
    _time.monotonic = lambda: _VCLOCK[0]
    import mythos.signals as s
    import mythos.trader as t
    import mythos.sim_feed as sf
    s.datetime = _FakeDatetime
    t.datetime = _FakeDatetime
    sf.datetime = _FakeDatetime
    return (s, t, sf)


def _restore(mods):
    _time.time, _time.monotonic = _real_time, _real_mono
    s, t, sf = mods
    s.datetime = datetime
    t.datetime = datetime
    sf.datetime = datetime


# H horizons in seconds for the forward-return measurement
HORIZONS = [30, 60, 120, 180, 300]


def _run_day(day_seed: int, minutes: int):
    """One independent synthetic session. Returns (timeline, fires)."""
    from mythos.feed import PriceStore
    from mythos.oi_engine import OIEngine
    from mythos.flow import FlowStack
    from mythos.vol import VolEngine
    from mythos.heavyweights import HeavyweightBasket
    from mythos.signals import SignalEngine
    from mythos.sim_feed import RealisticSimFeed
    from mythos import config, greeks as gk
    import numpy as np

    random.seed(day_seed)

    prices = PriceStore()
    oi = OIEngine()
    flow = FlowStack()
    vol = VolEngine()
    basket = HeavyweightBasket()
    sig = SignalEngine(oi, flow, vol, basket, prices)
    sig.bypass_time_gates = True

    feed = RealisticSimFeed(prices, basket)
    # seed without starting the real-time thread
    feed._seed_oi()
    feed._seed_basket()
    prices.vix = round(feed.vix, 2)
    flow.seed_candles(feed.warmup_candles(30))

    timeline = []          # (sec_index, spot)
    srseries = []          # (sec, spot, sup_dist, sup_str, res_dist, res_str)
    fires = []             # dict per FIRE edge
    prev = {"CE": "", "PE": ""}

    last_opt = last_hw = last_chain = -1e9
    for sec in range(minutes * 60):
        vt = _START + sec
        _VCLOCK[0] = vt

        feed._step_vix()
        feed._step_spot_and_futures(vt)
        if vt - last_opt >= 1.0:        # FAITHFUL 1s cadence — a sparser cadence
            feed._step_options()         # starves max-pain/OI and kills the sim's
            last_opt = vt                # S/R coupling (the validated courtroom)
        if vt - last_hw >= 1.0:
            feed._step_heavyweights()
            last_hw = vt
        if vt - last_chain >= 30.0:
            prices.chain_oi = dict(feed.oi)
            last_chain = vt

        spot, futp, atm, ce, pe = prices.freeze_core()
        if spot <= 0 or atm <= 0:
            timeline.append((sec, spot))
            continue

        # drain futures ticks -> flow (mirror app/_analytics_pass + replay)
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

        strikes = prices.snapshot_strikes(atm_override=atm)
        for (k, right), d in strikes.items():
            if d["oi"] > 0:
                oi.update_strike(k, right, d["oi"], vt)
            if d["vol"] > 0:
                oi.update_volume_baseline(k, right, d["vol"])
        oi.note_spot(spot, vt)
        oi.recompute(atm, spot)
        # NOTE: vol.update_chain (full IV-bisection over the chain) is SKIPPED —
        # SignalEngine only stores self.vol and never reads it (verified), so the
        # FIRE decisions this forensic measures are identical without it, and it
        # was ~all the runtime. The trader (which uses expected_move) is not run
        # here. _step_options still prices premiums faithfully every 1s.
        for sym, ltp in list(prices.hw_ltp.items()):
            basket.on_tick(sym, ltp)
        basket.recompute(spot)

        dec = sig.evaluate()
        timeline.append((sec, spot))

        # SIM S/R-COUPLING probe: does the sim price actually bounce off the OI
        # zones the engine trades? Record nearest strong support BELOW and
        # resistance ABOVE spot. If the sim respected S/R, forward return after
        # touching a strong support should be > 0 (a bounce) — if it's ~random,
        # the sim doesn't model the mechanism the engine bets on.
        sd = sr = 9e9
        ss = rs = 0.0
        for z in oi.support_zones:
            if z.level <= spot and (spot - z.level) < sd:
                sd, ss = spot - z.level, z.strength
        for z in oi.resistance_zones:
            if z.level >= spot and (z.level - spot) < sr:
                sr, rs = z.level - spot, z.strength
        srseries.append((sec, spot, sd, ss, sr, rs))

        for d in ("CE", "PE"):
            v = dec.ce if d == "CE" else dec.pe
            if v.state == "FIRE" and prev[d] != "FIRE":
                # momentum INTO the signal (last 60s) — chasing vs reversal
                prior = spot - timeline[max(0, sec - 60)][1]
                fires.append({
                    "sec": sec, "dir": d, "kind": v.kind,
                    "zone": v.zone_level, "spot": spot,
                    "dist": v.distance,          # spot - zone (signed)
                    "ok": v.ok_count, "need": v.needed,
                    "allowed": dec.allowed,
                    "prior60": prior,
                })
            prev[d] = v.state

    return timeline, fires, srseries


def _fwd_returns(timeline, fires):
    """Signed forward spot return for each fire at each horizon."""
    spot_at = {sec: sp for sec, sp in timeline}
    n = len(timeline)
    out = []
    for f in fires:
        sgn = 1.0 if f["dir"] == "CE" else -1.0
        row = dict(f)
        row["fwd"] = {}
        for H in HORIZONS:
            j = f["sec"] + H
            if j < n and spot_at.get(j) and spot_at.get(f["sec"]):
                row["fwd"][H] = sgn * (spot_at[j] - spot_at[f["sec"]])
        out.append(row)
    return out


def _random_baseline(timeline, k_per_day):
    """k random (sec, random-dir) entries -> signed forward returns."""
    spot_at = {sec: sp for sec, sp in timeline}
    n = len(timeline)
    rows = []
    hi = n - max(HORIZONS) - 1
    if hi <= 60:
        return rows
    for _ in range(k_per_day):
        sec = random.randint(60, hi)
        sgn = random.choice([1.0, -1.0])
        if not spot_at.get(sec):
            continue
        fwd = {}
        for H in HORIZONS:
            j = sec + H
            if spot_at.get(j):
                fwd[H] = sgn * (spot_at[j] - spot_at[sec])
        rows.append({"fwd": fwd})
    return rows


def _sim_coupling(srseries, band, min_str):
    """Does spot bounce at OI zones in the SIM? For seconds where spot sits
    within `band` of a STRONG support, forward (unsigned) return should be > 0
    if support causes bounces; mirror (negative) for resistance. Compared to
    the unconditional forward return = the sim's baseline drift."""
    spot_at = {sec: sp for sec, sp, *_ in srseries}
    n = len(srseries)
    near_sup = {H: [] for H in HORIZONS}
    near_res = {H: [] for H in HORIZONS}
    allmove = {H: [] for H in HORIZONS}
    for sec, spot, sd, ss, srd, rs in srseries:
        for H in HORIZONS:
            j = sec + H
            if not spot_at.get(j):
                continue
            mv = spot_at[j] - spot
            allmove[H].append(mv)
            if ss >= min_str and sd <= band:
                near_sup[H].append(mv)       # bounce => mv > 0
            if rs >= min_str and srd <= band:
                near_res[H].append(mv)       # reject => mv < 0
    def m(a):
        return sum(a) / len(a) if a else 0.0
    return {H: {"sup": m(near_sup[H]), "nsup": len(near_sup[H]),
                "res": m(near_res[H]), "nres": len(near_res[H]),
                "all": m(allmove[H])} for H in HORIZONS}


def _stat(rows, H):
    vals = [r["fwd"][H] for r in rows if H in r.get("fwd", {})]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mean = sum(vals) / n
    median = vals[n // 2]
    hit = sum(1 for v in vals if v > 0) / n * 100
    return {"n": n, "mean": mean, "median": median, "hit": hit}


# start the virtual session at a normal (non-expiry) weekday morning
_START = datetime(2026, 6, 16, 9, 25, 0, tzinfo=IST).timestamp()


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 150

    mods = _patch_all()
    try:
        from mythos import config
        all_fwd, all_rand, all_sr = [], [], []
        day_chars = []
        for d in range(days):
            timeline, fires, srseries = _run_day(1000 + d, minutes)
            fwd = _fwd_returns(timeline, fires)
            all_fwd.extend(fwd)
            all_rand.extend(_random_baseline(timeline, 40))
            all_sr.append(srseries)
            spots = [s for _, s in timeline if s > 0]
            rng = (max(spots) - min(spots)) if spots else 0.0
            ce = sum(1 for f in fires if f["dir"] == "CE")
            pe = len(fires) - ce
            day_chars.append((rng, len(fires), ce, pe))
            # per-day validity + edge peek so the run can be judged before it ends
            band = getattr(config, "ZONE_BAND", 20.0)
            mstr = getattr(config, "ZONE_MIN_STRENGTH", 0.5)
            nsup = sum(1 for _, sp, sd, ss, srd, rs in srseries
                       if ss >= mstr and sd <= band)
            day_fwd = _fwd_returns(timeline, fires)
            e60 = [r["fwd"][60] for r in day_fwd if 60 in r.get("fwd", {})]
            m60 = sum(e60) / len(e60) if e60 else 0.0
            print(f"  day {d+1}/{days}: range {rng:5.0f}pt  "
                  f"fires {len(fires):3d}  (CE {ce} / PE {pe})  "
                  f"nearSup_n {nsup:4d}  fwd60 {m60:+.2f}")
    finally:
        _restore(mods)

    print("\n" + "=" * 68)
    print(f"  ENTRY-EDGE FORENSIC — {days} days × {minutes} min")
    print("=" * 68)

    n = len(all_fwd)
    ce = [f for f in all_fwd if f["dir"] == "CE"]
    pe = [f for f in all_fwd if f["dir"] == "PE"]
    bounce = [f for f in all_fwd if f["kind"] == "BOUNCE"]
    brk = [f for f in all_fwd if f["kind"] == "BREAK"]
    print(f"\n  total FIRE events: {n}   CE {len(ce)} / PE {len(pe)}   "
          f"BOUNCE {len(bounce)} / BREAK {len(brk)}")

    print("\n  SIGNED FORWARD SPOT RETURN after each FIRE")
    print("  (positive = signal predicted the right direction)")
    print(f"  {'H(s)':>5} {'n':>5} {'mean':>8} {'median':>8} {'hit%':>7}   "
          f"| RANDOM {'mean':>8} {'hit%':>7}   | edge")
    for H in HORIZONS:
        s = _stat(all_fwd, H)
        r = _stat(all_rand, H)
        if not s:
            continue
        rmean = r["mean"] if r else 0.0
        rhit = r["hit"] if r else 50.0
        edge = s["mean"] - rmean
        print(f"  {H:>5} {s['n']:>5} {s['mean']:>+8.2f} {s['median']:>+8.2f} "
              f"{s['hit']:>6.1f}%   |        {rmean:>+8.2f} {rhit:>6.1f}%   "
              f"| {edge:>+6.2f}")

    print("\n  INVERSE SIGNAL (negate every fire's direction)")
    print("  (if inverting BEATS random, the signal carries WRONG-WAY info)")
    print(f"  {'H(s)':>5} {'inv mean':>9} {'inv hit%':>9} {'vs random':>10}")
    for H in HORIZONS:
        s = _stat(all_fwd, H)
        r = _stat(all_rand, H)
        if not s:
            continue
        inv_mean = -s["mean"]
        inv_hit = 100 - s["hit"]
        rmean = r["mean"] if r else 0.0
        print(f"  {H:>5} {inv_mean:>+9.2f} {inv_hit:>8.1f}% "
              f"{inv_mean - rmean:>+10.2f}")

    print("\n  BY DIRECTION (mean signed fwd return, pts)")
    print(f"  {'H(s)':>5} {'CE mean':>9} {'CE hit%':>8} {'PE mean':>9} {'PE hit%':>8}")
    for H in HORIZONS:
        sc, sp = _stat(ce, H), _stat(pe, H)
        if not sc and not sp:
            continue
        print(f"  {H:>5} {(sc['mean'] if sc else 0):>+9.2f} "
              f"{(sc['hit'] if sc else 0):>7.1f}% "
              f"{(sp['mean'] if sp else 0):>+9.2f} "
              f"{(sp['hit'] if sp else 0):>7.1f}%")

    print("\n  BY ARCHETYPE (mean signed fwd return, pts)")
    for label, grp in (("BOUNCE", bounce), ("BREAK", brk)):
        line = f"  {label:>7}: "
        for H in HORIZONS:
            s = _stat(grp, H)
            line += f"H{H}={s['mean']:+.2f}({s['hit']:.0f}%) " if s else ""
        print(line)

    # geometry / doctrine check: CE should fire at SUPPORT (dist>=0, zone below
    # spot); PE at RESISTANCE (dist<=0). dist = spot - zone.
    print("\n  GEOMETRY (doctrine: CE at support dist>0, PE at resistance dist<0)")
    for d, grp in (("CE", ce), ("PE", pe)):
        if not grp:
            continue
        correct = sum(1 for f in grp
                      if (f["dir"] == "CE" and f["dist"] >= 0)
                      or (f["dir"] == "PE" and f["dist"] <= 0))
        print(f"    {d}: {correct}/{len(grp)} geometrically correct "
              f"({correct/len(grp)*100:.0f}%)  "
              f"avg |dist| {sum(abs(f['dist']) for f in grp)/len(grp):.1f}pt")

    # chasing vs reversal: prior-60s move in the trade's favour?  For a reversal
    # entry the prior move should be AGAINST (we buy CE after a DOWN move that is
    # turning).  prior60 signed by direction: + means we entered WITH a move
    # already underway (chasing); - means against it (reversal).
    print("\n  ENTRY TIMING (prior-60s move signed by direction)")
    for d, grp in (("CE", ce), ("PE", pe)):
        if not grp:
            continue
        sgn = 1.0 if d == "CE" else -1.0
        chase = [sgn * f["prior60"] for f in grp]
        chasing = sum(1 for c in chase if c > 0)
        print(f"    {d}: prior60 signed avg {sum(chase)/len(chase):+.1f}pt   "
              f"chasing(with-move) {chasing}/{len(grp)} "
              f"({chasing/len(grp)*100:.0f}%)")

    # SIM S/R COUPLING — the decisive "is this a sim gap?" test
    print("\n  SIM S/R COUPLING — does spot bounce at OI zones in the SIM?")
    print("  (if 'near support' fwd ~ 'all', the sim ignores the engine's zones)")
    band = getattr(config, "ZONE_BAND", 12.0)
    min_str = getattr(config, "ZONE_MIN_STRENGTH", 0.0)
    cup = []
    for srseries in all_sr:
        cup.append(_sim_coupling(srseries, band, min_str))
    print(f"  {'H(s)':>5} {'nearSup mv':>11} {'nearRes mv':>11} {'all mv':>9}"
          f"   (support should be >0, resistance <0)")
    for H in HORIZONS:
        nsup = sum(c[H]["nsup"] for c in cup)
        nres = sum(c[H]["nres"] for c in cup)
        sup = (sum(c[H]["sup"] * c[H]["nsup"] for c in cup) / nsup) if nsup else 0.0
        res = (sum(c[H]["res"] * c[H]["nres"] for c in cup) / nres) if nres else 0.0
        al = sum(c[H]["all"] for c in cup) / len(cup)
        print(f"  {H:>5} {sup:>+8.2f}(n={nsup:<5}) {res:>+8.2f}(n={nres:<5}) "
              f"{al:>+9.2f}")

    print()
    # verdict
    s60 = _stat(all_fwd, 60)
    r60 = _stat(all_rand, 60)
    if s60 and r60:
        edge = s60["mean"] - r60["mean"]
        print("  VERDICT (60s horizon):")
        if edge > 1.0 and s60["hit"] > 55:
            print(f"    Entry HAS directional edge (+{edge:.2f}pt vs random, "
                  f"{s60['hit']:.0f}% hit) -> losses are a MANAGEMENT problem.")
        elif abs(edge) <= 1.0:
            print(f"    Entry has ~NO edge ({edge:+.2f}pt vs random) -> the "
                  f"signal does not predict direction. Entry logic is the problem.")
        else:
            print(f"    Entry is INVERTED/chasing ({edge:+.2f}pt vs random, "
                  f"{s60['hit']:.0f}% hit) -> firing on the wrong side.")
    print()


if __name__ == "__main__":
    main()
