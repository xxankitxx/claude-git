"""
MYTHOS — REALISTIC simulation feed (RealisticSimFeed).

Drop-in replacement for SimFeed. Same public surface — start(), stop(),
warmup_candles() — and the SAME PriceStore writer contract:
    prices._write_spot(ltp)
    prices._write_futures(ltp, qty, bid, ask, oi)
    prices._write_option(strike, right, ltp, oi, vol, bid, ask, bqty, aqty)
    prices._write_hw(symbol, ltp, vol, bqty, aqty)
    prices.idx_ltp / idx_prev / idx_ts    (BANKNIFTY, FINNIFTY)
    prices.fut_bqty / fut_aqty
    prices.chain_oi                        (wide REST-style OI snapshot)
    prices.vix
and basket.set_prev_close(sym, prev, ltp) / basket.on_chain(sym, ce, pe)
/ basket.on_tick(sym, ltp).

Fixes the toy sim's two named defects ('unrealistic OI figures', 'movement
not real-like') plus the closed-loop critique:
  1. MOVEMENT: intraday-U-shape-scheduled momentum-then-reversion process
     (fast AR(1) micro-momentum + OU pull of a slow level toward a VWAP/
     prior-close/round magnet) with swing legs, an opening gap, and rare
     gated jumps. Per-second sigma derived by the correct sqrt(375)/sqrt(60)
     annualization, so a median day prints ~150-200 pt high-low and 300 stays
     a big day.
  2. OI in correct UNITS: NSE reports OI in underlying UNITS (not contracts).
     Walls ~5e6-1.5e7, ATM ~1e6-3e6, far OTM ~5e4, piled on round strikes and
     deliberately NOT lot-multiples. PCR held in 0.8-1.3.
  3. NON-TAUTOLOGY: CVD/order-flow comes from a SEPARATE aggressor process —
     each futures print is placed at bid (seller) or ask (buyer) by an AR(1)
     aggressor whose mean is the OBSERVABLE realized return plus its own noise,
     never the hidden drift state. CVD therefore emerges from the tape and can
     diverge from price.

Every tunable is a named constant in class P so the replay harness --set A/B
sweep can reach it.
"""

import logging
import math
import random
import threading
import time
from datetime import datetime
from typing import Dict, Tuple

import numpy as np

from . import clk, config, greeks
from .config import IST

log = logging.getLogger("mythos.sim")


