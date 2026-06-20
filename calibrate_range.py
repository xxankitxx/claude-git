"""Empirically calibrate MEANREV_RATE so a NORMAL Nifty day prints ~150-220
pt high-low (user's standard: 300 = a big/miraculous day)."""
import sys, time as _t, statistics
sys.path.insert(0, "D:/ClaudeCode")
import mythos.sim_feed as sfmod
from mythos.sim_feed import SimFeed, P
from mythos.feed import PriceStore
from mythos.heavyweights import HeavyweightBasket

real_time = _t.time
vt = [real_time()]
sfmod.time.time = lambda: vt[0]


def session_range(force_normal=True):
    sf = SimFeed(PriceStore(), HeavyweightBasket())
    if force_normal:
        sf.big_day = False
        sf.event_day = False
        sf.soft_cap = P.RANGE_CAP_NORMAL
        sf.level_pull = P.LEVEL_PULL_NORMAL
    sf.spot = sf.session_open
    lo = hi = sf.spot
    for _ in range(375):
        vt[0] += 60
        for _ in range(60):
            sf._step_spot_and_futures(vt[0])
        lo = min(lo, sf.spot)
        hi = max(hi, sf.spot)
    return hi - lo


print("Calibrating LEVEL_PULL (normal-day high-low target 150-220)...\n")
print(f"  {'LEVEL_PULL':>14} {'median':>8} {'p25':>6} {'p75':>6} {'max':>6}")
best = None
for mr in (0.020, 0.040, 0.070, 0.110, 0.170):
    P.LEVEL_PULL_NORMAL = mr
    vt[0] = real_time()
    rs = []
    for s in range(14):
        vt[0] += 1e6
        rs.append(session_range())
    rs.sort()
    med = statistics.median(rs)
    p25 = rs[len(rs) // 4]
    p75 = rs[3 * len(rs) // 4]
    flag = "  <- in target" if 150 <= med <= 220 else ""
    print(f"  {mr:>14.4f} {med:>8.0f} {p25:>6.0f} {p75:>6.0f} {max(rs):>6.0f}{flag}")
    if 150 <= med <= 220 and (best is None or abs(med - 185) < best[1]):
        best = (mr, abs(med - 185))

sfmod.time.time = real_time
if best:
    print(f"\n  RECOMMENDED MEANREV_RATE = {best[0]:.4f} "
          f"(normal-day median closest to ~185 pts)")
else:
    print("\n  none hit target — widen the sweep")
