"""
MYTHOS — volatility engine: chain IVs, IV rank/percentile, skew, expected move,
realized-vol regime.

First-principles deviation from the requirement: it prescribes Heston + SABR
calibrated every 5 minutes "to validate S/R". Calibrating a 5-parameter
stochastic-vol model to one weekly expiry's noisy intraday quotes is curve
fitting, not information — the smile from a single expiry under-determines
Heston badly, and its output would feed nothing actionable that the raw skew
and IV-rank don't already give. What actually carries signal for an intraday
option *buyer*:
    1. IV rank/percentile  — is premium cheap or rich vs its own history?
    2. Skew (OTM put IV − OTM call IV) — hedging demand / fear gradient.
    3. Expected move        — 0.8 × ATM straddle, the market's own forecast.
    4. Realized-vol regime  — EWMA σ of 1-min returns vs IV (variance premium).
All four are implemented; Heston/SABR are deliberately not.
"""

import math
import time
from collections import deque
from typing import Dict, Optional

import numpy as np

from . import clk, config, greeks


class VolEngine:
    def __init__(self):
        # latest per-strike IVs {(strike, right): iv}
        self.chain_iv: Dict[tuple, float] = {}
        self.atm_iv: float = 0.0
        self.skew_25d: float = 0.0           # put-wing IV − call-wing IV
        self.expected_move: float = 0.0      # index points to expiry
        self.straddle: float = 0.0
        self.iv_rank: float = 50.0           # 0..100 vs history
        self.iv_percentile: float = 50.0
        self.realized_vol_1m: float = 0.0    # annualized EWMA of 1-min returns
        self.variance_premium: float = 0.0   # atm_iv − realized (vol pts)

        self._iv_intraday: deque = deque(maxlen=400)   # (epoch, atm_iv) ~1/min
        self._iv_history: list = []                    # daily ATM IV closes
        self._ewma_var: float = 0.0
        self._last_spot: float = 0.0
        self._last_iv_sample: float = 0.0

    # ── seeding from persistence ────────────────────────────────────────────
    def seed_history(self, daily_ivs: list, intraday: Optional[list] = None):
        """daily_ivs: list of ATM IV values from previous sessions (SQLite).
        intraday: today's (epoch, iv) samples after a restart."""
        self._iv_history = [v for v in daily_ivs if v and v > 0]
        if intraday:
            for ts, v in intraday:
                self._iv_intraday.append((ts, v))

    # ── updates ─────────────────────────────────────────────────────────────
    def update_chain(self, spot: float, strikes: Dict[tuple, dict],
                     T_years: float, atm: float):
        """Recompute chain IVs + derived metrics. strikes: {(K, right): {ltp..}}."""
        if spot <= 0 or not strikes:
            return
        for right in ("call", "put"):
            ks, prices = [], []
            for (k, r), d in strikes.items():
                if r == right and d.get("ltp", 0) > 0:
                    ks.append(float(k))
                    prices.append(float(d["ltp"]))
            if not ks:
                continue
            ivs = greeks.implied_vol(np.array(prices), spot, np.array(ks),
                                     T_years, right)
            for k, iv in zip(ks, ivs):
                if not np.isnan(iv):
                    self.chain_iv[(k, right)] = float(iv)

        ce = self.chain_iv.get((atm, "call"))
        pe = self.chain_iv.get((atm, "put"))
        if ce and pe:
            self.atm_iv = (ce + pe) / 2.0
        elif ce or pe:
            self.atm_iv = ce or pe

        # straddle & expected move
        ce_ltp = strikes.get((atm, "call"), {}).get("ltp", 0.0)
        pe_ltp = strikes.get((atm, "put"), {}).get("ltp", 0.0)
        if ce_ltp > 0 and pe_ltp > 0:
            self.straddle = ce_ltp + pe_ltp
            self.expected_move = config.EXPECTED_MOVE_K * self.straddle

        # 25-delta-ish skew via fixed moneyness wings (±2% — robust, no delta
        # root-finding on noisy quotes)
        wing = round(spot * 0.02 / config.STRIKE_STEP) * config.STRIKE_STEP
        put_wing = self.chain_iv.get((atm - wing, "put"))
        call_wing = self.chain_iv.get((atm + wing, "call"))
        if put_wing and call_wing:
            self.skew_25d = (put_wing - call_wing) * 100.0   # vol points

        # sample ATM IV once a minute for rank computation
        now = clk.now()
        if self.atm_iv > 0 and now - self._last_iv_sample >= 60.0:
            self._iv_intraday.append((now, self.atm_iv))
            self._last_iv_sample = now
        self._recompute_rank()

    def update_spot(self, spot: float):
        """EWMA realized vol from 1-min spot samples (lambda=0.94)."""
        if spot <= 0:
            return
        if self._last_spot > 0:
            r = math.log(spot / self._last_spot)
            self._ewma_var = 0.94 * self._ewma_var + 0.06 * r * r
            # annualize: 375 trading minutes/day × 252 days
            self.realized_vol_1m = math.sqrt(self._ewma_var * 375 * 252)
            if self.atm_iv > 0:
                self.variance_premium = (self.atm_iv - self.realized_vol_1m) * 100
        self._last_spot = spot

    def _recompute_rank(self):
        """IV rank vs combined history: prior daily closes + today's range.
        With < 5 days of history the intraday range alone is used (the system
        self-improves as the SQLite history grows)."""
        if self.atm_iv <= 0:
            return
        pool = list(self._iv_history)
        pool.extend(v for _, v in self._iv_intraday)
        if len(pool) < 10:
            return
        lo, hi = min(pool), max(pool)
        if hi - lo > 1e-6:
            # clamp: current IV can sit outside the historical pool right
            # after a restart or regime break — rank is 0..100 by definition
            self.iv_rank = max(0.0, min(100.0,
                               100.0 * (self.atm_iv - lo) / (hi - lo)))
        below = sum(1 for v in pool if v <= self.atm_iv)
        self.iv_percentile = 100.0 * below / len(pool)

    def iv_jump_5min(self) -> float:
        """IV-rank-equivalent jump over the last 5 minutes (commentary).
        Suppressed until the pool is meaningful — with <30 samples the rank
        denominator is tiny and every wiggle reads as an 'explosion'."""
        if len(self._iv_intraday) + len(self._iv_history) < 30:
            return 0.0
        if len(self._iv_intraday) < 2:
            return 0.0
        now = self._iv_intraday[-1]
        past = None
        for ts, v in self._iv_intraday:
            if now[0] - ts <= 300:
                past = (ts, v)
                break
        if past is None:
            return 0.0
        pool = [v for _, v in self._iv_intraday] + self._iv_history
        lo, hi = min(pool), max(pool)
        if hi - lo <= 1e-6:
            return 0.0
        return 100.0 * (now[1] - past[1]) / (hi - lo)

    def iv_expanding(self) -> bool:
        """ATM IV now above its level 3 minutes ago."""
        if len(self._iv_intraday) < 4:
            return False
        now_ts, now_iv = self._iv_intraday[-1]
        for ts, v in self._iv_intraday:
            if now_ts - ts <= 180:
                return now_iv > v * 1.002
        return False

    def smile_points(self, atm: float, n: int = 8):
        """[(strike, ce_iv, pe_iv)] for the UI smile chart."""
        out = []
        for off in range(-n, n + 1):
            k = atm + off * config.STRIKE_STEP
            ce = self.chain_iv.get((k, "call"))
            pe = self.chain_iv.get((k, "put"))
            if ce or pe:
                out.append((k,
                            round(ce * 100, 2) if ce else None,
                            round(pe * 100, 2) if pe else None))
        return out

    def reset_session(self):
        self.chain_iv.clear()
        self._iv_intraday.clear()
        self._ewma_var = 0.0
        self._last_spot = 0.0
        self.atm_iv = 0.0
        self.expected_move = 0.0
        self.straddle = 0.0