# ============================================================================
# PARAMETER SPEC  (value + unit). Calibrated to 2026 Nifty (~23,400).
# ============================================================================
class P:
    # anchor
    SPOT_ANCHOR        = 23400.0     # index pts
    SPOT_ANCHOR_JITTER = 120.0       # pts +/-
    BASIS              = 18.0        # pts futures premium
    LOT_SIZE           = config.LOT_SIZE   # OI is generated in UNITS (lot-agnostic)

    # volatility regime
    VIX_INIT           = 13.5        # % annualized
    VIX_MIN            = 9.0
    VIX_MAX            = 40.0
    VIX_MEANREV        = 0.0035      # /step OU pull to VIX_INIT (was 0.0008 — too
                                     # weak; VIX drifted up on down-legs and stayed
                                     # elevated, inflating ATM IV to ~22%)
    VIX_VOL            = 0.014       # %/step pure diffusion (the leverage term now
                                     # supplies most of the spot-driven variance)
    # LEVERAGE EFFECT — IV rises when spot FALLS, bleeds on rallies. corr(spot,VIX)
    # ~ -0.7 in real Nifty; was ZERO (VIX independent of spot). THE defining
    # dynamic of index vol, and the source of vega P&L / the IV-crush trap.
    VIX_LEVERAGE       = 0.075       # vol pts per 1-sigma spot move
    VIX_LEVERAGE_ASYM  = 1.8         # down-moves kick this much harder (asymmetry)
    VIX_EVENT_KICK     = (3.0, 9.0)  # % event-day jump
    SQRT_252           = 15.874
    SQRT_375           = 19.365
    SQRT_60            = 7.746

    # intraday U-shape: (start_min_from_open, end_min, multiplier on per-min sigma)
    USHAPE = [
        (0,   30,  2.00),   # 09:15-09:45 open frenzy
        (30,  105, 1.40),   # 09:45-11:00 decaying
        (105, 165, 1.00),   # 11:00-12:00 baseline
        (165, 255, 0.60),   # 12:00-13:30 lunch trough
        (255, 315, 1.00),   # 13:30-14:30 rising
        (315, 375, 1.55),   # 14:30-15:30 close frenzy
    ]

    # micro-structure (per second)
    STEP_SEC           = 1.0
    MICRO_AR1          = 0.18        # positive AR(1) on 1 s returns
    # /step reversion of slow level to the open-anchored magnet — the PRIMARY
    # daily-range container, CALIBRATED (calibrate_range.py). A TIGHT tether
    # holds a normal day to ~180 high-low; loosening it lets a big/event day's
    # larger legs actually run, so day-character maps to range. (The level is
    # what wanders; tethering it to the open bounds the day.)
    LEVEL_PULL_NORMAL  = 0.110       # normal day  -> ~180 pt median
    LEVEL_PULL_BIG     = 0.030       # big day     -> ~300 pt
    LEVEL_PULL_EVENT   = 0.015       # event day   -> ~430 pt
    # MEANREV_RATE is the PRIMARY range container: spot reverts to the slow
    # level at this per-step rate. An unanchored 22500-step walk ranges ~750
    # pts; this OU pull bounds the stationary std so a normal day prints
    # ~150-220 high-low. CALIBRATED empirically (calibrate_range.py), not
    # guessed — the design draft's 0.18 governor was 4 orders mis-scaled.
    MEANREV_RATE       = 0.0015      # /step spot->level pull (low = smooths
                                     # spot's tracking of the level; range is
                                     # controlled by LEVEL_PULL + leg size,
                                     # not this — see calibration finding)
    MAGNET_OPEN_W      = 0.52        # session-open ANCHOR (does not drift)
    MAGNET_VWAP_W      = 0.13        # session VWAP weight (drifts with price)
    MAGNET_ROUND_W     = 0.10        # nearest round-100 weight
    MAGNET_PAIN_W      = 0.25        # MAX PAIN — couples price to the OI book
                                     # (weights sum to 1.0)
    ROUND_STEP         = 100.0       # pts

    # swing legs — intraday micro-swings, smaller than the WHOLE-DAY move
    # (several 18-40 pt legs compose a ~150-200 pt normal day; legs were the
    # dominant range driver when sized 30-60)
    LEG_IMPULSE_NORM   = (18.0, 40.0)   # pts
    LEG_IMPULSE_BIG    = (45.0, 90.0)   # pts
    LEG_PULLBACK       = (0.40, 0.65)   # fraction retraced
    LEG_DUR_SEC        = (90.0, 360.0)  # s
    BIG_DAY_PROB       = 0.18

    # opening gap
    GAP_PROB           = 0.78
    GAP_SMALL          = (0.0, 50.0)    # pts
    GAP_LARGE          = (50.0, 170.0)  # pts
    GAP_LARGE_PROB     = 0.22

    # event jumps
    JUMP_PROB_PER_STEP = 0.00012
    JUMP_SIZE          = (25.0, 90.0)   # pts
    EVENT_DAY_PROB     = 0.12
    EVENT_DAY_MULT     = 6.0

    # ── REGIME SWITCHING (Markov) ────────────────────────────────────────────
    # Real intraday Nifty is not one process: it alternates between ranging
    # (mean-revert / chop) and trending (one-way institutional flow) stretches,
    # with the occasional volatile burst. The OLD sim only mean-reverted to a
    # fixed open-anchor, so continuation moves could never run — which made the
    # tape structurally punish CE/breakout strategies (entry forensic, 2026-06-13).
    # The regime is a HIDDEN state the engine must DETECT from the tape (ADX,
    # SuperTrend, CVD) — it is NOT revealed to the engine, so this is not circular.
    # Durations (sec) per regime — calibrated so a day holds 2-4 regimes:
    REGIME_DUR = {
        "RANGE":      (1500.0, 4200.0),   # 25-70 min
        "TREND_UP":   (1100.0, 2600.0),   # 18-43 min
        "TREND_DOWN": (1100.0, 2600.0),
        "VOLATILE":   (600.0,  1700.0),   # 10-28 min
    }
    # next-regime weights given the current regime (rows sum ~1). Trends mostly
    # decay back to RANGE; RANGE is the gravity well. Target stylised fact: a
    # clean one-way TREND day ~25-30% of sessions, range/chop ~50%, rest mixed.
    REGIME_TRANS = {
        "RANGE":      {"RANGE": 0.30, "TREND_UP": 0.28, "TREND_DOWN": 0.28, "VOLATILE": 0.14},
        "TREND_UP":   {"RANGE": 0.55, "TREND_UP": 0.12, "TREND_DOWN": 0.13, "VOLATILE": 0.20},
        "TREND_DOWN": {"RANGE": 0.55, "TREND_DOWN": 0.12, "TREND_UP": 0.13, "VOLATILE": 0.20},
        "VOLATILE":   {"RANGE": 0.52, "TREND_UP": 0.21, "TREND_DOWN": 0.21, "VOLATILE": 0.06},
    }
    TREND_SPEED        = (0.045, 0.110)  # pts/sec directional drift in a trend
                                          # (35 min × 0.08 ≈ 168 pt leg)
    # The magnet's "open" component is actually a RATCHETING anchor (fair value).
    # On a range day it stays at the open (mean-reversion, bounded range). On a
    # trending day it migrates toward price — real value follows price when OI
    # repositions — so the trend is NOT reeled back to the open and the day can
    # close net-directional. Without this, meanrev × distance always overpowered
    # the constant trend drift and every day round-tripped (net move ~27pt).
    ANCHOR_PULL_TREND  = 0.0020      # /step anchor->spot pull while trending
                                     # (~8min lag — tracks price so a trend
                                     # consolidates at its new level, not the open)
    ANCHOR_PULL_RANGE  = 0.00004     # /step (≈fixed at open on a range day)
    # per-regime dynamics: ar1 momentum, spot->level meanrev, level_pull mult,
    # sigma mult, and the open-anchor weight (trend dilutes the anchor so the
    # move is not reeled back to the open). Remaining magnet weight redistributes
    # to vwap/round/pain, which DRIFT with price -> trend persists.
    # In a trend the ANCHOR leads (marches at trend speed, see
    # _step_spot_and_futures) and mean-reversion DRAGS spot along behind it, so
    # meanrev is STRONG in a trend (not weak — a weak pull let spot decouple and
    # ignore the marching level). The trend comes entirely from the marching
    # anchor + this pull; there is no separate spot drift term (that double-
    # counted and was swamped by noise anyway).
    REGIME_DYN = {
        "RANGE":      dict(ar1=0.08, meanrev=0.0015, lp=1.00, sig=1.00, open_w=0.52),
        "TREND_UP":   dict(ar1=0.20, meanrev=0.0180, lp=0.85, sig=1.05, open_w=0.16),
        "TREND_DOWN": dict(ar1=0.20, meanrev=0.0180, lp=0.85, sig=1.05, open_w=0.16),
        "VOLATILE":   dict(ar1=0.05, meanrev=0.0020, lp=1.00, sig=1.95, open_w=0.45),
    }
    # big/event days lean toward trend & volatility (more, longer directional legs)
    REGIME_BIGDAY_BIAS = {"TREND_UP": 0.10, "TREND_DOWN": 0.10, "VOLATILE": 0.08}
    # DAY-LEVEL directional character. Real sessions ARE trend-up / trend-down /
    # range days — without this the within-day TREND_UP and TREND_DOWN legs
    # cancel and every day round-trips to its open (measured: net move ~18pt on
    # a 181pt range — unrealistically mean-reverting). On a biased day the
    # aligned trend dominates the transition matrix and the open-anchor is
    # loosened so the move does not get reeled back.
    DAY_BIAS_UP_PROB   = 0.27        # P(trend-up day)
    DAY_BIAS_DN_PROB   = 0.27        # P(trend-down day); rest are range days
    DAY_BIAS_BOOST     = 3.5         # × aligned-trend transition weight
    DAY_BIAS_CUT       = 0.08        # × counter-trend transition weight (a biased
                                     # day almost never trends against itself)
    # On a directional day the magnet must FOLLOW price (consolidate at the new
    # level), not reel back to the open. The ratcheting anchor is the
    # price-following term, so it is BOOSTED; the laggy/anchored terms — session
    # VWAP (cumulative, sits near the open) and max-pain (pinned to the opening
    # OI walls) — are cut. With anchor at full weight earlier, VWAP+pain kept
    # ~55% of the magnet glued to the open and biased days moved no more than
    # range days (both ~35pt). A trend day BLOWS THROUGH max-pain anyway —
    # pinning is a range/expiry-day phenomenon.
    DAY_BIAS_OPEN_MULT = 3.5         # × ratcheting-anchor weight on a biased day
    DAY_BIAS_PAIN_MULT = 0.20        # × max-pain weight on a biased day
    DAY_BIAS_VWAP_MULT = 0.30        # × session-VWAP weight on a biased day

    # ── S/R CAUSALITY (resting-liquidity speed bumps) ────────────────────────
    # Real mechanism: heavy OI / resting orders at a strike resist price crossing
    # it (put writers defend support below, call writers defend resistance above)
    # until momentum overwhelms them, then it breaks and the role flips. The OLD
    # sim had NO such force — price only felt the blended magnet, so the engine's
    # zone bounces/breaks keyed off levels the price ignored. Force is
    # sigma-scaled (the 'explosive ping-pong' lesson: never a raw fractional
    # constant). Calibrate SR_GAIN/SR_BREAK_PTS to a real hold-rate ~60-70%.
    SR_RANGE_PTS       = 28.0        # within this many pts a wall exerts force
    SR_GAIN            = 0.90        # × per-sec sigma at the wall face (prox=str=1).
                                     # A speed bump is a FRACTION of sigma — at the
                                     # wall it cancels roughly one sigma of drift,
                                     # so a strong trend still crosses it (break)
                                     # but chop bounces. NOT 6+ (that = ping-pong).
    SR_WALL_REF        = 6.0e6       # OI units treated as a full-strength wall
    SR_MIN_STRENGTH    = 0.22        # ignore walls weaker than this fraction
    SR_BREAK_PTS       = 12.0        # cross this far past a wall -> broken today

    # daily range governor — day-character-aware (user's lived standard:
    # "300 pts is already a miraculous big day"). A NORMAL day must stay
    # ~150-200 high-low; only big/event days punch toward/past 300.
    RANGE_CAP_NORMAL   = 150.0       # pts from open — soft pull starts here
    RANGE_CAP_BIG      = 300.0
    RANGE_CAP_EVENT    = 480.0
    HARD_RANGE_CAP     = 620.0       # absolute clamp
    RANGE_PULL_GAIN    = 8.0         # multiple of per-sec sigma, applied past
                                     # the soft cap (now correctly sigma-scaled)

    # aggressor / CVD (SEPARATE process)
    AGG_AR1            = 0.30
    AGG_RET_GAIN       = 90.0
    AGG_NOISE          = 0.35
    AGG_CLIP           = 0.92
    FUT_TICKS_PER_STEP = (3, 9)

    # futures OI
    FUT_OI_INIT        = 1.2e7       # units
    FUT_OI_BUILD       = 1200.0      # units/print std
    FUT_OI_UNWIND_PROB = 0.35
    FUT_OI_MIN         = 5e6

    # option-chain OI (UNITS, NSE convention)
    OI_ATM_PEAK        = 2.4e6
    OI_WALL_PEAK       = 1.05e7
    OI_TAPER_STRIKES   = 6.0
    OI_OTM_CENTER      = 3.0
    OI_FAR_FLOOR       = 5.0e4
    OI_ROUND_BOOST_500 = 1.6
    OI_ROUND_BOOST_1000= 2.4
    OI_DRIFT_STD       = 200.0       # units/0.5s (was 900 — audit: 8-30x too large)
    OI_WRITE_BUILD     = 700.0       # writers adding at a defended strike (was 4200)
    OI_WRITE_COVER     = 350.0       # covering on a break (was 2000)
    OI_DECAY           = 0.0012      # /step pull of OI back toward its seeded
                                     # baseline — stops the monotonic unbounded growth
    OI_NUM_STRIKES     = 16
    PCR_TARGET         = (0.85, 1.20)
    PCR_PULL           = 0.015

    # IV surface
    IV_VIX_RATIO       = 1.00
    IV_PUT_SKEW        = 4.0         # vol pts per 1% OTM (puts) — was 1.10, far below
                                     # the real Nifty 25d-put +3-6 vp crash premium
    IV_CALL_SKEW       = 0.12        # vol pts per 1% OTM (calls) — nearly flat; an
                                     # equity index has no upside crash premium
    IV_CURV            = 0.35        # curvature, now in MONEYNESS: vol pts per (%OTM)^2
                                     # (was a strike-COUNT off^2 that blew up the
                                     # call wing and was index-level-dependent)
    IV_TENOR_REF       = 3.5 / 365.0 # skew & curvature steepen as sqrt(REF/T) into
                                     # expiry (sticky-delta gamma/pin smile)
    IV_AR              = 0.12        # per-step pull of persistent IV -> smile (was
                                     # 0.04 — too slow; vega lagged fast spot moves)
    IV_INNOV           = 0.00025     # tiny IV innovation (vol-of-vol), smooth
    IV_EXPIRY_CRUSH    = 0.55

    # bid/ask (rupees)
    TICK               = 0.05
    SPREAD_ATM         = (0.05, 1.00)
    SPREAD_OTM_K       = 0.012       # Rs per pt of |offset|
    SPREAD_PREM_K      = 0.004       # Rs per Rs premium
    SPREAD_VOL_MULT    = (1.0, 4.0)
    SPREAD_MAX         = 8.0

    # sister indices
    BN_LEVEL_MULT      = 2.29
    FN_LEVEL_MULT      = 1.06
    BN_BETA            = 1.25
    FN_BETA            = 1.08
    IDX_NOISE          = 0.00012

    # heavyweights
    HW_BETA            = (0.65, 1.45)
    HW_NOISE           = 0.00035
    HW_PRESSURE_PROB   = 0.04

    # cadence
    OPT_STEP_SEC       = 0.5
    HW_STEP_SEC        = 1.0
    CHAIN_PUSH_SEC     = 30.0


