"""
MYTHOS — simulation feed: a synthetic but statistically plausible Nifty
session driven through the exact same PriceStore writers the live WS uses.
Purpose: full-stack verification (feed → analytics → signals → trades → UI →
audio → archive) any time of day, with zero API dependency.

Market model:
    spot      — regime-switching random walk: TREND_UP / TREND_DOWN / CHOP
                phases of 2-6 minutes with realistic drift and noise
    futures   — spot + basis(≈18) + microstructure noise, with qty/bid/ask
    options   — Black-Scholes prices off the live sim spot with a skewed IV
                surface + quote noise; OI evolves with direction-dependent
                writing/unwinding so OI walls and PCR move like the real thing
    basket    — heavyweights random-walk correlated to the index by beta
    VIX       — slow mean-reverting walk, jumps on regime flips
"""

import logging
import math
import random
import threading
import time
from datetime import datetime
from typing import Dict

import numpy as np

from . import config, greeks
from .config import IST

log = logging.getLogger("mythos.sim")


class SimFeed:
    TICK_SEC = 0.20

    def __init__(self, prices, basket):
        self.prices = prices
        self.basket = basket
        self._stop = threading.Event()
        self._thread = None

        self.spot = 26000.0 + random.uniform(-150, 150)
        self.session_open = self.spot          # anchor for the daily range cap
        self.fut_oi = 1.2e7                    # futures OI for quadrant analysis
        self.hw_volc: dict = {}                # heavyweight cumulative volumes
        # swing-structure state: markets move in LEGS (impulse → pullback),
        # printing higher highs/higher lows in uptrends — not smooth drift
        self.leg_dir = random.choice([1, -1])
        self.leg_target = self.spot + self.leg_dir * random.uniform(30, 60)
        self.leg_speed = 0.45                  # pts per tick toward target
        self.last_impulse = 45.0
        self.regime = "CHOP"
        self._regime_until = 0.0
        self.vix = 13.5
        self.base_iv = 0.135

        # per-strike OI state
        self.oi: Dict[tuple, float] = {}
        self.vol_cum: Dict[tuple, float] = {}

        # heavyweights: symbol -> (price, beta)
        self.hw: Dict[str, list] = {}
        base_prices = {
            "HDFCBANK": 1850, "ICICIBANK": 1420, "RELIANCE": 1530,
            "INFY": 1880, "BHARTIARTL": 1720, "LT": 3950, "ITC": 480,
            "TCS": 4300, "AXISBANK": 1280, "SBIN": 920, "KOTAKBANK": 2150,
            "M&M": 3300, "BAJFINANCE": 7400, "HINDUNILVR": 2600,
        }
        for sym in config.HEAVYWEIGHTS:
            p = base_prices.get(sym, 1000) * random.uniform(0.97, 1.03)
            self.hw[sym] = [p, random.uniform(0.7, 1.4)]

    def warmup_candles(self, minutes: int = 30):
        """Synthetic 1-min candle history ENDING at the current sim spot, with
        a directional tail so momentum indicators are warm and aligned from
        the first second instead of blind for 14+ minutes (RSI/SuperTrend/ADX
        warmup was why early sim sessions produced zero trades)."""
        from .flow import Candle
        now = time.time()
        path = []
        p = self.spot
        tail = random.choice([+1.4, -1.4])   # random recent trend direction
        # walk BACKWARD from current spot: last ~8 bars trend into the present
        for i in range(minutes):
            drift = tail if i < 8 else random.gauss(0, 0.4)
            p -= drift + random.gauss(0, 4.0)
            path.append(p)
        path.reverse()                       # oldest first, ends near spot
        candles = []
        prev = path[0]
        for i, close in enumerate(path):
            ts = now - (minutes - i) * 60.0
            hi = max(prev, close) + abs(random.gauss(0, 2.0))
            lo = min(prev, close) - abs(random.gauss(0, 2.0))
            candles.append(Candle(ts, prev, hi, lo, close,
                                  random.uniform(3e4, 1e5)))
            prev = close
        return candles

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        self._seed_oi()
        self._seed_basket()
        self.prices.vix = self.vix
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="SimFeed")
        self._thread.start()
        log.info("SimFeed started: spot %.1f", self.spot)

    def stop(self):
        self._stop.set()

    # ── seeding ──────────────────────────────────────────────────────────────
    def _seed_oi(self):
        """REAL chain shape (user correction): OI concentrates in OTM strikes —
        calls ABOVE spot, puts BELOW spot, peaking 2-4 strikes out — while ITM
        OI is a small fraction (writers don't sell ITM; buyers book it out)."""
        atm = round(self.spot / config.STRIKE_STEP) * config.STRIKE_STEP
        for off in range(-12, 13):
            k = atm + off * config.STRIKE_STEP
            wall = 2.2 if abs(off) in (3, 6) else 1.0
            # CALLS: OTM above spot, peak near +3 strikes; ITM ≈ 15%
            if off >= 0:
                call_base = 3.8e6 * math.exp(-((off - 3) / 3.5) ** 2)
            else:
                call_base = 0.15 * 3.8e6 * math.exp(-(abs(off) / 4.0) ** 2)
            # PUTS: OTM below spot, peak near −3 strikes; ITM ≈ 15%
            if off <= 0:
                put_base = 3.8e6 * math.exp(-((off + 3) / 3.5) ** 2)
            else:
                put_base = 0.15 * 3.8e6 * math.exp(-(off / 4.0) ** 2)
            self.oi[(k, "call")] = max(4e4, call_base
                                       * (wall if off > 0 else 1.0)
                                       * random.uniform(0.85, 1.15))
            self.oi[(k, "put")] = max(4e4, put_base
                                      * (wall if off < 0 else 1.0)
                                      * random.uniform(0.85, 1.15))
            self.vol_cum[(k, "put")] = random.uniform(1e5, 5e5)
            self.vol_cum[(k, "call")] = random.uniform(1e5, 5e5)
        self.prices.chain_oi = dict(self.oi)

    def _seed_basket(self):
        # sister indices: BN ≈ 2.2× Nifty level, FN ≈ 0.95× (sentiment inputs)
        self.idx = {"BANKNIFTY": self.spot * 2.2, "FINNIFTY": self.spot * 0.95}
        for name, lvl in self.idx.items():
            self.prices.idx_ltp[name] = lvl
            self.prices.idx_prev[name] = lvl * random.uniform(0.995, 1.005)
            self.prices.idx_ts[name] = time.monotonic()
        for sym, (p, _beta) in self.hw.items():
            self.basket.set_prev_close(sym, p * random.uniform(0.995, 1.005), p)
        # synthetic stock chains so PCR/walls populate
        for sym, (p, _b) in self.hw.items():
            step = max(round(p * 0.01 / 5) * 5, 5)
            atm_k = round(p / step) * step
            ce, pe = [], []
            for off in range(-6, 7):
                k = atm_k + off * step
                ce.append({"strike_price": k,
                           "open_interest": 2e5 * math.exp(-(off / 3) ** 2)
                           * (1.5 if off == 2 else 1.0)})
                pe.append({"strike_price": k,
                           "open_interest": 2e5 * math.exp(-(off / 3) ** 2)
                           * (1.6 if off == -2 else 1.0)})
            self.basket.on_chain(sym, ce, pe)

    # ── main loop ────────────────────────────────────────────────────────────
    def _run(self):
        last_opt = 0.0
        last_hw = 0.0
        last_chain = 0.0
        while not self._stop.is_set():
            now = time.time()
            self._step_regime(now)
            self._step_spot()
            if now - last_opt >= 0.5:
                self._step_options()
                last_opt = now
            if now - last_hw >= 1.0:
                self._step_heavyweights()
                last_hw = now
            if now - last_chain >= 30.0:
                self.prices.chain_oi = dict(self.oi)
                last_chain = now
            time.sleep(self.TICK_SEC)

    def _step_regime(self, now: float):
        if now < self._regime_until:
            return
        r = random.random()
        # trend-rich tape (40/40/20) with longer phases: the sim exists to
        # exercise the trade engine, not to model an average dull Tuesday
        self.regime = ("TREND_UP" if r < 0.40 else
                       "TREND_DOWN" if r < 0.80 else "CHOP")
        self._regime_until = now + random.uniform(120, 300)
        if "TREND" in self.regime:
            self.vix = min(35.0, self.vix + random.uniform(0.1, 0.9))
        else:
            self.vix = max(10.0, self.vix - random.uniform(0.0, 0.5))
        self.prices.vix = round(self.vix, 2)
        log.info("SIM regime → %s (until +%.0fs) VIX %.1f",
                 self.regime, self._regime_until - now, self.vix)

    def _new_leg(self):
        """Start the next swing leg. Trends print impulse→pullback sequences
        (higher highs + higher lows up, mirror down); chop oscillates."""
        trending = self.regime in ("TREND_UP", "TREND_DOWN")
        trend_dir = 1 if self.regime == "TREND_UP" else -1
        if trending:
            if self.leg_dir == trend_dir:
                # impulse just ended → pullback retraces 35-62% of it
                self.leg_dir = -trend_dir
                size = self.last_impulse * random.uniform(0.35, 0.62)
            else:
                # pullback ended → next impulse, slightly larger than retrace
                self.leg_dir = trend_dir
                size = random.uniform(30, 75)
                self.last_impulse = size
        else:
            # chop: alternate roughly-equal legs around the current area
            self.leg_dir = -self.leg_dir
            size = random.uniform(18, 45)
        # daily range cap: beyond ±300 from open, force the leg back inward
        dev = self.spot - self.session_open
        if dev > 300:
            self.leg_dir = -1
        elif dev < -300:
            self.leg_dir = 1
        self.leg_target = self.spot + self.leg_dir * size
        # leg duration 25-90 s → speed in pts/tick (5 ticks/s)
        self.leg_speed = size / random.uniform(25, 90) / 5.0

    def _step_spot(self):
        # move TOWARD the current leg target with breathing noise; real tape
        # overshoots and stalls, so steps vary tick to tick
        remaining = self.leg_target - self.spot
        if abs(remaining) < 2.0 or random.random() < 0.002:
            self._new_leg()
            remaining = self.leg_target - self.spot
        step = (1 if remaining > 0 else -1) * self.leg_speed \
            * random.uniform(0.4, 1.7) + random.gauss(0, 0.45)
        self.spot = max(10000.0, self.spot + step)
        self.prices._write_spot(round(self.spot, 2))
        # futures tick
        fut = self.spot + 18.0 + random.gauss(0, 0.8)
        qty = random.choice([25, 50, 75, 150, 300, 600])
        # order-flow imprint: trending tape prints more at ask/bid
        aggress = random.random()
        if self.regime == "TREND_UP" and aggress < 0.62:
            bid, ask = fut - 0.4, fut          # printed at ask
        elif self.regime == "TREND_DOWN" and aggress < 0.62:
            bid, ask = fut, fut + 0.4          # printed at bid
        else:
            bid, ask = fut - 0.25, fut + 0.25
        # futures OI evolution: trends mostly BUILD OI (fresh positions), but
        # ~35% of trend phases run on UNWINDING (the user's reversal fuel) —
        # price moving while OI drops = forced exits accelerating the move
        if self.regime == "TREND_UP":
            self.fut_oi += random.gauss(800, 900) * (1 if random.random() < 0.65 else -1.6)
        elif self.regime == "TREND_DOWN":
            self.fut_oi += random.gauss(800, 900) * (1 if random.random() < 0.65 else -1.6)
        else:
            self.fut_oi += random.gauss(0, 500)
        self.fut_oi = max(5e6, self.fut_oi)
        self.prices.fut_bqty = random.uniform(5e3, 4e4)
        self.prices.fut_aqty = random.uniform(5e3, 4e4)
        self.prices._write_futures(round(fut, 2), qty, round(bid, 2),
                                   round(ask, 2), round(self.fut_oi, 0))

    def _step_options(self):
        atm = round(self.spot / config.STRIKE_STEP) * config.STRIKE_STEP
        T = greeks.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))
        T = max(T, 0.7 / 365.0)        # keep premiums alive even on expiry eve
        iv_base = self.base_iv * (self.vix / 13.5)

        # price the current ATM window PLUS every strike ever quoted — a held
        # position's strike must NEVER stop ticking. (Bug: spot trended out of
        # the ±8 window, the held strike's quote froze, and the exit logic
        # compared a frozen price against the trail forever — stuck position.)
        strikes = {atm + off * config.STRIKE_STEP
                   for off in range(-config.NUM_STRIKES, config.NUM_STRIKES + 1)}
        strikes.update(k for k, _right in self.prices.opt_ltp.keys())

        for k in sorted(strikes):
            off = (k - atm) / config.STRIKE_STEP
            money = (k - self.spot) / self.spot
            for right in ("call", "put"):
                # skewed surface: puts richer below, smile on wings
                skew = 0.022 * max(0.0, -money) * 100 if right == "put" \
                    else 0.010 * max(0.0, money) * 100
                iv = max(0.07, iv_base + skew + 0.0012 * abs(off)
                         + random.gauss(0, 0.002))
                px = float(greeks.bs_price(self.spot, k, T, iv, right))
                px = max(0.05, px + random.gauss(0, 0.35))
                spread = max(0.1, min(1.8, px * 0.004 + random.uniform(0.05, 0.5)))

                key = (k, right)
                # OI evolution: trend up → put writing below (support builds),
                # call unwind above; mirrored for downtrend.
                # Scale: ~±1800 per 0.5 s on a 30-50L base ≈ 20-30%/hour at the
                # active strikes — brisk but realistic. (Earlier ±9500 ballooned
                # OTM put OI to absurd multiples within minutes — user caught it.)
                oi = self.oi.get(key, 1e6)
                drift = random.gauss(0, 400)
                if self.regime == "TREND_UP":
                    if right == "put" and k <= atm:
                        drift += 1800          # bulls defending — support builds
                    if right == "call" and k >= atm:
                        drift -= 800           # bears covering — OI unwind signal
                elif self.regime == "TREND_DOWN":
                    if right == "call" and k >= atm:
                        drift += 1800
                    if right == "put" and k <= atm:
                        drift -= 800
                # OI concentrates near the money in reality — fade far strikes
                if abs(off) > 6:
                    drift *= 0.4
                self.oi[key] = max(5e4, oi + drift)
                self.vol_cum[key] = self.vol_cum.get(key, 1e5) \
                    + abs(random.gauss(0, 900)) * (3.0 if abs(off) <= 1 else 1.0)

                # order-book depth: trending tape stacks bids on the favoured
                # side (lets the bid/ask-imbalance commentary fire in sim)
                bqty = random.uniform(2e3, 1.5e4)
                aqty = random.uniform(2e3, 1.5e4)
                aligned = ((self.regime == "TREND_UP" and right == "call") or
                           (self.regime == "TREND_DOWN" and right == "put"))
                if aligned and abs(off) <= 2 and random.random() < 0.25:
                    bqty *= random.uniform(2.5, 5.0)     # buyers stacking
                self.prices._write_option(
                    k, right, round(px, 2), round(self.oi[key], 0),
                    round(self.vol_cum[key], 0),
                    round(px - spread / 2, 2), round(px + spread / 2, 2),
                    round(bqty, 0), round(aqty, 0))

    def _step_heavyweights(self):
        idx_ret = {"TREND_UP": 0.00002, "TREND_DOWN": -0.00002,
                   "CHOP": 0.0}[self.regime]
        # sister indices track the Nifty leg with their own beta + noise
        leg = 1 if self.leg_dir > 0 else -1
        for name, beta in (("BANKNIFTY", 1.25), ("FINNIFTY", 1.05)):
            lvl = self.idx[name]
            ret = leg * self.leg_speed / max(self.spot, 1) * beta \
                + random.gauss(0, 0.00012)
            self.idx[name] = max(1000.0, lvl * (1 + ret))
            self.prices.idx_ltp[name] = round(self.idx[name], 2)
            self.prices.idx_ts[name] = time.monotonic()
        for sym, state in self.hw.items():
            p, beta = state
            ret = idx_ret * beta + random.gauss(0, 0.00035)
            state[0] = max(1.0, p * (1 + ret))
            # volume + book sizes so the activity radars have live material
            self.hw_volc[sym] = self.hw_volc.get(sym, 1e5) \
                + abs(random.gauss(0, 2500)) * (4.0 if random.random() < 0.03 else 1.0)
            bq = random.uniform(2e3, 2e4)
            aq = random.uniform(2e3, 2e4)
            if random.random() < 0.04:           # occasional pressure burst
                if random.random() < 0.5:
                    bq *= random.uniform(2.5, 5)
                else:
                    aq *= random.uniform(2.5, 5)
            self.prices._write_hw(sym, round(state[0], 2),
                                  round(self.hw_volc[sym], 0),
                                  round(bq, 0), round(aq, 0))
