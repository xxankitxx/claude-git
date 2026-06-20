"""
MYTHOS — price flow: candles, indicators, VWAP, cumulative volume delta.

All classes are incremental (O(1) per tick/candle) and single-threaded by
contract: they are only ever touched from the analytics thread. The WS callback
never calls into here — it only writes the PriceStore (run7's proven design).

CVD note (first principles): Breeze WS ticks don't carry per-trade aggressor
side. The standard proxy is the tick rule — a trade at/above ask (or an uptick)
is buyer-initiated, at/below bid (or downtick) seller-initiated. We apply it to
futures ticks using best bid/ask when present, falling back to tick direction.
It is a proxy, and is treated as one: CVD gets 0.25 weight, not a veto.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from . import clk, config


def _safe_snap(dq):
    """Snapshot a deque another thread may be appending to. A bare ``list(dq)``
    or ``for x in dq`` raises ``RuntimeError('deque mutated during iteration')``
    on a cross-thread race — and these histories ARE read off-thread (the 4 Hz
    exit loop and, transitively, the dashboard push both call slope()/divergence
    while the analytics thread appends). Retry a few times, then return [] so the
    caller degrades to a safe default instead of crashing the read (which, via
    build_state, silently dropped 2,203 price pushes on 2026-06-15)."""
    for _ in range(3):
        try:
            return list(dq)
        except RuntimeError:
            continue
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Candles
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Candle:
    ts:     float          # epoch of candle open
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float = 0.0


class CandleAggregator:
    """Builds fixed-interval candles from a stream of (price, volume) updates."""

    def __init__(self, interval: float = 60.0, max_candles: int = 400):
        self.interval = interval
        self._candles: deque = deque(maxlen=max_candles)
        self._cur: Optional[Candle] = None

    def update(self, price: float, volume: float = 0.0,
               now: Optional[float] = None) -> Optional[Candle]:
        """Feed a tick. Returns the just-closed candle when one completes."""
        if price <= 0:
            return None
        now = now if now is not None else clk.now()
        bucket = now - (now % self.interval)
        closed = None
        if self._cur is None:
            self._cur = Candle(bucket, price, price, price, price, volume)
        elif bucket > self._cur.ts:
            closed = self._cur
            self._candles.append(closed)
            self._cur = Candle(bucket, price, price, price, price, volume)
        else:
            c = self._cur
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume
        return closed

    @property
    def candles(self) -> List[Candle]:
        return list(self._candles)

    @property
    def current(self) -> Optional[Candle]:
        return self._cur

    def closes(self, n: int = 0) -> List[float]:
        cs = [c.close for c in self._candles]
        return cs[-n:] if n else cs

    def reset(self):
        self._candles.clear()
        self._cur = None


# ─────────────────────────────────────────────────────────────────────────────
# Indicators — incremental on closed candles
# ─────────────────────────────────────────────────────────────────────────────
class RSI:
    """Wilder RSI(14) on closed candles."""

    def __init__(self, period: int = 14):
        self.period = period
        self._avg_gain = self._avg_loss = 0.0
        self._prev_close: Optional[float] = None
        self._n = 0
        self.value: float = 50.0

    def on_candle(self, c: Candle):
        if self._prev_close is None:
            self._prev_close = c.close
            return
        chg = c.close - self._prev_close
        gain, loss = max(chg, 0.0), max(-chg, 0.0)
        self._n += 1
        if self._n <= self.period:
            self._avg_gain += gain / self.period
            self._avg_loss += loss / self.period
        else:
            p = self.period
            self._avg_gain = (self._avg_gain * (p - 1) + gain) / p
            self._avg_loss = (self._avg_loss * (p - 1) + loss) / p
        self._prev_close = c.close
        if self._n >= self.period:
            if self._avg_loss <= 1e-12:
                self.value = 100.0
            else:
                rs = self._avg_gain / self._avg_loss
                self.value = 100.0 - 100.0 / (1.0 + rs)

    @property
    def ready(self) -> bool:
        return self._n >= self.period


class ATR:
    """Wilder ATR on closed candles."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float = 0.0
        self._prev_close: Optional[float] = None
        self._n = 0

    def on_candle(self, c: Candle):
        if self._prev_close is None:
            tr = c.high - c.low
        else:
            tr = max(c.high - c.low,
                     abs(c.high - self._prev_close),
                     abs(c.low - self._prev_close))
        self._n += 1
        if self._n == 1:
            self.value = tr
        else:
            p = min(self._n, self.period)
            self.value = (self.value * (p - 1) + tr) / p
        self._prev_close = c.close

    @property
    def ready(self) -> bool:
        return self._n >= self.period