_HW_BASE = {
    "HDFCBANK": 1850, "ICICIBANK": 1420, "RELIANCE": 1530, "INFY": 1880,
    "BHARTIARTL": 1720, "LT": 3950, "ITC": 480, "TCS": 4300, "AXISBANK": 1280,
    "SBIN": 920, "KOTAKBANK": 2150, "M&M": 3300, "BAJFINANCE": 7400,
    "HINDUNILVR": 2600,
}


class RealisticSimFeed:
    """A statistically realistic synthetic Nifty session. Public surface and
    PriceStore writes are byte-for-byte compatible with SimFeed."""

    TICK_SEC = P.STEP_SEC

    def __init__(self, prices, basket):
        self.prices = prices
        self.basket = basket
        self._stop = threading.Event()
        self._thread = None

        # day character (drawn once per session)
        self.big_day = random.random() < P.BIG_DAY_PROB
        self.event_day = random.random() < P.EVENT_DAY_PROB
        self.vix = P.VIX_INIT + (random.uniform(*P.VIX_EVENT_KICK)
                                 if self.event_day else random.uniform(-1.5, 2.0))
        self.vix = min(P.VIX_MAX, max(P.VIX_MIN, self.vix))

        # prior close + opening gap
        self.prior_close = P.SPOT_ANCHOR + random.uniform(-P.SPOT_ANCHOR_JITTER,
                                                          P.SPOT_ANCHOR_JITTER)
        gap = 0.0
        if random.random() < P.GAP_PROB:
            mag = (random.uniform(*P.GAP_LARGE)
                   if random.random() < P.GAP_LARGE_PROB
                   else random.uniform(*P.GAP_SMALL))
            gap = mag * random.choice([1, -1])
        self.session_open = self.prior_close + gap
        self.spot = self.session_open

        # slow level (OU target) + fast micro state
        self.level = self.spot
        self.anchor = self.session_open   # ratcheting "fair value" (see P notes)
        self.last_ret = 0.0
        self.last_price = self.spot

        # session VWAP accumulators (a real magnet)
        self._vwap_pv = self.spot * 1.0
        self._vwap_v = 1.0
        self.vwap = self.spot

        # swing-leg state
        self.leg_dir = random.choice([1, -1])
        self.last_impulse = random.uniform(*(P.LEG_IMPULSE_BIG if self.big_day
                                             else P.LEG_IMPULSE_NORM))
        self.leg_bias = self.leg_dir * self.last_impulse
        self.leg_until = 0.0

        # aggressor (CVD) state — SEPARATE process
        self.agg_imbalance = 0.0

        # day-character soft range cap + matching level tether (a bigger day
        # needs BOTH a higher cap AND a looser tether, or the tether reels the
        # bigger legs back in and every day looks the same)
        if self.event_day:
            self.soft_cap, self.level_pull = P.RANGE_CAP_EVENT, P.LEVEL_PULL_EVENT
        elif self.big_day:
            self.soft_cap, self.level_pull = P.RANGE_CAP_BIG, P.LEVEL_PULL_BIG
        else:
            self.soft_cap, self.level_pull = P.RANGE_CAP_NORMAL, P.LEVEL_PULL_NORMAL

        # day-level directional character: trend-up / trend-down / range day.
        # Without it, within-day up & down trends cancel and the day round-trips.
        r = random.random()
        self.day_bias = (1 if r < P.DAY_BIAS_UP_PROB
                         else -1 if r < P.DAY_BIAS_UP_PROB + P.DAY_BIAS_DN_PROB
                         else 0)

        # REGIME state (Markov) — a day opens RANGE unless a directional/biased
        # or big/event day rolls it straight into a trend/volatile open.
        # regime_until is set on the first step (needs the virtual clock).
        self.regime = "RANGE"
        if self.day_bias > 0 and random.random() < 0.5:
            self.regime = "TREND_UP"
        elif self.day_bias < 0 and random.random() < 0.5:
            self.regime = "TREND_DOWN"
        elif (self.big_day or self.event_day) and random.random() < 0.45:
            self.regime = random.choice(["TREND_UP", "TREND_DOWN", "VOLATILE"])
        self.regime_until = 0.0          # forces a (re)draw on first step
        self.trend_dir = 1 if self.regime == "TREND_UP" else (
            -1 if self.regime == "TREND_DOWN" else 0)
        self.trend_speed = random.uniform(*P.TREND_SPEED)
        self.broken_walls = set()        # (strike, side) broken today

        # futures OI
        self.fut_oi = P.FUT_OI_INIT

        # per-strike option OI / cumulative volume (UNITS) / PERSISTENT IV
        self.oi: Dict[Tuple[float, str], float] = {}
        self.vol_cum: Dict[Tuple[float, str], float] = {}
        self.iv: Dict[Tuple[float, str], float] = {}   # slowly-evolving surface
        self.max_pain = self.spot                      # OI magnet for price

        # heavyweights: symbol -> [price, beta] ; cumulative volumes
        self.hw: Dict[str, list] = {}
        self.hw_volc: Dict[str, float] = {}
        for sym in config.HEAVYWEIGHTS:
            base = _HW_BASE.get(sym, 1000) * random.uniform(0.97, 1.03)
            self.hw[sym] = [base, random.uniform(*P.HW_BETA)]

        # sister indices
        self.idx = {"BANKNIFTY": self.spot * P.BN_LEVEL_MULT,
                    "FINNIFTY":  self.spot * P.FN_LEVEL_MULT}

        self._boot = clk.now()

    # ----- warmup candles (momentum stack hot from second 1) -----------------
    def warmup_candles(self, minutes: int = 30):
        from .flow import Candle
        now = clk.now()
        sig_min = self._sigma_per_min()
        p = self.spot
        path = []
        tail = random.choice([+1.0, -1.0]) * sig_min * 0.5
        for i in range(minutes):
            drift = tail if i < 8 else 0.0
            p -= drift + random.gauss(0, sig_min)
            path.append(p)
        path.reverse()
        candles = []
        prev = path[0]
        for i, close in enumerate(path):
            ts = now - (minutes - i) * 60.0
            wick = abs(random.gauss(0, sig_min * 0.4))
            hi = max(prev, close) + wick
            lo = min(prev, close) - wick
            candles.append(Candle(ts, prev, hi, lo, close,
                                  random.uniform(3e4, 1e5)))
            prev = close
        return candles

    # ----- lifecycle ---------------------------------------------------------
    def start(self):
        self._seed_oi()
        self._seed_basket()
        self.prices.vix = round(self.vix, 2)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="RealisticSimFeed")
        self._thread.start()
        log.info("RealisticSimFeed: spot %.1f open %.1f gap %.1f big=%s event=%s VIX %.1f",
                 self.spot, self.session_open, self.session_open - self.prior_close,
                 self.big_day, self.event_day, self.vix)

    def stop(self):
        self._stop.set()

    # ----- vol schedule ------------------------------------------------------
    def _daily_sigma_frac(self) -> float:
        return (self.vix / 100.0) / P.SQRT_252

    def _sigma_per_min(self) -> float:
        return self.spot * self._daily_sigma_frac() / P.SQRT_375

    def _ushape_mult(self) -> float:
        now = datetime.now(IST)
        mins = (now.hour * 60 + now.minute) - (config.MARKET_OPEN[0] * 60
                                               + config.MARKET_OPEN[1])
        mins = max(0, min(374, mins))
        for lo, hi, mult in P.USHAPE:
            if lo <= mins < hi:
                return mult
        return 1.0

    def _sigma_per_sec(self) -> float:
        return self._sigma_per_min() * self._ushape_mult() / P.SQRT_60

    # ----- seeding -----------------------------------------------------------
    @staticmethod
    def _round_boost(strike: float) -> float:
        if strike % 1000 == 0:
            return P.OI_ROUND_BOOST_1000
        if strike % 500 == 0:
            return P.OI_ROUND_BOOST_500
        return 1.0

    def _seed_oi(self):
        atm = round(self.spot / config.STRIKE_STEP) * config.STRIKE_STEP
        for off in range(-P.OI_NUM_STRIKES, P.OI_NUM_STRIKES + 1):
            k = atm + off * config.STRIKE_STEP
            boost = self._round_boost(k)
            if off >= 0:
                cb = P.OI_ATM_PEAK * math.exp(-((off - P.OI_OTM_CENTER)
                                                / P.OI_TAPER_STRIKES) ** 2)
            else:
                cb = 0.18 * P.OI_ATM_PEAK * math.exp(-(abs(off) / 4.0) ** 2)
            if off <= 0:
                pb = P.OI_ATM_PEAK * math.exp(-((off + P.OI_OTM_CENTER)
                                                / P.OI_TAPER_STRIKES) ** 2)
            else:
                pb = 0.18 * P.OI_ATM_PEAK * math.exp(-(off / 4.0) ** 2)
            cb *= boost
            pb *= boost
            if boost >= P.OI_ROUND_BOOST_1000:
                cb = max(cb, P.OI_WALL_PEAK * (0.6 if off < 0 else 1.0))
                pb = max(pb, P.OI_WALL_PEAK * (0.6 if off > 0 else 1.0))
            # raw-units jitter: deliberately NON-lot-multiple
            self.oi[(k, "call")] = max(P.OI_FAR_FLOOR, cb * random.uniform(0.82, 1.18)
                                       + random.uniform(0, 9999))
            self.oi[(k, "put")] = max(P.OI_FAR_FLOOR, pb * random.uniform(0.82, 1.18)
                                      + random.uniform(0, 9999))
            self.vol_cum[(k, "call")] = random.uniform(2e5, 1.2e6)
            self.vol_cum[(k, "put")] = random.uniform(2e5, 1.2e6)
        self.prices.chain_oi = dict(self.oi)
        self._oi_seed = dict(self.oi)      # baseline for OI mean-reversion (decay)

    def _seed_basket(self):
        for name, lvl in self.idx.items():
            self.prices.idx_ltp[name] = round(lvl, 2)
            self.prices.idx_prev[name] = round(lvl * random.uniform(0.995, 1.005), 2)
            self.prices.idx_ts[name] = clk.mono()
        for sym, (p, _b) in self.hw.items():
            self.basket.set_prev_close(sym, p * random.uniform(0.995, 1.005), p)
            self.hw_volc[sym] = random.uniform(1e5, 8e5)
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

    # ----- main loop ---------------------------------------------------------
    def _run(self):
        last_opt = last_hw = last_chain = 0.0
        while not self._stop.is_set():
            now = clk.now()
            self._step_vix()
            self._step_spot_and_futures(now)
            if now - last_opt >= P.OPT_STEP_SEC:
                self._step_options()
                last_opt = now
            if now - last_hw >= P.HW_STEP_SEC:
                self._step_heavyweights()
                last_hw = now
            if now - last_chain >= P.CHAIN_PUSH_SEC:
                self.prices.chain_oi = dict(self.oi)
                last_chain = now
            clk.sleep(self.TICK_SEC)

    def _step_vix(self):
        pull = P.VIX_MEANREV * (P.VIX_INIT - self.vix)
        # LEVERAGE EFFECT: scale the last realized return by its own per-sec sigma
        # (return in sigmas); a DOWN move lifts VIX (panic put bid), an UP move
        # bleeds it, down asymmetric. This is what gives a PE buyer vega on a
        # sell-off and crushes a panic-bought option on the relief rally.
        sig_frac = max(self._sigma_per_sec() / max(self.spot, 1.0), 1e-9)
        z = self.last_ret / sig_frac
        lev = -P.VIX_LEVERAGE * z * (P.VIX_LEVERAGE_ASYM if self.last_ret < 0 else 1.0)
        self.vix += pull + lev + random.gauss(0, P.VIX_VOL)
        if self.event_day and random.random() < 0.002:
            self.vix += random.uniform(*P.VIX_EVENT_KICK) * 0.4
        self.vix = min(P.VIX_MAX, max(P.VIX_MIN, self.vix))
        self.prices.vix = round(self.vix, 2)

    # ----- regime (Markov) ---------------------------------------------------
    def _step_regime(self, now: float):
        """Advance the hidden regime. Drawn by DURATION; on expiry a new regime
        is chosen from the transition matrix. The engine is never told the
        regime — it must infer it from the tape (ADX/SuperTrend/CVD)."""
        if now < self.regime_until:
            return
        if self.regime_until == 0.0:
            # initial regime already set in __init__ — just give it a duration
            lo, hi = P.REGIME_DUR[self.regime]
            self.regime_until = now + random.uniform(lo, hi)
        else:
            self.regime = self._next_regime()
            lo, hi = P.REGIME_DUR[self.regime]
            self.regime_until = now + random.uniform(lo, hi)
        self.trend_dir = (1 if self.regime == "TREND_UP"
                          else -1 if self.regime == "TREND_DOWN" else 0)
        if self.trend_dir != 0:
            self.trend_speed = random.uniform(*P.TREND_SPEED)
        log.debug("sim regime -> %s (until +%.0fs)", self.regime,
                  self.regime_until - now)

    def _next_regime(self) -> str:
        w = dict(P.REGIME_TRANS[self.regime])
        if self.big_day or self.event_day:
            for k, b in P.REGIME_BIGDAY_BIAS.items():
                w[k] = w.get(k, 0.0) + b
        # a directional day favours its aligned trend and suppresses the counter
        if self.day_bias > 0:
            w["TREND_UP"] = w.get("TREND_UP", 0.0) * P.DAY_BIAS_BOOST
            w["TREND_DOWN"] = w.get("TREND_DOWN", 0.0) * P.DAY_BIAS_CUT
        elif self.day_bias < 0:
            w["TREND_DOWN"] = w.get("TREND_DOWN", 0.0) * P.DAY_BIAS_BOOST
            w["TREND_UP"] = w.get("TREND_UP", 0.0) * P.DAY_BIAS_CUT
        states = list(w)
        return random.choices(states, weights=[w[s] for s in states])[0]

    # ----- S/R causality (resting-liquidity speed bumps) ---------------------
    def _sr_force(self, spot: float, sig: float) -> float:
        """Fractional return from the resting liquidity at the two bracketing
        strikes: the put wall below pushes UP (support), the call wall above
        pushes DOWN (resistance), scaled by wall strength × proximity × sigma.
        A strike decisively crossed flips role automatically (floor/ceil
        reassignment), modelling broken-support-becomes-resistance."""
        step = config.STRIKE_STEP
        k_sup = math.floor(spot / step) * step
        k_res = k_sup + step
        force = 0.0
        for k, sgn, oi_side in ((k_sup, +1.0, "put"), (k_res, -1.0, "call")):
            d = abs(spot - k)
            if d > P.SR_RANGE_PTS:
                continue
            strength = min(1.0, self.oi.get((k, oi_side), 0.0) / P.SR_WALL_REF)
            if strength < P.SR_MIN_STRENGTH:
                continue
            prox = 1.0 - d / P.SR_RANGE_PTS
            force += sgn * P.SR_GAIN * prox * strength * (sig / max(spot, 1.0))
        return force

    def _maybe_new_leg(self, now: float):
        if now < self.leg_until:
            return
        size_rng = P.LEG_IMPULSE_BIG if self.big_day else P.LEG_IMPULSE_NORM
        retracing = abs(self.leg_bias) >= self.last_impulse * 0.9
        if retracing:
            self.leg_dir = -int(math.copysign(1, self.leg_bias or 1))
            pull = P.LEG_PULLBACK
            # on a directional day a COUNTER-trend retrace gives back less, so
            # the swings compound into the net move instead of cancelling it
            if self.day_bias and self.leg_dir != self.day_bias:
                pull = (P.LEG_PULLBACK[0] * 0.4, P.LEG_PULLBACK[1] * 0.5)
            mag = self.last_impulse * random.uniform(*pull)
        else:
            if self.day_bias:
                # a biased day's legs strongly favour the day direction
                self.leg_dir = (self.day_bias if random.random() < 0.78
                                else -self.day_bias)
            else:
                self.leg_dir = random.choice([1, -1]) if random.random() < 0.35 \
                    else int(math.copysign(1, self.leg_bias or 1))
            mag = random.uniform(*size_rng)
            self.last_impulse = mag
        self.leg_bias = self.leg_dir * mag
        self.leg_until = now + random.uniform(*P.LEG_DUR_SEC)

    def _step_spot_and_futures(self, now: float):
        self._step_regime(now)
        self._maybe_new_leg(now)
        sig = self._sigma_per_sec()
        dyn = P.REGIME_DYN[self.regime]

        # MAGNET: in RANGE the session-OPEN anchor dominates (mean-reversion, a
        # bounded day). In a TREND the open weight is diluted and the freed
        # weight is redistributed to vwap/round/pain — all of which DRIFT with
        # price — so the pull no longer reels the move back to the open and a
        # directional leg can actually run (the realism the entry forensic said
        # was missing). Max-pain keeps price coupled to the OI book throughout.
        # the anchor (fair value) LEADS during a trend — it marches at the trend
        # speed so the magnet/level move with the leg instead of reeling it back.
        # Outside a trend it FOLLOWS price (consolidate at the new level on a
        # biased day; stay near the open on a range day).
        if self.trend_dir:
            self.anchor += self.trend_dir * self.trend_speed * P.STEP_SEC
        else:
            anchor_pull = (P.ANCHOR_PULL_TREND if self.day_bias
                           else P.ANCHOR_PULL_RANGE)
            self.anchor += anchor_pull * (self.spot - self.anchor)

        # weighted magnet, renormalised. On a directional day the ANCHORING
        # components (open + max-pain) are down-weighted and the PRICE-FOLLOWING
        # components (vwap + round + the ratcheting anchor) carry the magnet, so
        # a trend isn't reeled back. On a range day weights are the originals.
        nearest_round = round(self.spot / P.ROUND_STEP) * P.ROUND_STEP
        w_open = dyn["open_w"] * (P.DAY_BIAS_OPEN_MULT if self.day_bias else 1.0)
        w_pain = P.MAGNET_PAIN_W * (P.DAY_BIAS_PAIN_MULT if self.day_bias else 1.0)
        w_vwap = P.MAGNET_VWAP_W * (P.DAY_BIAS_VWAP_MULT if self.day_bias else 1.0)
        w_round = P.MAGNET_ROUND_W
        wsum = w_open + w_pain + w_vwap + w_round
        magnet = (w_open * self.anchor + w_pain * self.max_pain
                  + w_vwap * self.vwap + w_round * nearest_round) / wsum
        leg_push = (self.leg_bias / max(1.0, (self.leg_until - now)
                                        if self.leg_until > now else 1.0))
        self.level += (self.level_pull * dyn["lp"] * (magnet - self.level)
                       + leg_push * P.STEP_SEC)

        innov = random.gauss(0, 1.0) * (sig / max(self.spot, 1.0)) * dyn["sig"]
        # the trend is carried by the marching anchor (above) pulling the level,
        # and meanrev dragging spot to it — no separate drift term needed.
        ret = (dyn["ar1"] * self.last_ret
               + dyn["meanrev"] * (self.level - self.spot) / max(self.spot, 1.0)
               + self._sr_force(self.spot, sig)      # resting-liquidity bumps
               + innov)

        hazard = P.JUMP_PROB_PER_STEP * (P.EVENT_DAY_MULT if self.event_day else 1.0)
        if random.random() < hazard:
            jump = random.uniform(*P.JUMP_SIZE) * random.choice([1, -1])
            ret += jump / max(self.spot, 1.0)

        # soft governor: bounds a RANGE day. A trend leg legitimately extends the
        # range, so during a trend the soft cap is raised (a trending normal day
        # can print ~250) — the HARD cap still clamps any runaway. Sigma-scaled
        # (NOT a raw fractional constant — the draft's 0.18 caused ping-pong).
        eff_cap = max(self.soft_cap, P.RANGE_CAP_BIG) if self.trend_dir else self.soft_cap
        dev = self.spot - self.anchor      # deviation from fair value, not the
                                            # fixed open — else the governor reels
                                            # a ratcheted trend back during a
                                            # range regime on a directional day
        if abs(dev) > eff_cap:
            over = (abs(dev) - eff_cap) / eff_cap
            ret -= math.copysign(P.RANGE_PULL_GAIN * over * (sig / max(self.spot, 1.0)),
                                 dev)

        new_spot = self.spot * (1.0 + ret)
        new_spot = max(self.session_open - P.HARD_RANGE_CAP,
                       min(self.session_open + P.HARD_RANGE_CAP, new_spot))

        realized_ret = (new_spot - self.spot) / max(self.spot, 1.0)
        self.last_ret = realized_ret
        self.last_price = self.spot
        self.spot = new_spot
        self.prices._write_spot(round(self.spot, 2))

        step_vol = random.uniform(2e4, 1.2e5) * (1.5 if self._ushape_mult() > 1.3 else 1.0)
        self._vwap_pv += self.spot * step_vol
        self._vwap_v += step_vol
        self.vwap = self._vwap_pv / self._vwap_v

        # SEPARATE aggressor process drives CVD (non-tautology): the imbalance
        # mean is the OBSERVABLE realized return, plus its own noise — NOT the
        # hidden leg direction. So CVD can diverge from price.
        target = math.tanh(P.AGG_RET_GAIN * realized_ret)
        self.agg_imbalance = (P.AGG_AR1 * self.agg_imbalance
                              + (1 - P.AGG_AR1) * target
                              + random.gauss(0, P.AGG_NOISE))
        self.agg_imbalance = max(-P.AGG_CLIP, min(P.AGG_CLIP, self.agg_imbalance))
        p_buyer = 0.5 + 0.5 * self.agg_imbalance

        fut_mid = self.spot + P.BASIS
        n_prints = random.randint(*P.FUT_TICKS_PER_STEP)
        for _ in range(n_prints):
            qty = random.choice([25, 50, 75, 150, 300, 600, 1200])
            half = max(P.TICK, random.uniform(0.1, 0.5))
            bid = round(fut_mid - half, 2)
            ask = round(fut_mid + half, 2)
            if random.random() < p_buyer:
                ltp = ask                  # buyer-initiated (uptick) -> CVD +qty
            else:
                ltp = bid                  # seller-initiated (downtick) -> CVD -qty
            ltp = round(ltp + random.gauss(0, 0.15), 2)

            building = random.random() > P.FUT_OI_UNWIND_PROB
            d_oi = abs(random.gauss(0, P.FUT_OI_BUILD)) * (1 if building else -1.6)
            self.fut_oi = max(P.FUT_OI_MIN, self.fut_oi + d_oi)
            self.prices._write_futures(ltp, qty, bid, ask, round(self.fut_oi, 0))

        base_q = random.uniform(5e3, 4e4)
        self.prices.fut_bqty = round(base_q * (1.0 + 0.8 * max(0.0, self.agg_imbalance)), 0)
        self.prices.fut_aqty = round(base_q * (1.0 + 0.8 * max(0.0, -self.agg_imbalance)), 0)

    # ----- options -----------------------------------------------------------
    def _iv_for(self, k: float, atm: float, money: float, T: float) -> float:
        atm_iv = (self.vix / 100.0) * P.IV_VIX_RATIO
        crush = 1.0
        if config.is_expiry_day():
            now = datetime.now(IST)
            mins_to_close = (config.MARKET_CLOSE[0] * 60 + config.MARKET_CLOSE[1]) \
                - (now.hour * 60 + now.minute)
            if mins_to_close < 180:
                crush = P.IV_EXPIRY_CRUSH + (1 - P.IV_EXPIRY_CRUSH) \
                    * max(0.0, mins_to_close) / 180.0
        atm_iv *= crush
        pct_otm = abs(money) * 100.0                      # % out-of-the-money
        # skew & curvature STEEPEN into expiry (sticky-delta): a fixed %OTM is many
        # more sigmas near expiry, so the smile turns sharply convex (gamma/pin).
        tenor = min(2.5, math.sqrt(P.IV_TENOR_REF / max(T, 1.0 / (365.0 * 144))))
        if money < 0:                                     # PUT wing — steep
            skew = P.IV_PUT_SKEW * pct_otm if k < atm else 0.0
            curv = P.IV_CURV * (pct_otm ** 2)
        else:                                             # CALL wing — nearly flat
            skew = P.IV_CALL_SKEW * pct_otm if k > atm else 0.0
            curv = 0.3 * P.IV_CURV * (pct_otm ** 2)       # muted (no upside crash)
        # NO per-call random noise — this is the SMOOTH target of the surface;
        # evolution toward it (and the leverage-driven VIX) happens in _step_options.
        return max(0.05, atm_iv + (skew + curv) * tenor / 100.0)

    def _spread_for(self, px: float, off: float) -> float:
        # spread is a fraction of PREMIUM plus a small absolute floor — NEVER a
        # function of strike DISTANCE (the old point-distance term drove far-OTM
        # spreads to 200-2000% of px and pushed bids NEGATIVE). Wider in high vol,
        # but capped at ~25% of premium so the bid stays positive.
        base = random.uniform(*P.SPREAD_ATM)
        s = base + P.SPREAD_PREM_K * px
        widen = 1.0 + (self.vix - P.VIX_INIT) / 20.0
        widen = max(P.SPREAD_VOL_MULT[0], min(P.SPREAD_VOL_MULT[1], widen))
        s *= widen
        s = min(s, max(P.TICK, 0.25 * px))
        s = max(P.TICK, min(P.SPREAD_MAX, round(s / P.TICK) * P.TICK))
        return s

    def _step_options(self):
        atm = round(self.spot / config.STRIKE_STEP) * config.STRIKE_STEP
        T = greeks.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))
        # floor T LOW only on expiry day so 0DTE gamma/theta can actually bloom
        # (the flat 16.8h floor froze expiry-day gamma the whole session)
        T = max(T, (600.0 / (365.0 * 24 * 3600)) if config.is_expiry_day()
                else 0.7 / 365.0)

        strikes = {atm + off * config.STRIKE_STEP
                   for off in range(-config.NUM_STRIKES, config.NUM_STRIKES + 1)}
        strikes.update(k for k, _r in self.prices.opt_ltp.keys())
        strikes.update(k for k, _r in self.oi.keys())

        tot_ce = sum(v for (k, r), v in self.oi.items() if r == "call") or 1.0
        tot_pe = sum(v for (k, r), v in self.oi.items() if r == "put")
        pcr = tot_pe / tot_ce
        if pcr < P.PCR_TARGET[0]:
            pcr_push = +1
        elif pcr > P.PCR_TARGET[1]:
            pcr_push = -1
        else:
            pcr_push = 0

        up = self.last_ret > 0

        for k in sorted(strikes):
            off = (k - atm) / config.STRIKE_STEP
            money = (k - self.spot) / self.spot
            for right in ("call", "put"):
                key = (k, right)
                # PERSISTENT IV: evolve the stored IV slowly toward the smooth
                # smile target (AR), so premium = pure BS(current spot, smooth
                # IV) and TRACKS spot tick-by-tick via delta. Tiny innovation
                # only. (Was: fresh gauss every tick -> premium looked random.)
                target_iv = self._iv_for(k, atm, money, T)
                prev_iv = self.iv.get(key, target_iv)
                iv = prev_iv + P.IV_AR * (target_iv - prev_iv) \
                    + random.gauss(0, P.IV_INNOV)
                iv = max(0.05, iv)
                self.iv[key] = iv
                px = float(greeks.bs_price(self.spot, k, T, iv, right))
                # quote noise is at most one tick — premium must follow spot,
                # not wander on its own
                px = max(0.05, round(px / P.TICK) * P.TICK)
                spread = self._spread_for(px, off)

                key = (k, right)
                oi = self.oi.get(key, P.OI_ATM_PEAK * 0.4)
                drift = random.gauss(0, P.OI_DRIFT_STD)
                if up:
                    if right == "put" and k <= atm:
                        drift += P.OI_WRITE_BUILD
                    if right == "call" and k >= atm:
                        drift -= P.OI_WRITE_COVER
                else:
                    if right == "call" and k >= atm:
                        drift += P.OI_WRITE_BUILD
                    if right == "put" and k <= atm:
                        drift -= P.OI_WRITE_COVER
                if pcr_push and ((pcr_push > 0 and right == "put")
                                 or (pcr_push < 0 and right == "call")):
                    drift += P.PCR_PULL * oi * 0.02
                if abs(off) > 6:
                    drift *= 0.4
                # mean-revert toward the seeded baseline so OI breathes instead of
                # growing monotonically forever (audit: was unbounded)
                base = getattr(self, "_oi_seed", {}).get(key, oi)
                drift += P.OI_DECAY * (base - oi)
                self.oi[key] = max(P.OI_FAR_FLOOR, oi + drift)
                self.vol_cum[key] = self.vol_cum.get(key, 2e5) \
                    + abs(random.gauss(0, 1.4e4)) * (3.0 if abs(off) <= 1 else 1.0)

                bqty = random.uniform(2e3, 1.5e4)
                aqty = random.uniform(2e3, 1.5e4)
                aligned = ((up and right == "call") or (not up and right == "put"))
                if aligned and abs(off) <= 2 and random.random() < 0.25:
                    bqty *= random.uniform(2.5, 5.0)
                self.prices._write_option(
                    k, right, round(px, 2), round(self.oi[key], 0),
                    round(self.vol_cum[key], 0),
                    round(px - spread / 2, 2), round(px + spread / 2, 2),
                    round(bqty, 0), round(aqty, 0))

        # MAX PAIN: strike minimising total option-buyer payout — the OI
        # magnet that pulls spot toward the book's equilibrium each step
        self.max_pain = self._compute_max_pain(atm)

    def _compute_max_pain(self, atm: float) -> float:
        ks = [atm + i * config.STRIKE_STEP for i in range(-12, 13)]
        best_k, best_pain = atm, float("inf")
        for expire_at in ks:
            pain = 0.0
            for k in ks:
                ce = self.oi.get((k, "call"), 0.0)
                pe = self.oi.get((k, "put"), 0.0)
                pain += max(expire_at - k, 0.0) * ce + max(k - expire_at, 0.0) * pe
            if pain < best_pain:
                best_pain, best_k = pain, expire_at
        return best_k

    # ----- heavyweights + sister indices -------------------------------------
    def _step_heavyweights(self):
        idx_ret = self.last_ret
        for name, beta in (("BANKNIFTY", P.BN_BETA), ("FINNIFTY", P.FN_BETA)):
            lvl = self.idx[name]
            ret = idx_ret * beta + random.gauss(0, P.IDX_NOISE)
            self.idx[name] = max(1000.0, lvl * (1 + ret))
            self.prices.idx_ltp[name] = round(self.idx[name], 2)
            self.prices.idx_ts[name] = clk.mono()
        for sym, state in self.hw.items():
            p, beta = state
            ret = idx_ret * beta + random.gauss(0, P.HW_NOISE)
            state[0] = max(1.0, p * (1 + ret))
            self.basket.on_tick(sym, round(state[0], 2))
            self.hw_volc[sym] = self.hw_volc.get(sym, 1e5) \
                + abs(random.gauss(0, 2500)) * (4.0 if random.random() < 0.03 else 1.0)
            bq = random.uniform(2e3, 2e4)
            aq = random.uniform(2e3, 2e4)
            if random.random() < P.HW_PRESSURE_PROB:
                if random.random() < 0.5:
                    bq *= random.uniform(2.5, 5)
                else:
                    aq *= random.uniform(2.5, 5)
            self.prices._write_hw(sym, round(state[0], 2),
                                  round(self.hw_volc[sym], 0),
                                  round(bq, 0), round(aq, 0))

# Backwards-compatible alias — app.py and replay.py import SimFeed
SimFeed = RealisticSimFeed

