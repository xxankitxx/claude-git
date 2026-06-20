#!/usr/bin/env python3
"""
MYTHOS — SIM STYLISED-FACTS measurement (calibration tool).

Measures the PRICE PROCESS ONLY — never the engine. This is the courtroom for
the sim-realism upgrade: the new regime/S-R parameters are tuned so these
numbers match real intraday Nifty, NOT so the engine wins. (Anti-circularity
guardrail — see memory 'sim-cannot-judge-entry-edge'.)

Reports, over many headless synthetic days:
  * daily high-low range distribution (median / p25 / p75 / p90 / max)
  * |close-open| directional move, and TREND-day fraction (one-way days)
  * time spent in each regime
  * 1-step return autocorrelation, split RANGE vs TREND (trend should be +,
    range should be ~0/negative)
  * S/R hold-vs-break rate at strike walls (real Nifty: strong levels hold
    ~60-70% of tests)

    python sim_stats.py [days] [minutes]
"""
import sys
import time as _time
import math
import random
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))
_VCLOCK = [0.0]
_real_time, _real_mono = _time.time, _time.monotonic


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_VCLOCK[0], tz or IST)


def _patch():
    _time.time = lambda: _VCLOCK[0]
    _time.monotonic = lambda: _VCLOCK[0]
    import mythos.sim_feed as sf
    sf.datetime = _FakeDatetime
    return sf


def _restore(sf):
    _time.time, _time.monotonic = _real_time, _real_mono
    sf.datetime = datetime


_START = datetime(2026, 6, 16, 9, 20, 0, tzinfo=IST).timestamp()


def _run_day(seed, minutes):
    from mythos.feed import PriceStore
    from mythos.heavyweights import HeavyweightBasket
    from mythos.sim_feed import RealisticSimFeed
    from mythos import config
    random.seed(seed)
    prices = PriceStore()
    basket = HeavyweightBasket()
    feed = RealisticSimFeed(prices, basket)
    feed._seed_oi()
    feed._seed_basket()

    path = []          # spot each second
    regimes = []       # regime each second
    step = config.STRIKE_STEP
    for s in range(minutes * 60):
        vt = _START + s
        _VCLOCK[0] = vt
        feed._step_spot_and_futures(vt)
        if s % 10 == 0:
            feed._step_options()        # keep max-pain / OI evolving (faithful)
        path.append(feed.spot)
        regimes.append(feed.regime)
    return path, regimes, feed.session_open, step, feed.oi, feed.day_bias


def _autocorr1(rets):
    n = len(rets)
    if n < 3:
        return 0.0
    m = sum(rets) / n
    num = sum((rets[i] - m) * (rets[i - 1] - m) for i in range(1, n))
    den = sum((r - m) ** 2 for r in rets)
    return num / den if den else 0.0