class SuperTrend:
    """SuperTrend(10, 3) — direction only ('UP' / 'DOWN' / 'NA')."""

    def __init__(self, period: int = 10, mult: float = 3.0):
        self.period, self.mult = period, mult
        self._atr = ATR(period)
        self._upper = self._lower = 0.0
        self.direction: str = "NA"
        self._prev_close = 0.0

    def on_candle(self, c: Candle):
        self._atr.on_candle(c)
        if not self._atr.ready:
            self._prev_close = c.close
            return
        mid = (c.high + c.low) / 2.0
        band = self.mult * self._atr.value
        upper, lower = mid + band, mid - band
        # band ratcheting
        if self._prev_close > self._upper or upper < self._upper or self._upper == 0:
            self._upper = upper
        if self._prev_close < self._lower or lower > self._lower or self._lower == 0:
            self._lower = lower
        if self.direction == "NA":
            self.direction = "UP" if c.close > self._upper else "DOWN"
        elif self.direction == "UP" and c.close < self._lower:
            self.direction = "DOWN"
            self._upper = upper
        elif self.direction == "DOWN" and c.close > self._upper:
            self.direction = "UP"
            self._lower = lower
        self._prev_close = c.close


class ADX:
    """Wilder ADX(14) on closed candles — trend strength filter."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float = 0.0
        self._prev: Optional[Candle] = None
        self._atr = self._pdm = self._ndm = 0.0
        self._dx_hist: deque = deque(maxlen=period)
        self._n = 0

    def on_candle(self, c: Candle):
        if self._prev is None:
            self._prev = c
            return
        p = self._prev
        up, dn = c.high - p.high, p.low - c.low
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        self._n += 1
        per = self.period
        if self._n == 1:
            self._atr, self._pdm, self._ndm = tr, pdm, ndm
        else:
            self._atr = self._atr - self._atr / per + tr
            self._pdm = self._pdm - self._pdm / per + pdm
            self._ndm = self._ndm - self._ndm / per + ndm
        if self._atr > 0:
            pdi = 100.0 * self._pdm / self._atr
            ndi = 100.0 * self._ndm / self._atr
            denom = pdi + ndi
            if denom > 0:
                self._dx_hist.append(100.0 * abs(pdi - ndi) / denom)
                if len(self._dx_hist) == self.period:
                    self.value = sum(self._dx_hist) / self.period
        self._prev = c

    @property
    def ready(self) -> bool:
        return len(self._dx_hist) >= self.period


# ─────────────────────────────────────────────────────────────────────────────
# VWAP — session anchored
# ─────────────────────────────────────────────────────────────────────────────
class VWAP:
    """Session VWAP from futures ticks. With no per-tick volume available on
    every tick, falls back to time-weighting (each tick weight 1) — for an
    'above/below VWAP' regime test the difference is immaterial."""

    def __init__(self):
        self._pv = 0.0
        self._v = 0.0
        self.value: float = 0.0

    def update(self, price: float, volume: float = 1.0):
        if price <= 0:
            return
        w = volume if volume > 0 else 1.0
        self._pv += price * w
        self._v += w
        self.value = self._pv / self._v

    def reset(self):
        self._pv = self._v = 0.0
        self.value = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Cumulative Volume Delta (tick-rule proxy)
# ─────────────────────────────────────────────────────────────────────────────
class CVD:
    """Cumulative volume delta on futures ticks with slope/acceleration.

    Keeps a 1-second-resolution history ring so the signal engine can measure
    'rising and accelerating' (Requirement §6) and the commentary engine can
    detect CVD/price divergence in σ units.
    """

    def __init__(self):
        self.value: float = 0.0
        self._last_price: float = 0.0
        self._last_dir: int = 0
        self._hist: deque = deque(maxlen=900)    # (epoch, cvd, price) ~15 min
        self._last_hist_ts: float = 0.0

    def on_tick(self, price: float, qty: float = 1.0,
                bid: float = 0.0, ask: float = 0.0):
        if price <= 0:
            return
        if ask > 0 and price >= ask:
            d = 1
        elif bid > 0 and price <= bid:
            d = -1
        elif self._last_price > 0:
            if price > self._last_price:
                d = 1
            elif price < self._last_price:
                d = -1
            else:
                d = self._last_dir          # zero-tick: inherit
        else:
            d = 0
        self._last_dir = d
        self._last_price = price
        self.value += d * (qty if qty > 0 else 1.0)

        now = clk.now()
        if now - self._last_hist_ts >= 1.0:
            self._hist.append((now, self.value, price))
            self._last_hist_ts = now

    def slope(self, seconds: float = 60.0) -> float:
        """CVD change per second over the window."""
        hist = _safe_snap(self._hist)            # tear-proof: read off-thread
        if len(hist) < 2:
            return 0.0
        now = hist[-1][0]
        past = None
        for ts, cvd, _ in hist:
            if now - ts <= seconds:
                past = (ts, cvd)
                break
        if past is None or now - past[0] < 1.0:
            return 0.0
        return (hist[-1][1] - past[1]) / (now - past[0])

    def accelerating(self) -> bool:
        """Recent 1-min slope stronger than prior 3-min slope (same sign)."""
        s1, s3 = self.slope(60), self.slope(180)
        return abs(s1) > abs(s3) and (s1 * s3 >= 0)

    def divergence_sigma(self, window: int = 300) -> float:
        """How unusual is current CVD-vs-price disagreement, in σ of the
        rolling residuals. Positive = CVD strong while price lagging (bullish
        anomaly), negative = mirror. 0 when insufficient history."""
        pts = [(c, p) for _, c, p in _safe_snap(self._hist)[-window:]]
        if len(pts) < 60:
            return 0.0
        import statistics
        cvds = [c for c, _ in pts]
        prices = [p for _, p in pts]
        # z-score both series over the window, residual = z_cvd - z_price
        try:
            mc, sc = statistics.fmean(cvds), statistics.pstdev(cvds)
            mp, sp = statistics.fmean(prices), statistics.pstdev(prices)
        except statistics.StatisticsError:
            return 0.0
        if sc <= 1e-9 or sp <= 1e-9:
            return 0.0
        resid = [(c - mc) / sc - (p - mp) / sp for c, p in pts]
        mr, sr = statistics.fmean(resid), statistics.pstdev(resid)
        if sr <= 1e-9:
            return 0.0
        return (resid[-1] - mr) / sr

    def reset(self):
        self.value = 0.0
        self._last_price = 0.0
        self._last_dir = 0
        self._hist.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Kinematics — 1st/2nd/3rd derivatives of any tracked quantity.
#   v (velocity)     : direction and speed of the move
#   a (acceleration) : is the move gaining or losing force — turns positive
#                      BEFORE price turns at a bottom (the cheapest entry),
#                      turns negative BEFORE the top prints (the exit warning)
#   j (jerk)         : change of force — displayed for context, too noisy to
#                      gate decisions on its own
# Cascaded EMA-of-differences: each stage smoothed before differentiating the
# next, which keeps 2nd/3rd derivatives usable on 1 Hz market data.
# ─────────────────────────────────────────────────────────────────────────────
class Kinematics:
    def __init__(self, alpha: float = 0.30):
        self.alpha = alpha
        self._s: Optional[float] = None     # smoothed level (folds EVERY tick)
        self._s_mark: float = 0.0           # _s captured at the last derivative step
        self._ts: float = 0.0               # time of the last derivative step
        self.v: float = 0.0
        self.a: float = 0.0
        self.j: float = 0.0

    def update(self, x: float, now: Optional[float] = None):
        if x is None or x <= 0:
            return
        now = clk.now() if now is None else now   # 0.0 is a valid stamp, not falsy
        if self._s is None:
            self._s = self._s_mark = x
            self._ts = now
            return
        if now <= self._ts:
            return                      # ignore duplicate / out-of-order stamps
        # Fold EVERY in-order observation into the smoother. The old code
        # early-returned on sub-0.25s ticks *before* folding, so a fast burst of
        # ticks — precisely a fast inflection, the move this signal exists to
        # lead — dropped 2 of every 3 observations and starved the v→a→j chain
        # exactly when it mattered. Now the level always sees them; we still
        # DIFFERENTIATE only on steps ≥0.25s apart (the anti-noise intent), so at
        # the engine's 1 Hz cadence — where no sub-0.25s ticks occur — the output
        # is byte-identical to before (_s_mark == the old prev_s).
        self._s += self.alpha * (x - self._s)
        dt = now - self._ts
        if dt < 0.25:
            return                      # accumulate; differentiate next ≥0.25s step
        prev_v, prev_a = self.v, self.a
        v_raw = (self._s - self._s_mark) / dt
        self.v += self.alpha * (v_raw - self.v)
        a_raw = (self.v - prev_v) / dt
        self.a += self.alpha * (a_raw - self.a)
        j_raw = (self.a - prev_a) / dt
        self.j += self.alpha * (j_raw - self.j)
        self._s_mark = self._s
        self._ts = now

    def reset(self):
        self._s = None
        self._s_mark = 0.0
        self._ts = 0.0          # clear the derivative clock too, else the
        self.v = self.a = self.j = 0.0   # now<=_ts guard rejects fresh post-reset ticks


# ─────────────────────────────────────────────────────────────────────────────
# Swing pivots — genuine 30-35 pt legs launch from intraday swing highs/lows,
# not only from OI walls. A local extreme that price reverses ≥15 pts away
# from is a confirmed pivot; recent pivots are tradeable zones.
# ─────────────────────────────────────────────────────────────────────────────
class SwingPivots:
    REVERSAL = 15.0

    def __init__(self):
        # start tracking a high: _dir MUST be non-zero — at 0 the extreme
        # followed price in BOTH directions and no reversal could ever fire
        # (bug: zero pivots produced, swing zones invisible to the hunter)
        self._dir = 1
        self._ext = 0.0
        self.pivots: deque = deque(maxlen=10)   # (level, 'H'|'L', epoch)

    def update(self, price: float):
        if price <= 0:
            return
        if self._ext == 0.0:
            self._ext = price
            return
        if self._dir >= 0 and price >= self._ext:
            self._ext = price
        elif self._dir <= 0 and price <= self._ext:
            self._ext = price
        elif self._dir >= 0 and self._ext - price >= self.REVERSAL:
            # round to 5 pts: stable zone identity, no flicker downstream
            self.pivots.append((round(self._ext / 5) * 5, "H", clk.now()))
            self._dir = -1
            self._ext = price
        elif self._dir <= 0 and price - self._ext >= self.REVERSAL:
            self.pivots.append((round(self._ext / 5) * 5, "L", clk.now()))
            self._dir = 1
            self._ext = price

    def supports(self) -> List[float]:
        return [lvl for lvl, kind, _ in self.pivots if kind == "L"]

    def resistances(self) -> List[float]:
        return [lvl for lvl, kind, _ in self.pivots if kind == "H"]

    def reset(self):
        self._dir = 1
        self._ext = 0.0
        self.pivots.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Anchored VWAP from the day's extremes — reversal lenses:
#   AVWAP-from-day-HIGH = average price of everyone who sold the top.
#       Price crossing ABOVE it → those bears are underwater → squeeze fuel (CE).
#   AVWAP-from-day-LOW  = average price of everyone who bought the bottom.
#       Price crossing BELOW it → those bulls are underwater → flush fuel (PE).
# Each anchor restarts whenever a new extreme is printed.
# ─────────────────────────────────────────────────────────────────────────────
class AnchoredVWAP:
    def __init__(self):
        self.day_high: float = 0.0
        self.day_low: float = 0.0
        self._hi_pv = self._hi_v = 0.0
        self._lo_pv = self._lo_v = 0.0
        self.from_high: float = 0.0
        self.from_low: float = 0.0

    def update(self, price: float, volume: float = 1.0):
        if price <= 0:
            return
        w = volume if volume > 0 else 1.0
        if self.day_high == 0.0 or price > self.day_high:
            self.day_high = price
            self._hi_pv = self._hi_v = 0.0      # re-anchor at the new high
        if self.day_low == 0.0 or price < self.day_low:
            self.day_low = price
            self._lo_pv = self._lo_v = 0.0      # re-anchor at the new low
        self._hi_pv += price * w
        self._hi_v += w
        self._lo_pv += price * w
        self._lo_v += w
        self.from_high = self._hi_pv / self._hi_v
        self.from_low = self._lo_pv / self._lo_v

    def reset(self):
        self.__init__()


# ─────────────────────────────────────────────────────────────────────────────
# Futures OI quadrant — the classic positioning read (user's reversal fuel):
#   price ↑ + OI ↑ = LONG BUILDUP      (fresh longs — bullish)
#   price ↑ + OI ↓ = SHORT COVERING    (shorts bailing — bullish fuel, fast)
#   price ↓ + OI ↑ = SHORT BUILDUP     (fresh shorts — bearish)
#   price ↓ + OI ↓ = LONG UNWINDING    (longs abandoning — bearish fuel, fast)
# The two ↓-OI quadrants are the user's "best money": forced unwinding
# accelerates moves off a reversal zone.
# ─────────────────────────────────────────────────────────────────────────────
class FuturesOIQuadrant:
    WINDOW = 180.0          # compare vs ~3 minutes ago

    def __init__(self):
        self._hist: deque = deque(maxlen=900)   # (epoch, price, oi)
        self.quadrant: str = "NEUTRAL"
        self.price_bps: float = 0.0
        self.oi_pct: float = 0.0

    def update(self, price: float, oi: float):
        if price <= 0 or oi <= 0:
            return
        now = clk.now()
        if self._hist and now - self._hist[-1][0] < 1.0:
            self._hist[-1] = (self._hist[-1][0], price, oi)
        else:
            self._hist.append((now, price, oi))
        self._recompute(now)

    def _recompute(self, now: float):
        past = None
        for ts, p, o in self._hist:
            if now - ts <= self.WINDOW:
                past = (p, o)
                break
        if past is None or len(self._hist) < 5:
            return
        p_now, o_now = self._hist[-1][1], self._hist[-1][2]
        self.price_bps = (p_now - past[0]) / past[0] * 10000.0
        self.oi_pct = (o_now - past[1]) / past[1] * 100.0
        up, dn = self.price_bps > 4.0, self.price_bps < -4.0     # ~10 pts
        oi_up, oi_dn = self.oi_pct > 0.10, self.oi_pct < -0.10
        if up and oi_up:
            self.quadrant = "LONG_BUILDUP"
        elif up and oi_dn:
            self.quadrant = "SHORT_COVERING"
        elif dn and oi_up:
            self.quadrant = "SHORT_BUILDUP"
        elif dn and oi_dn:
            self.quadrant = "LONG_UNWINDING"
        else:
            self.quadrant = "NEUTRAL"

    def reset(self):
        self._hist.clear()
        self.quadrant = "NEUTRAL"
        self.price_bps = self.oi_pct = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Composite tracker bundling the spot/futures indicator stack
# ─────────────────────────────────────────────────────────────────────────────
class FlowStack:
    """Everything the signal engine needs about price flow, in one object.
    Fed by the analytics thread: per-tick (futures price) and per-candle."""

    def __init__(self):
        self.candles_1m = CandleAggregator(60.0)
        self.rsi = RSI(14)
        self.atr = ATR(14)
        self.supertrend = SuperTrend(10, 3.0)
        self.adx = ADX(14)
        self.vwap = VWAP()
        self.cvd = CVD()
        self.fut_oi = FuturesOIQuadrant()
        self.avwap = AnchoredVWAP()
        self.swings = SwingPivots()

    def on_futures_tick(self, price: float, qty: float = 0.0,
                        bid: float = 0.0, ask: float = 0.0):
        self.vwap.update(price, qty)
        self.cvd.on_tick(price, qty, bid, ask)
        closed = self.candles_1m.update(price, qty)
        if closed:
            self.rsi.on_candle(closed)
            self.atr.on_candle(closed)
            self.supertrend.on_candle(closed)
            self.adx.on_candle(closed)

    def seed_candles(self, candles: List[Candle]):
        """Replay persisted candles after a mid-session restart so the
        indicators are warm instead of blind for 14+ minutes."""
        for c in candles:
            self.candles_1m._candles.append(c)
            self.rsi.on_candle(c)
            self.atr.on_candle(c)
            self.supertrend.on_candle(c)
            self.adx.on_candle(c)

    def reset_session(self):
        self.candles_1m.reset()
        self.rsi = RSI(14)
        self.atr = ATR(14)
        self.supertrend = SuperTrend(10, 3.0)
        self.adx = ADX(14)
        self.vwap.reset()
        self.cvd.reset()
        self.fut_oi.reset()
        self.avwap.reset()
        self.swings.reset()