def _sr_hold_break(path, step, oi):
    """Count tests of a strike wall: spot approaches within SR_RANGE, then does
    it BREAK (cross by SR_BREAK_PTS) or HOLD (retreat SR_RANGE away first)?"""
    from mythos.sim_feed import P
    holds = breaks = 0
    state = None        # (strike, side, ) currently being tested
    for sp in path:
        k_sup = math.floor(sp / step) * step
        k_res = k_sup + step
        # pick the nearer bracketing wall as the active test
        for k, side, oi_side in ((k_sup, "sup", "put"), (k_res, "res", "call")):
            d = abs(sp - k)
            strength = min(1.0, oi.get((k, oi_side), 0.0) / P.SR_WALL_REF)
            if d <= P.SR_RANGE_PTS and strength >= P.SR_MIN_STRENGTH:
                if state is None:
                    state = [k, side]
        if state is not None:
            k, side = state
            if side == "res":
                if sp >= k + P.SR_BREAK_PTS:
                    breaks += 1; state = None
                elif sp <= k - P.SR_RANGE_PTS:
                    holds += 1; state = None
            else:
                if sp <= k - P.SR_BREAK_PTS:
                    breaks += 1; state = None
                elif sp >= k + P.SR_RANGE_PTS:
                    holds += 1; state = None
    return holds, breaks


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 90

    sf = _patch()
    try:
        ranges, moves, trend_days = [], [], 0
        biased_moves, neutral_moves, biased_days = [], [], 0
        regime_secs = {}
        ac_trend, ac_range = [], []
        tot_hold = tot_break = 0
        for d in range(days):
            path, regimes, open_px, step, oi, day_bias = _run_day(2000 + d, minutes)
            hi, lo = max(path), min(path)
            rng = hi - lo
            move = abs(path[-1] - path[0])
            ranges.append(rng)
            moves.append(move)
            if day_bias:
                biased_days += 1
                biased_moves.append(move)
            else:
                neutral_moves.append(move)
            if rng > 0 and move / rng >= 0.55:
                trend_days += 1
            for r in regimes:
                regime_secs[r] = regime_secs.get(r, 0) + 1
            # autocorr split by regime
            rets_trend, rets_range = [], []
            for i in range(1, len(path)):
                ret = (path[i] - path[i - 1])
                if regimes[i] in ("TREND_UP", "TREND_DOWN"):
                    rets_trend.append(ret)
                elif regimes[i] == "RANGE":
                    rets_range.append(ret)
            if len(rets_trend) > 50:
                ac_trend.append(_autocorr1(rets_trend))
            if len(rets_range) > 50:
                ac_range.append(_autocorr1(rets_range))
            h, b = _sr_hold_break(path, step, oi)
            tot_hold += h; tot_break += b
    finally:
        _restore(sf)

    def pct(vals, p):
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(p / 100 * len(vals)))]

    print("\n" + "=" * 64)
    print(f"  SIM STYLISED FACTS — {days} days × {minutes} min")
    print("  (calibration target: REAL Nifty — NOT the engine)")
    print("=" * 64)
    print(f"\n  DAILY RANGE (high-low), pts")
    print(f"    median {pct(ranges,50):.0f}   p25 {pct(ranges,25):.0f}   "
          f"p75 {pct(ranges,75):.0f}   p90 {pct(ranges,90):.0f}   "
          f"max {max(ranges):.0f}")
    print(f"    target: median ~150-200, p90 ~280-330, rare >400")
    print(f"\n  DIRECTIONAL MOVE |close-open|, pts")
    print(f"    median {pct(moves,50):.0f}   p90 {pct(moves,90):.0f}")
    print(f"  TREND-day fraction (|move|/range ≥ 0.55): "
          f"{trend_days}/{days} = {trend_days/days*100:.0f}%   "
          f"(target ~25-35%)")
    bm = sum(biased_moves) / len(biased_moves) if biased_moves else 0.0
    nm = sum(neutral_moves) / len(neutral_moves) if neutral_moves else 0.0
    print(f"    mean |move|: directional days {bm:.0f}pt ({biased_days}d)  "
          f"vs range days {nm:.0f}pt ({days-biased_days}d)")
    tot = sum(regime_secs.values()) or 1
    print(f"\n  REGIME TIME SHARE")
    for r in ("RANGE", "TREND_UP", "TREND_DOWN", "VOLATILE"):
        print(f"    {r:11} {regime_secs.get(r,0)/tot*100:5.1f}%")
    act = sum(ac_trend) / len(ac_trend) if ac_trend else 0.0
    acr = sum(ac_range) / len(ac_range) if ac_range else 0.0
    print(f"\n  1-STEP RETURN AUTOCORRELATION")
    print(f"    TREND {act:+.3f}  (want > 0 — momentum)")
    print(f"    RANGE {acr:+.3f}  (want ≤ 0 — mean-revert)")
    tot_t = tot_hold + tot_break
    print(f"\n  S/R WALL TESTS: {tot_t}   hold {tot_hold} "
          f"({tot_hold/tot_t*100:.0f}%)  break {tot_break} "
          f"({tot_break/tot_t*100:.0f}%)" if tot_t else
          "\n  S/R WALL TESTS: none")
    print(f"    target: hold ~60-70% (strong levels usually hold, sometimes break)")
    print()


if __name__ == "__main__":
    main()
