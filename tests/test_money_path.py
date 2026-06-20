"""
MYTHOS — money-path test suite.

Pins the parts where a silent bug would misstate P&L or break the risk
contract: Black-Scholes/IV math, position sizing, the exit ladder (hard SL,
breakeven guard, hold-first, fill model), and cooldown asymmetry.

Runs with bare `python tests/test_money_path.py` (no pytest needed) AND as a
pytest suite if pytest is later installed (functions are named test_*).

Every test states the INVARIANT it protects — these are the promises the
system makes about your money. If one ever fails, a promise broke.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mythos import config, greeks
from mythos.trader import PaperTrader, Trade


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles — a minimal PriceStore the trader can drive
# ─────────────────────────────────────────────────────────────────────────────
class FakePrices:
    def __init__(self, spot=25000.0):
        self.spot = spot
        self.futures = spot + 18
        self._opt = {}            # (strike, right) -> ltp
        self.opt_bid = {}
        self.opt_ask = {}
        self.opt_bqty = {}
        self.opt_aqty = {}

    def set(self, strike, right, ltp, spread=1.0):
        self._opt[(strike, right)] = ltp
        self.opt_bid[(strike, right)] = ltp - spread / 2
        self.opt_ask[(strike, right)] = ltp + spread / 2

    def option_price(self, strike, right):
        return self._opt.get((strike, right), 0.0)

    def option_age(self, strike, right):
        return 1.0                # always fresh

    def freeze_core(self):
        atm = round(self.spot / config.STRIKE_STEP) * config.STRIKE_STEP
        return (self.spot, self.futures, atm,
                self._opt.get((atm, "call"), 0.0),
                self._opt.get((atm, "put"), 0.0))


class FakeOI:
    support_zones = []
    resistance_zones = []


def _trade(entry=100.0, qty=130, direction="CE", strike=25000.0):
    """A Trade past its blind-hold window, ready for exit testing. qty=130 (2
    lots) reflects the risk-based sizing the trader now uses — a single such
    trade's worst stop is ~1-2% of capital, well under the daily equity stop, so
    the exit-ladder logic under test is not pre-empted by the portfolio breaker."""
    return Trade(
        id=1, direction=direction, strike=strike,
        right="call" if direction == "CE" else "put",
        lots=qty // config.LOT_SIZE, qty=qty,
        entry_price=entry, entry_time="10:00:00",
        entry_epoch=time.monotonic() - 120.0,   # blind hold long passed
        stop_loss=entry - config.SL_POINTS, target=entry + config.TARGET_POINTS,
        entry_score=0.8, peak_price=entry, hold_escaped=True)


def _new_trader():
    t = PaperTrader(FakePrices(), store=None)
    t.bypass_time = True          # skip EOD/market-hours gating in tests
    # ISOLATE from any persisted day-state: PaperTrader loads trades_today.json on
    # init, so a prior live/sim/test session's depleted capital would leak in and
    # (now that the daily equity stop reads capital) pre-empt the exit-ladder logic
    # under test. Force a clean fresh day and stop tests from writing the day file.
    t.capital = config.STARTING_CAPITAL
    t.open = []
    t.closed = []
    t.consec_sl = {"CE": 0, "PE": 0}
    t._last_exit_pnl = 0.0
    t._save_today = lambda: None
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Greeks / IV math
# ─────────────────────────────────────────────────────────────────────────────
def test_put_call_parity():
    """INVARIANT: C - P == S - K*e^(-rT). If broken, every premium is wrong."""
    S, K, T, iv = 25000.0, 25000.0, 4 / 365, 0.13
    c = float(greeks.bs_price(S, K, T, iv, "call"))
    p = float(greeks.bs_price(S, K, T, iv, "put"))
    rhs = S - K * math.exp(-config.RISK_FREE_RATE * T)
    assert abs((c - p) - rhs) < 0.01, f"parity broke: {c-p:.3f} vs {rhs:.3f}"


def test_iv_roundtrip():
    """INVARIANT: IV solved from a BS price recovers the input vol."""
    S, K, T = 25000.0, 25100.0, 4 / 365
    for true_iv in (0.10, 0.15, 0.22, 0.30):
        px = float(greeks.bs_price(S, K, T, true_iv, "call"))
        solved = float(greeks.implied_vol(np.array([px]), S, np.array([K]), T, "call")[0])
        assert abs(solved - true_iv) < 1e-3, f"IV roundtrip {solved} != {true_iv}"


def test_greek_signs():
    """INVARIANT: long-option theta < 0 (decay), vega > 0, |delta| in (0,1)."""
    g = greeks.single_greeks(150.0, 25000.0, 25000.0, 4 / 365, "call")
    assert g is not None
    assert g.theta < 0, "long call theta must be negative"
    assert g.vega > 0, "vega must be positive"
    assert 0.0 < g.delta < 1.0, "call delta in (0,1)"
    gp = greeks.single_greeks(150.0, 25000.0, 25000.0, 4 / 365, "put")
    assert -1.0 < gp.delta < 0.0, "put delta in (-1,0)"


def test_single_greeks_deep_itm_returns_bounded_delta():
    """DELTA/GREEKS FIX: a DEEP-ITM option (premium ≈ intrinsic) makes the IV solve
    fail — old code returned None and select_strike SKIPPED the highest-conviction
    MOVER. Now it must return a BOUNDED |delta| near 1.0, never None."""
    T = 5 / 365
    # deep-ITM call: S 24200, K 23800 (intrinsic 400), premium just inside the
    # IV-solve cushion (intrinsic + 0.01) → solver fails → fallback delta path
    g = greeks.single_greeks(400.01, 24200.0, 23800.0, T, "call")
    assert g is not None, "a deep-ITM mover must NOT be NaN-skipped"
    assert 0.90 <= g.delta <= 1.0, f"deep-ITM call delta must be ~1.0, got {g.delta}"
    gp = greeks.single_greeks(400.01, 23800.0, 24200.0, T, "put")
    assert gp is not None and -1.0 <= gp.delta <= -0.90, "deep-ITM put delta ~ -1.0"


def test_single_greeks_otm_junk_still_returns_none():
    """REGRESSION: an OTM quote with an unsolvable IV and NO intrinsic (a junk/
    crossed book) must still return None — the fix must not award a fake delta to
    genuine garbage."""
    T = 5 / 365
    # OTM call (K>S, intrinsic 0), premium below the solve cushion → unsolvable
    assert greeks.single_greeks(0.03, 24200.0, 24600.0, T, "call") is None


def test_iv_rejects_below_intrinsic():
    """INVARIANT: a premium below intrinsic returns nan, not a bogus vol."""
    # call deep ITM: S=25000 K=24000 intrinsic≈1000; price 500 is impossible
    iv = float(greeks.implied_vol(np.array([500.0]), 25000.0, np.array([24000.0]),
                                  4 / 365, "call")[0])
    assert math.isnan(iv), "sub-intrinsic premium must yield nan"


# ─────────────────────────────────────────────────────────────────────────────
# Sizing & P&L
# ─────────────────────────────────────────────────────────────────────────────
def test_position_sizing():
    """INVARIANT (Requirement.txt §7.3): lots = floor(capital / (premium *
    lot_size)) — FULL capital deployed every trade. With cheaper-entry the
    'premium' is the LIMIT we actually buy at (≤ LTP), so full capital is sized
    on the real fill price, not the LTP."""
    tr = _new_trader()
    tr.prices.set(25000.0, "call", 100.0)
    tr.capital = 100_000.0
    from mythos.signals import Decision, ZoneView
    d = Decision(direction="CE", allowed=True)
    d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
    t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
    assert t is not None, "entry should succeed"
    assert t.pending, "cheaper-entry rests a limit before filling"
    expected = int(tr.capital // (t.limit_price * config.LOT_SIZE))
    assert t.lots == expected, f"lots {t.lots} != {expected} (full capital on limit)"
    assert t.qty == expected * config.LOT_SIZE


def test_cheaper_entry_rests_below_ltp():
    """INVARIANT (cheaper-entry mandate): try_enter does NOT fill at market — it
    rests a BUY LIMIT below the LTP for a margin of safety."""
    tr = _new_trader()
    tr.prices.set(25000.0, "call", 100.0)
    from mythos.signals import Decision, ZoneView
    d = Decision(direction="CE", allowed=True)
    d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
    t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
    off = max(config.ENTRY_LIMIT_MIN_OFFSET, config.ENTRY_LIMIT_OFFSET_PTS,
              100.0 * config.ENTRY_LIMIT_OFFSET_FRAC)
    assert t.pending and t.limit_price == round(100.0 - off, 2)
    assert t.limit_price < 100.0, "limit must be below LTP"
    assert tr.closed == [], "a resting order is not a closed trade"


def test_pending_fills_only_when_price_comes_down():
    """INVARIANT: the resting limit fills (clock + stops start) only once the
    option trades down to it — never by chasing a rising price."""
    tr = _new_trader()
    tr.prices.set(25000.0, "call", 100.0)
    from mythos.signals import Decision, ZoneView
    d = Decision(direction="CE", allowed=True)
    d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
    t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
    limit = t.limit_price
    # price RISES — must NOT fill (no chasing)
    tr.prices.set(25000.0, "call", 105.0)
    tr.check_exits(live_score=0.8)
    assert t.pending, "must not fill on a rising price"
    # price comes down to the limit — now it fills
    tr.prices.set(25000.0, "call", limit)
    tr.check_exits(live_score=0.8)
    assert not t.pending, "should fill once price reaches the limit"
    assert t.entry_price <= limit, "fill no worse than the limit"
    assert t.stop_loss == round(t.entry_price - config.SL_POINTS, 2)
    assert t in tr.open and tr.closed == [], "fill keeps it open, not closed"


def test_pending_takes_market_after_window():
    """INVARIANT (user mandate): a working limit that doesn't get its cheaper
    fill is TAKEN AT MARKET once the window elapses — the trade is ALWAYS taken,
    NEVER lapsed. 'You have a conviction to enter, enter it; don't miss the move.'"""
    tr = _new_trader()
    tr.prices.set(25000.0, "call", 100.0)
    from mythos.signals import Decision, ZoneView
    d = Decision(direction="CE", allowed=True)
    d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
    t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
    assert t.pending, "starts as a working order"
    # price never dips; force the window to elapse → must MARKET fill, not lapse
    t.limit_epoch = time.monotonic() - (config.ENTRY_LIMIT_WAIT_SEC + 1.0)
    tr.prices.set(25000.0, "call", 101.0)        # the market is here now
    out = tr.check_exits(live_score=0.8)
    assert not t.pending, "must fill at market after the window — never lapse"
    assert t in tr.open and t not in tr.closed, "a fill keeps it open, not closed"
    assert out == [], "a fill is not an exit"
    assert t.entry_price == 101.0, "filled at the current market LTP"
    assert t.stop_loss == round(101.0 - config.SL_POINTS, 2)


def test_pending_never_quoted_lapses_and_frees_slot():
    """AUDIT FIX: a working order whose strike NEVER quotes must not hold the
    single slot forever (it would silently block all entries). After 3× the work
    window it LAPSES — removed from open, NOT recorded as a trade, capital intact."""
    tr = _new_trader()
    tr.prices.set(25000.0, "call", 100.0)        # quotes at order time only
    from mythos.signals import Decision, ZoneView
    d = Decision(direction="CE", allowed=True)
    d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
    cap0 = tr.capital
    t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
    assert t.pending and t in tr.open
    # the strike goes silent (no quote) and stays silent past 3× the window
    tr.prices._opt[(25000.0, "call")] = 0.0
    t.limit_epoch = time.monotonic() - (config.ENTRY_LIMIT_WAIT_SEC * 3 + 1.0)
    out = tr.check_exits(live_score=0.8)
    assert t not in tr.open, "never-quoted order must be lapsed to free the slot"
    assert t not in tr.closed and out == [], "a lapse is NOT a recorded trade"
    assert tr.capital == cap0, "a lapse must not touch capital"


def test_pnl_cash_math():
    """INVARIANT: pnl_cash == pnl_pts * qty; capital += pnl_cash exactly."""
    tr = _new_trader()
    t = _trade(entry=100.0, qty=975)
    tr.open.append(t)
    cap0 = tr.capital
    tr.prices.set(t.strike, "call", 130.0)   # well above any stop
    # force a clean close at market via EOD-style path
    tr._close(t, 130.0, "TEST")
    assert t.pnl_pts == 30.0
    assert t.pnl_cash == 30.0 * 975
    assert abs(tr.capital - (cap0 + t.pnl_cash)) < 1e-6


def test_gap_through_earned_lock_is_not_a_doctrine_breach():
    """INSTRUMENTATION: a trade that EARNED its +12 lock (peak >= TRAIL_ACTIVATE)
    and then gaps straight DOWN through the floor exits in the dead zone
    INVOLUNTARILY — the market never traded at +12 on the way down. That is a
    GAP-THROUGH, not a doctrine breach (the doctrine bans voluntary scratches).
    This is the false alarm that previously mislabeled 5 replay exits as breaches."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    t.peak_price = 100.0 + config.TRAIL_ACTIVATE + 2.0   # peak +16: the lock was earned
    tr.open.append(t)
    # gap to entry+0.4 — >5 pts below the entry+12 floor, so the fill model books
    # the gapped price (a genuine gap-through), landing in the dead zone.
    tr._close(t, 100.4, "TRAIL SL", stop_level=112.0)
    assert -10.0 < t.pnl_pts < 10.0, "precondition: exit lands in the dead zone"
    assert tr._gap_throughs == 1, "earned-lock gap-through must be counted as such"
    assert tr._doctrine_breaches == 0, "a gap-through is NOT a doctrine breach"


def test_dead_zone_exit_without_earned_lock_is_a_real_breach():
    """REGRESSION GUARD: a sub-+12 exit on a trade that NEVER reached the lock
    (peak < TRAIL_ACTIVATE) is a genuine doctrine breach and MUST still fire. The
    binary exit has no code path that voluntarily banks in the dead zone — if this
    count ever goes nonzero in live/replay, the exit ladder has regressed."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    t.peak_price = 103.0                      # peak only +3: the lock was never earned
    tr.open.append(t)
    tr._close(t, 100.4, "TRAIL SL")           # dead-zone exit with no lock behind it
    assert -10.0 < t.pnl_pts < 10.0
    assert tr._doctrine_breaches == 1, "an un-earned dead-zone exit is a real breach"
    assert tr._gap_throughs == 0


def test_safety_exit_in_dead_zone_is_neither_breach_nor_gap():
    """A STALE QUOTE / EOD safety close is allowed at any pnl — never a breach,
    never a gap-through."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    tr.open.append(t)
    tr._close(t, 100.4, "STALE QUOTE")
    assert tr._safety_exits == 1
    assert tr._doctrine_breaches == 0 and tr._gap_throughs == 0


# ─────────────────────────────────────────────────────────────────────────────
# Exit ladder — the risk contract
# ─────────────────────────────────────────────────────────────────────────────
def test_hard_stop_never_worse_than_minus_11():
    """INVARIANT: a stop-out fills no worse than -10 stop minus 1 slippage,
    when the breach is within the 5-pt gap tolerance."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 89.5)    # below stop (90), 0.5 breach
    closed = tr.check_exits(live_score=0.8)
    assert closed and closed[0].status == "CLOSED"
    # stop_level=90, fill=max(89.5, 90-1)=89.5 -> pnl -10.5
    assert t.pnl_pts >= -11.0, f"stop fill {t.pnl_pts} worse than -11"
    assert t.exit_reason == "SL HIT"


def test_no_profit_floor_below_trail():
    """INVARIANT (profit rule v4 — BINARY): below the +14 trail there is NO profit
    floor. A +12 peak that fully reverses rides to the -10 stop. No small-profit
    banking; outcomes are -10 or +12."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 112.0)   # peak +12, below the +14 trail
    tr.check_exits(live_score=0.8)
    assert not t.trail_active, "trail must NOT arm below +14"
    assert t.stop_loss == t.entry_price - config.SL_POINTS, "no floor below the trail"
    tr.prices.set(t.strike, "call", 89.5)    # full reverse, below -10
    closed = tr.check_exits(live_score=0.8)
    assert closed and t.exit_reason == "SL HIT"
    assert t.pnl_pts <= -10.0, f"a non-runner must take the -10 ({t.pnl_pts})"


def test_trail_secures_min_12_once_armed():
    """INVARIANT (profit rule v4): once peak >= +14 the trail arms and SECURES the
    +12 minimum — a reversal then exits no worse than ~+12, never a scrap."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 114.5)   # peak +14.5 -> trail arms, floor +12
    tr.check_exits(live_score=0.8)
    assert t.trail_active, "trail must arm at +14"
    assert t.trail_sl >= t.entry_price + config.TRAIL_INITIAL_LOCK - 0.01
    tr.prices.set(t.strike, "call", 111.0)   # reverse toward the floor
    closed = tr.check_exits(live_score=0.8)
    assert closed, "should exit on the +12 trail"
    assert t.pnl_pts >= 10.0, f"secured only {t.pnl_pts} (expected ~+12)"


def test_trail_arms_only_at_plus14():
    """INVARIANT: the trail/chandelier engages only once peak >= +14 (the +12
    floor); below it the trade is held with the -10 stop as the only exit."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 113.0)   # +13, below +14
    tr.check_exits(live_score=0.8)
    assert not t.trail_active, "trail must NOT arm at +13"
    tr.prices.set(t.strike, "call", 115.0)   # +15
    tr.check_exits(live_score=0.8)
    assert t.trail_active, "trail MUST arm at +15"
    assert t.trail_sl >= t.entry_price + config.TRAIL_INITIAL_LOCK - 0.01


def test_no_exit_in_blind_hold_except_stop():
    """INVARIANT: in the first 30 s, only the hard stop may exit."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    t.entry_epoch = time.monotonic()         # brand-new: inside blind hold
    t.hold_escaped = False
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 115.0)   # big profit — must NOT exit yet
    assert not tr.check_exits(live_score=0.8), "no profit exit during blind hold"
    assert t.status == "OPEN"


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown asymmetry
# ─────────────────────────────────────────────────────────────────────────────
def test_cooldown_asymmetry():
    """INVARIANT: 90 s pause after a WIN, 15 s after a LOSS (chasing a winner's
    spent momentum is the toxic pattern; re-attempt after a loss is fine)."""
    tr = _new_trader()
    tr._last_exit_epoch = time.monotonic()
    tr._last_exit_pnl = 12.0                  # a win
    assert tr.cooldown_remaining() > 60.0, "win cooldown should be ~90 s"
    tr._last_exit_pnl = -10.0                 # a loss
    assert tr.cooldown_remaining() <= 15.0, "loss cooldown should be <= 15 s"


# ─────────────────────────────────────────────────────────────────────────────
# BINARY EXIT DOCTRINE — no scratches (user, 2026-06-14)
# ─────────────────────────────────────────────────────────────────────────────
def _drive(tr, t, path):
    """Push a premium path through check_exits; return the closed trade or None."""
    for px in path:
        tr.prices.set(t.strike, "call", px)
        closed = tr.check_exits(live_score=0.8)
        if closed:
            return closed[0]
    return None


def test_binary_exit_invariant():
    """THE user's core rule: every NON-SAFETY exit lands at pnl ≤ −9 or ≥ +10 —
    NEVER a scratch in (−10,+12). Stop-outs, trail wins, and non-runners that
    reverse from various peaks all obey it."""
    paths = [
        [95, 90, 89.5],                       # straight to the −10 stop
        [100, 112, 116, 130, 118],            # peak +30, reverse → trail ≥ +12
        [100, 113.9, 100, 90, 89.5],          # peak +13.9 (never armed) → rides to −10
        [105, 108, 102, 95, 89.0],            # wander then stop
    ]
    for path in paths:
        tr = _new_trader()
        t = _trade(entry=100.0)
        tr.open.append(t)
        c = _drive(tr, t, path)
        assert c is not None, f"path {path} never exited"
        assert c.exit_reason in ("SL HIT", "TRAIL SL"), f"bad reason {c.exit_reason}"
        assert c.pnl_pts <= -9.0 or c.pnl_pts >= 10.0, \
            f"SCRATCH {c.exit_reason} {c.pnl_pts:+.1f} in (−10,+12) for {path}"
        assert tr._doctrine_breaches == 0, "doctrine-breach counter must stay 0"


def test_non_runner_rides_no_scratch():
    """With STALL KILL + TIMEOUT deleted, a flat non-runner held WELL past the old
    900 s cap stays OPEN — it must ride to −10 or +12, never be scratched."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    t.entry_epoch = time.monotonic() - 5000.0       # way past every old time cap
    t.last_peak_epoch = time.monotonic() - 5000.0
    tr.open.append(t)
    tr.prices.set(t.strike, "call", 102.0)          # flat, inside (−10,+12)
    for _ in range(6):
        assert not tr.check_exits(live_score=0.8), "a non-runner must NOT be scratched"
    assert t.status == "OPEN"


def test_no_stall_or_timeout_reason():
    """The STALL KILL / TIMEOUT reason strings can never be produced."""
    tr = _new_trader()
    t = _trade(entry=100.0)
    t.entry_epoch = time.monotonic() - 5000.0
    tr.open.append(t)
    c = _drive(tr, t, [101, 102, 101, 95, 90, 89.0])   # long wander, then stop
    assert c is not None and c.exit_reason == "SL HIT"
    assert c.exit_reason not in ("STALL KILL", "TIMEOUT")


# ─────────────────────────────────────────────────────────────────────────────
# ATM-default strike selection
# ─────────────────────────────────────────────────────────────────────────────
def test_atm_default_strike():
    """LEGACY (OFF) path: ATM by default (delta ≈0.50); OTM (≈0.45) only when
    conviction is very strong; the legacy no-arg caller gets ATM. (Live default is
    now the ATM/ITM-forced mode — this pins the still-supported OFF behavior.)"""
    saved = config.STRIKE_FORCE_ATM_OR_ITM
    try:
        config.STRIKE_FORCE_ATM_OR_ITM = False
        tr = _new_trader()
        tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI(), conviction=0.0)
        assert abs(tr._last_strike_target - config.STRIKE_TARGET_DELTA_ATM) < 1e-9
        tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI(),
                         conviction=config.STRIKE_OTM_CONVICTION + 0.05)
        assert abs(tr._last_strike_target - config.STRIKE_TARGET_DELTA_STRONG) < 1e-9
        tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI())   # default arg → ATM
        assert abs(tr._last_strike_target - config.STRIKE_TARGET_DELTA_ATM) < 1e-9
    finally:
        config.STRIKE_FORCE_ATM_OR_ITM = saved


def _chain(prices, spot, right, iv=0.13, T=4 / 365, strikes=None):
    """Quote a realistic BS option chain so single_greeks recovers a true delta."""
    if strikes is None:
        atm = round(spot / config.STRIKE_STEP) * config.STRIKE_STEP
        strikes = [atm + i * config.STRIKE_STEP for i in range(-3, 4)]
    for k in strikes:
        px = float(greeks.bs_price(spot, k, T, iv, right))
        prices.set(k, right, px, spread=1.0)


def test_force_atm_or_itm_picks_a_mover():
    """USER MANDATE (2026-06-17): with the ATM/ITM gate ON, every pick is a strike
    that MOVES — achieved |delta| >= 0.50 and the strike sits at or inside the
    money. Conviction no longer pushes the pick OTM. Flag OFF stays the legacy
    path (covered by test_atm_default_strike); we restore it here."""
    saved = config.STRIKE_FORCE_ATM_OR_ITM
    saved_band = config.STRIKE_DELTA_BAND_ATM_ITM
    try:
        config.STRIKE_FORCE_ATM_OR_ITM = True
        for direction, right in (("CE", "call"), ("PE", "put")):
            tr = _new_trader()
            _chain(tr.prices, 25000.0, right)
            # even at MAX conviction the gate must NOT step OTM
            k, r = tr.select_strike(direction, 25000.0, 25000.0, 0.0, FakeOI(),
                                    conviction=0.95)
            assert r == right
            assert tr._last_strike_achieved >= 0.50, \
                f"{direction}: chose Δ{tr._last_strike_achieved} (< 0.50 OTM)"
            inside = (k <= 25000.0) if direction == "CE" else (k >= 25000.0)
            assert inside, f"{direction}: strike {k} is OTM, not at/inside the money"

        # HARD FLOOR: if the scan band only offers an OTM strike, the floor steps
        # one strike toward the money and takes it when it quotes.
        config.STRIKE_DELTA_BAND_ATM_ITM = (1, 2)   # CE: only the +1 (OTM) offset
        tr = _new_trader()
        _chain(tr.prices, 25000.0, "call")
        k, _ = tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI(), conviction=0.0)
        assert k <= 25000.0 and tr._last_strike_achieved >= 0.50, \
            f"floor failed: chose {k} Δ{tr._last_strike_achieved}"
    finally:
        config.STRIKE_FORCE_ATM_OR_ITM = saved
        config.STRIKE_DELTA_BAND_ATM_ITM = saved_band


def test_force_skips_entry_when_only_otm_quotes():
    """ON-mode mandate (thin-book net): if the book is so thin that no at/inside-
    money strike quotes, the walk keeps the best OTM available but try_enter must
    REFUSE to send the order (Δ below the floor) — never buy a non-mover. On a
    liquid chain the same setup enters normally. OFF-mode is unaffected."""
    from mythos.signals import Decision, ZoneView
    saved = config.STRIKE_FORCE_ATM_OR_ITM
    try:
        config.STRIKE_FORCE_ATM_OR_ITM = True
        # thin PUT book: only two OTM strikes quote, nothing at/inside the money
        tr = _new_trader()
        tr.prices.spot = 23658.0
        tr.prices.futures = 23658.0
        tr.prices.set(23600.0, "put", 101.1)
        tr.prices.set(23650.0, "put", 122.2)        # best available is still Δ<0.50
        d = Decision(direction="PE", allowed=True)
        d.pe = ZoneView(zone_level=23650.0, kind="BREAK")
        assert tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI()) is None, \
            "must skip when only OTM quotes"
        assert tr.open == [] and tr.closed == [], "a skip is not a trade"

        # liquid chain: at/inside-money strikes quote → enters normally
        tr2 = _new_trader()
        tr2.prices.spot = 23658.0
        tr2.prices.futures = 23658.0
        _chain(tr2.prices, 23658.0, "put")
        d2 = Decision(direction="PE", allowed=True)
        d2.pe = ZoneView(zone_level=23650.0, kind="BREAK")
        t = tr2.try_enter(d2, expected_move=0.0, oi_engine=FakeOI())
        assert t is not None and t.strike_delta_achieved >= 0.50, \
            "a liquid chain must enter with an ATM/ITM mover"
    finally:
        config.STRIKE_FORCE_ATM_OR_ITM = saved


def test_force_off_is_unchanged_on_a_chain():
    """BYTE-IDENTICAL OFF: with the flag forced OFF, the legacy branch reproduces
    the legacy target (ATM 0.50 at low conviction, 0.45 at high) — the ATM/ITM
    branch is fully inert. (The OFF path must stay available even though live now
    ships ON.)"""
    saved = config.STRIKE_FORCE_ATM_OR_ITM
    try:
        config.STRIKE_FORCE_ATM_OR_ITM = False
        tr = _new_trader()
        _chain(tr.prices, 25000.0, "call")
        tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI(), conviction=0.0)
        assert abs(tr._last_strike_target - config.STRIKE_TARGET_DELTA_ATM) < 1e-9
        tr.select_strike("CE", 25000.0, 25000.0, 0.0, FakeOI(),
                         conviction=config.STRIKE_OTM_CONVICTION + 0.05)
        assert abs(tr._last_strike_target - config.STRIKE_TARGET_DELTA_STRONG) < 1e-9
    finally:
        config.STRIKE_FORCE_ATM_OR_ITM = saved


# ─────────────────────────────────────────────────────────────────────────────
# Cross-instrument LEAD (task #37) — BankNifty leads, basket confirms
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_lead_vote_grades_and_degrades():
    """_lead_vote: +1 when BankNifty strongly LEADS our way (move exceeds Nifty's
    own) with the basket confirming; -1 when it strongly opposes; 0 when the flag
    is off OR data is stale/absent (a dead poller must never force/block a trade)."""
    from collections import deque
    from mythos import clk
    from mythos.signals import SignalEngine

    def _engine(bn_now, bn_past, nif_now, nif_past, sent):
        eng = SignalEngine.__new__(SignalEngine)
        now, mono = clk.now(), clk.mono()

        class _P:
            pass
        eng.prices = _P()
        eng.prices.idx_ltp = {"BANKNIFTY": bn_now}
        eng.prices.idx_ts = {"BANKNIFTY": mono}
        eng._idx_hist = {"BANKNIFTY": deque([(now - 40, bn_past), (now, bn_now)])}
        eng._spot_window = deque([(now - 40, nif_past), (now, nif_now)])

        class _B:
            sentiment = sent
        eng.basket = _B()
        return eng

    saved = config.CROSS_LEAD_ON
    try:
        config.CROSS_LEAD_ON = True
        # BankNifty +0.20% vs Nifty +0.02%, basket bullish → CE agree (+1), PE opp (-1)
        eng = _engine(57600.0, 57485.0, 25005.0, 25000.0, 65.0)
        assert eng._lead_vote("CE") == 1, "strong bull lead must agree for CE"
        assert eng._lead_vote("PE") == -1, "a bull lead must oppose a PE fire"
        # mirror: BankNifty -0.20%, basket bearish → PE agree, CE opp
        eng = _engine(57400.0, 57515.0, 24995.0, 25000.0, 35.0)
        assert eng._lead_vote("PE") == 1
        assert eng._lead_vote("CE") == -1
        # weak move (under threshold) → neutral both sides
        eng = _engine(57505.0, 57500.0, 25001.0, 25000.0, 65.0)
        assert eng._lead_vote("CE") == 0 and eng._lead_vote("PE") == 0
        # stale BankNifty (idx_ts old) → degrade to neutral
        eng = _engine(57600.0, 57485.0, 25005.0, 25000.0, 65.0)
        eng.prices.idx_ts["BANKNIFTY"] = clk.mono() - 120.0
        assert eng._lead_vote("CE") == 0, "a stale poller must not lead"
        # flag OFF → always neutral (byte-identical baseline)
        config.CROSS_LEAD_ON = False
        eng = _engine(57600.0, 57485.0, 25005.0, 25000.0, 65.0)
        assert eng._lead_vote("CE") == 0
    finally:
        config.CROSS_LEAD_ON = saved


def test_vis_inflection_fires_and_is_silent_on_knife():
    """VELOCITY-INFLECTION SNAP (task #38) — the smart single decision. It must
    FIRE when spot AND our premium 2nd-derivatives turn up together, freshly, and
    be SILENT on a falling knife (the exact bar=0 failure mode), on an ATM-roll
    cold premium, and it must mirror correctly for PE."""
    from mythos.flow import Kinematics
    from mythos.signals import SignalEngine, PremiumVelocity

    def _eng(ce_path, pe_path=None):
        e = SignalEngine.__new__(SignalEngine)
        e.kin = {"spot": Kinematics(), "ce": Kinematics(), "pe": Kinematics()}
        e.prem = PremiumVelocity()
        pe_path = pe_path or [50.0] * len(ce_path)
        for c, p in zip(ce_path, pe_path):
            e.prem.update(c, p)
        return e

    A = config.VIS_SPOT_A_MIN
    # CE FIRE: rising CE premium + spot up-inflection (v<=0, a>=floor, j>=0)
    e = _eng([100, 101, 102, 103, 104, 106])
    e.kin["spot"].v, e.kin["spot"].a, e.kin["spot"].j = -1.5, A + 0.02, 0.01
    e.kin["ce"].a = 0.4
    assert e._vis_inflection("CE") is True, "must fire on a real dual up-inflection"
    # KNIFE (spot still accelerating DOWN): silent
    e.kin["spot"].v, e.kin["spot"].a, e.kin["spot"].j = -4.0, -(A + 0.02), -0.02
    assert e._vis_inflection("CE") is False, "must NOT catch a falling knife"
    # KNIFE (premium still bleeding): spot ok but CE premium marked down hard
    e2 = _eng([110, 108, 106, 104, 102, 100])
    e2.kin["spot"].v, e2.kin["spot"].a, e2.kin["spot"].j = -1.0, A + 0.02, 0.01
    e2.kin["ce"].a = -0.3
    assert e2._vis_inflection("CE") is False, "premium must agree (not still bleeding)"
    # COLD premium (post ATM-roll): too few samples -> silent even on a real turn
    e3 = _eng([100, 101])
    e3.kin["spot"].v, e3.kin["spot"].a, e3.kin["spot"].j = -1.0, A + 0.02, 0.01
    e3.kin["ce"].a = 0.4
    assert e3._vis_inflection("CE") is False, "cold premium after ATM roll must suppress"
    # PE mirror FIRE: spot down-inflection + rising PE premium
    e4 = _eng([50.0] * 6, [100, 101, 102, 103, 104, 106])
    e4.kin["spot"].v, e4.kin["spot"].a, e4.kin["spot"].j = +1.5, -(A + 0.02), -0.01
    e4.kin["pe"].a = 0.4
    assert e4._vis_inflection("PE") is True, "PE side must mirror"


def test_battle_lines_finds_defended_floor_and_resisted_ceiling():
    """OPTION BATTLE LINES: a premium that twice dips to ~100 and bounces with
    serious buying prints a DEFENDED FLOOR; a premium twice rejected at ~130 with
    serious selling prints a RESISTED CEILING — both battle-tested (strength≥2)."""
    from mythos.levels import _Track
    tr = _Track(config.BATTLE_REBOUND_FRAC, config.BATTLE_BAND_FRAC,
                config.BATTLE_REBOUND_PTS, config.BATTLE_BAND_PTS, True)
    path = [120, 115, 108, 101, 100, 108, 118, 128, 130, 124,
            116, 109, 101, 100, 109, 120, 129, 131, 125, 118]
    for i, p in enumerate(path):
        if p <= 105:                      # at the lows: bids swamp offers (buyers)
            bq, aq = 900, 100
        elif p >= 128:                    # at the highs: offers swamp bids (sellers)
            bq, aq = 100, 900
        else:
            bq, aq = 300, 300
        tr.update(p, bq, aq, float(i))
    floor = tr._best(tr.floors, True, 110.0)
    ceil = tr._best(tr.ceils, False, 120.0)
    assert floor is not None and abs(floor.price - 100.0) <= 3 and floor.strength >= 2, \
        "must find the buyer-defended floor near 100"
    assert floor.serious >= 1, "the floor must be flow-confirmed (serious buying)"
    assert ceil is not None and abs(ceil.price - 130.0) <= 3 and ceil.strength >= 2, \
        "must find the seller-resisted ceiling near 130"
    assert ceil.serious >= 1, "the ceiling must be flow-confirmed (serious selling)"


def test_knife_guard_holds_collapsing_fill_takes_dip_and_timeout():
    """Cheap-entry fix: a 'cheap' limit fill that is a KNIFE (our-side premium in
    free-fall AND the live thesis turned against the side) is HELD — no position.
    A shallow cheap dip with the thesis intact is TAKEN. The window-timeout market
    take ALWAYS fills (don't miss the move), and SL=fill−10 / TGT=fill+12 hold."""
    from mythos.signals import Decision, ZoneView
    saved = config.STRIKE_FORCE_ATM_OR_ITM
    try:
        config.STRIKE_FORCE_ATM_OR_ITM = False     # isolate the knife guard
        config.KNIFE_GUARD_ON = True

        def _pending(ce=100.0):
            tr = _new_trader()
            tr.prices.set(25000.0, "call", ce)
            d = Decision(direction="CE", allowed=True)
            d.ce = ZoneView(zone_level=25000.0, kind="BOUNCE")
            t = tr.try_enter(d, expected_move=0.0, oi_engine=FakeOI())
            assert t is not None and t.pending
            return tr, t

        # (a) cheap dip + thesis INTACT (live CE, high score, mild velocity) → FILLS
        tr, t = _pending()
        tr.prices.set(25000.0, "call", t.limit_price)
        tr.check_exits(live_score=0.8, prem_vel=-0.1, live_dir="CE")
        assert not t.pending and t in tr.open, "shallow cheap dip, thesis intact → fill"

        # (b) cheap dip that is a KNIFE (steep −ve velocity + direction flipped) → HELD
        tr, t = _pending()
        tr.prices.set(25000.0, "call", t.limit_price)
        tr.check_exits(live_score=0.2, prem_vel=-1.2, live_dir="PE")
        assert t.pending and tr.closed == [], "a knife cheap fill must be HELD (no position)"

        # (c) same knife context but the WINDOW elapsed → market take ALWAYS fills
        t.limit_epoch = time.monotonic() - (config.ENTRY_LIMIT_WAIT_SEC + 1.0)
        tr.prices.set(25000.0, "call", 96.0)
        tr.check_exits(live_score=0.2, prem_vel=-1.2, live_dir="PE")
        assert not t.pending and t in tr.open, "window-timeout takes the trade (don't miss the move)"
        assert t.stop_loss == round(t.entry_price - config.SL_POINTS, 2), "SL still entry−10"
        assert t.target == round(t.entry_price + config.TARGET_POINTS, 2), "TGT still entry+12"

        # (d) cold velocity (post ATM-roll, prem_vel 0.0) → fails safe to FILL
        tr, t = _pending()
        tr.prices.set(25000.0, "call", t.limit_price)
        tr.check_exits(live_score=0.2, prem_vel=0.0, live_dir="PE")
        assert not t.pending, "cold velocity (0.0) must fail safe to TAKE"
    finally:
        config.STRIKE_FORCE_ATM_OR_ITM = saved


def test_consensus_core_fuses_and_abstains():
    """consensus_core (task #39): a confident one-sided board nets |C|>=MIN at low
    contested and permits only that side; a both-sides-lit board is contested and
    permits neither; an all-abstain board nets C=0 (no phantom vote)."""
    import consensus_core as cc
    bull = {"FLOW": {"vote": 0.8, "conf": 0.9}, "TREND": {"vote": 0.6, "conf": 0.8},
            "BREADTH": {"vote": 0.7, "conf": 0.7}, "STRUCTURE": {"vote": 0.2, "conf": 0.4}}
    cb = cc.fuse(bull)
    assert cb["C"] > cc.CONSENSUS_MIN and cb["contested"] < cc.CONTESTED_MAX
    assert cc.gate_pass("CE", cb) and not cc.gate_pass("PE", cb)
    split = {"FLOW": {"vote": 0.85, "conf": 0.9}, "TREND": {"vote": -0.85, "conf": 0.9},
             "BREADTH": {"vote": 0.0, "conf": 0.0}, "STRUCTURE": {"vote": 0.0, "conf": 0.0}}
    cs = cc.fuse(split)
    assert cs["contested"] >= cc.CONTESTED_MAX, "a both-sides-lit board is contested"
    assert not cc.gate_pass("CE", cs) and not cc.gate_pass("PE", cs)
    empty = {k: {"vote": 0.0, "conf": 0.0} for k in cc.PANEL_WEIGHTS}
    assert cc.fuse(empty)["C"] == 0.0, "abstaining panels must not net a phantom vote"


def test_consensus_gate_demotes_against_tape_only_when_confident():
    """The money-path semantics: the bloc vetoes a side ONLY when it leans
    confidently AGAINST it on a non-split board; a contested or weak board abstains
    (don't suppress on a quiet/split tape). Flag OFF → fully inert."""
    from mythos.signals import SignalEngine
    e = SignalEngine.__new__(SignalEngine)
    saved = config.CONSENSUS_GATE_ON
    try:
        config.CONSENSUS_GATE_ON = True
        bear = {"C": -0.6, "contested": 0.2, "_cmin": 0.30, "_cmax": 0.55}
        assert e._consensus_blocks("CE", bear) and not e._consensus_blocks("PE", bear)
        bull = {"C": +0.6, "contested": 0.2, "_cmin": 0.30, "_cmax": 0.55}
        assert e._consensus_blocks("PE", bull) and not e._consensus_blocks("CE", bull)
        contested = {"C": -0.6, "contested": 0.9, "_cmin": 0.30, "_cmax": 0.55}
        assert not e._consensus_blocks("CE", contested), "split board abstains"
        weak = {"C": -0.10, "contested": 0.2, "_cmin": 0.30, "_cmax": 0.55}
        assert not e._consensus_blocks("CE", weak), "weak board abstains"
        # flag OFF → _consensus returns None (no import, no compute = byte-identical)
        config.CONSENSUS_GATE_ON = False
        assert e._consensus(25000.0, 25000.0) is None
    finally:
        config.CONSENSUS_GATE_ON = saved


def test_commentary_routine_budget_and_critical_bypass():
    """Declutter: with priority ON, routine chatter is capped per rolling window
    but CRITICAL tells (seller-exhaustion, fall-warning) ALWAYS chime. With
    priority OFF, every line fires exactly as before (byte-identical)."""
    from collections import deque
    from mythos.commentary import Commentary

    def _fresh():
        c = Commentary.__new__(Commentary)
        c.items = deque(maxlen=60)
        c._last_fired = {}
        c._routine_fires = deque()
        fired = []
        c.on_alert = fired.append
        return c, fired

    # all-routine kinds (none in the MEDIUM/CRITICAL tiers)
    routine = ["book_call", "book_put", "vol_surge_call",
               "vol_surge_put", "liquidity", "gex"]
    saved = config.COMMENT_PRIORITY_ON
    saved_so = config.COMMENT_SIGNAL_ONLY
    try:
        config.COMMENT_SIGNAL_ONLY = False        # this test exercises the budget layer
        config.COMMENT_PRIORITY_ON = True
        c, fired = _fresh()
        for k in routine:
            c._fire(k, f"{k} chatter")
        assert len(fired) == config.COMMENT_ROUTINE_MAX_PER_WIN, \
            "low-value chatter must be capped to the rolling-window budget"
        # MEDIUM tell (PCR/CVD/max-pain) bypasses the budget — never crowded out
        c._fire("max_pain", "MAX PAIN SHIFTED 100pts")
        assert "MAX PAIN SHIFTED 100pts" in fired, "a MEDIUM tell bypasses the budget"
        # CRITICAL tells always fire
        c._fire("reversal_fuel", "SELLERS ARE RUNNING AWAY")
        assert "SELLERS ARE RUNNING AWAY" in fired, \
            "a CRITICAL tell must never be rate-capped away"
        c._fire("distribution", "MARKET ROLLING OVER")
        assert "MARKET ROLLING OVER" in fired, "the fall-warning is CRITICAL"

        # OFF → no cap, all routine lines fire
        config.COMMENT_PRIORITY_ON = False
        c, fired = _fresh()
        for k in routine:
            c._fire(k, f"{k} chatter")
        assert len(fired) == len(routine), "priority OFF must be byte-identical"

        # SIGNAL-ONLY: only CRITICAL directional alerts fire; all else silenced
        config.COMMENT_SIGNAL_ONLY = True
        c, fired = _fresh()
        for k in routine:
            c._fire(k, f"{k} chatter")
        c._fire("max_pain", "MAX PAIN 100")              # MEDIUM is also silenced
        assert fired == [], "SIGNAL-ONLY silences all non-critical commentary"
        c._fire("distribution", "DISTRIBUTION FORMING — fall 82 RISING")
        c._fire("reversal_fuel", "SELLERS ARE RUNNING AWAY")
        assert len(fired) == 2, "SIGNAL-ONLY still shows the VERY bullish/bearish tells"
    finally:
        config.COMMENT_PRIORITY_ON = saved
        config.COMMENT_SIGNAL_ONLY = saved_so


# ─────────────────────────────────────────────────────────────────────────────
# Closed adaptive loop — TrustBook (bounded, min-sample, doctrine-clean)
# ─────────────────────────────────────────────────────────────────────────────
def test_trust_min_sample_and_bounded_and_relative():
    from mythos.learning import TrustBook
    p = os.path.join(os.environ.get("TEMP", "/tmp"), "mythos_trust_test.json")
    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)
    tb = TrustBook(p)
    for _ in range(config.ADAPT_MIN_SAMPLES - 1):
        tb.update("CE", 25000.0, 0.0, 100.0)            # losses, but below min samples
    assert tb.trust_gate("CE", 25000.0) == 0, "no bump below min samples"
    for _ in range(6):
        tb.update("PE", 24000.0, 1.0, 100.0)            # a WINNING context lifts the book
        tb.update("CE", 25000.0, 0.0, 100.0)            # the failing context
    assert tb.trust_gate("CE", 25000.0) in (1, 2), "a below-book failing context bumps"
    assert tb.trust_gate("PE", 24000.0) == 0, "a winning (above-book) context never bumps"
    for c in tb.ctx.values():
        assert 0.0 <= c["ema"] <= 1.0, "ema bounded to [0,1]"
    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)


def test_book_brake_throttles_a_losing_book():
    """The fix for the 74-trade wipeout: when the WHOLE book is a proven loser
    (every zone equally bad → relative bump gives nothing), an ABSOLUTE brake
    raises the bar book-wide; it stays 0 until enough samples, and RELEASES when
    wins lift the book back up."""
    from mythos.learning import TrustBook
    p = os.path.join(os.environ.get("TEMP", "/tmp"), "mythos_brake.json")
    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)
    tb = TrustBook(p)
    # a handful of losses — below ADAPT_BOOK_MIN_N → no brake yet (anti-noise)
    for i in range(config.ADAPT_BOOK_MIN_N - 1):
        tb.update("CE", 23000.0 + i * 50, 0.0, 100.0)
    assert tb.book_brake() == 0, "no brake below the minimum sample count"
    # keep losing past the threshold → book trust collapses → brake engages
    for i in range(20):
        tb.update("CE", 23000.0 + (i % 5) * 50, 0.0, 100.0)
    assert tb.global_ema < config.ADAPT_BOOK_FLOOR, "a losing book drops below the floor"
    assert tb.book_brake() >= config.ADAPT_BOOK_BRAKE, "losing book → brake engages"
    # now a streak of wins lifts the book back above the floor → brake releases
    for _ in range(30):
        tb.update("PE", 24000.0, 1.0, 100.0)
    assert tb.book_brake() == 0, "brake releases once the book recovers"
    for f in (p, p + ".tmp"):
        if os.path.exists(f):
            os.remove(f)


def test_trust_reward_doctrine_clean():
    """The crux anti-overfit guarantee: an HONEST non-runner −10 (thesis intact)
    teaches NOTHING; only +12 (1.0) and a −10 WITH a broken thesis (0.0) update."""
    from types import SimpleNamespace
    from mythos.learning import TrustBook
    tb = TrustBook(os.path.join(os.environ.get("TEMP", "/tmp"), "mythos_trust_r.json"))
    R = tb._reward
    assert R(SimpleNamespace(exit_reason="SL HIT", pnl_pts=-10.0), "CLEAN_LOSS") is None
    assert R(SimpleNamespace(exit_reason="SL HIT", pnl_pts=-10.0), "HELD_LOSER") == 0.0
    assert R(SimpleNamespace(exit_reason="TRAIL SL", pnl_pts=12.5), "CLEAN_WIN") == 1.0
    assert R(SimpleNamespace(exit_reason="TRAIL SL", pnl_pts=12.0), "BOOKED_EARLY") == 1.0
    assert R(SimpleNamespace(exit_reason="STALE QUOTE", pnl_pts=3.0), "SAFETY_EXIT") is None


def test_path_mfe_running_max():
    """The post-exit watch captures the TRUE path max — a +30 spike that fades to
    +5 by the window end is still graded BOOKED_EARLY (single-read would miss it)."""
    from mythos.learning import LearningLoop, MistakeJournal

    class ScriptPrices:
        def __init__(self, seq):
            self.seq, self.i = list(seq), 0

        def option_age(self, k, r):
            return 1.0

        def option_price(self, k, r):
            v = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return v

    base = os.path.join(os.environ.get("TEMP", "/tmp"), "mythos_pathmfe")
    for f in (base + ".json", base + ".json.tmp", base + ".trust", base + ".trust.tmp"):
        if os.path.exists(f):
            os.remove(f)
    ll = LearningLoop(MistakeJournal(base + ".json"))
    ll.trust.path = base + ".trust"          # isolate from the live adaptive state
    ll.trust.ctx, ll.trust.global_ema = {}, 0.5
    t = _trade(entry=100.0)
    t.exit_price, t.pnl_pts = 100.0, 13.0    # a win, exited at 100
    now = time.monotonic()
    ll.on_exit(t, {"factors": [{"name": "x", "ok": True}]}, 24990.0, now)
    prices = ScriptPrices([110, 120, 130, 122, 108, 105, 105, 105])  # spikes +30 then fades
    # multi-horizon watch: a PROVISIONAL line fires at ~60s, the FINAL graded entry
    # at ~POSTEXIT_WATCH_SEC_LONG (~300s). Tick across the full long horizon and
    # grade on the final (mistake_class-bearing) entry.
    fin = []
    step = config.POSTEXIT_WATCH_SEC / 6.0       # ~10s ticks
    s = 1
    while now + s * step <= now + config.POSTEXIT_WATCH_SEC_LONG + 2 * step:
        fin += ll.tick(now + s * step, prices)
        s += 1
    finals = [e for e, _ in fin if "mistake_class" in e]
    assert finals, "the long-horizon watch should finalize a graded entry"
    entry = finals[-1]
    assert entry["mfe_after"] >= 29.0, f"path max ~+30 expected, got {entry['mfe_after']}"
    assert entry["mistake_class"] == "BOOKED_EARLY"
    for f in (base + ".json", base + ".json.tmp", base + ".trust", base + ".trust.tmp"):
        if os.path.exists(f):
            os.remove(f)


# ─────────────────────────────────────────────────────────────────────────────
# Real-time price path — prices must NEVER freeze on the dashboard (Requirement §3)
# ─────────────────────────────────────────────────────────────────────────────
def test_option_feed_liveness_flags_a_stale_leg():
    """feed_alive() watches only spot/futures, so the OPTION feed could stall with
    spot still ticking — CE/PE prices frozen, no warning (the 06-15 failure mode).
    atm_option_age() is the missing probe: it reports the WORSE of the two ATM legs
    and only counts as alive when BOTH are fresh."""
    from mythos.feed import PriceStore
    from mythos import clk
    p = PriceStore()
    p.spot = 25000.0
    atm = round(25000.0 / config.STRIKE_STEP) * config.STRIKE_STEP
    now = clk.mono()
    p.opt_ltp[(atm, "call")] = 120.0; p.opt_ts[(atm, "call")] = now
    p.opt_ltp[(atm, "put")] = 110.0;  p.opt_ts[(atm, "put")] = now - 60.0  # stalled
    assert p.atm_option_age() >= 59.0          # reports the worse (older) leg
    assert not p.option_feed_alive(45.0)       # one stale leg → not alive
    p.opt_ts[(atm, "put")] = now               # heal it
    assert p.atm_option_age() < 2.0
    assert p.option_feed_alive(45.0)
    p.spot = 0.0                               # no spot → unknown, never "alive"
    assert p.atm_option_age() > 1e8
    assert not p.option_feed_alive()


def test_nan_never_reaches_the_wire():
    """A NaN/Inf must never be serialized: as bare `NaN` it is invalid JSON that
    wedges the client's JSON.parse and silently freezes the whole dashboard. _f
    coerces non-finite to 0.0, and _safe_dumps refuses any frame that still holds
    one (skipping a single push) rather than emitting poison."""
    from mythos.state import _f
    from mythos.server import _safe_dumps
    assert _f(float("nan")) == 0.0
    assert _f(float("inf")) == 0.0
    assert _f(-float("inf")) == 0.0
    assert _f(1.234) == 1.23
    assert _safe_dumps({"x": float("nan")}) is None     # poison frame → skipped
    assert _safe_dumps({"x": 1.5}) == '{"x": 1.5}'      # clean frame → sent


def test_price_frame_is_tagged_and_price_only():
    """The fast push path sends a tiny kind='price' frame (market+health only) so
    prices refresh every ~200ms without waiting for the heavy build."""
    from mythos.feed import PriceStore
    from mythos.state import price_frame
    from mythos.server import _safe_dumps
    from mythos import clk

    class _App:
        def __init__(self, p): self.prices = p
    p = PriceStore()
    p.spot = 25011.5; p.futures = 25030.0; p.spot_ts = clk.mono()
    atm = round(p.spot / config.STRIKE_STEP) * config.STRIKE_STEP
    p.opt_ltp[(atm, "call")] = 121.25; p.opt_ts[(atm, "call")] = clk.mono()
    p.opt_ltp[(atm, "put")] = 108.75;  p.opt_ts[(atm, "put")] = clk.mono()
    fr = price_frame(_App(p))
    assert fr["kind"] == "price"
    assert fr["market"]["spot"] == 25011.5 and fr["market"]["ce_ltp"] == 121.25
    assert "opt_age" in fr["health"] and "spot_age" in fr["health"]
    assert "position" not in fr and "chain" not in fr   # price-only, not the tree
    assert _safe_dumps(fr) is not None                  # always serializable


def test_oi_divergence_survives_concurrent_poller_mutation():
    """oi_divergence runs in the ENTRY path while the chain poller inserts strikes
    into _tracks on another thread. Reading the live dict there violated the
    module's own snapshot contract; this pins that a writer churning the dict
    (forcing resizes) can never crash a trade evaluation."""
    import threading
    from mythos.oi_engine import OIEngine
    from mythos import clk
    e = OIEngine()
    atm = 25000.0
    now = clk.now()
    for i in range(70):
        e.note_spot(24950.0 + i * 1.0, now - 200 + i * 3)

    stop = threading.Event()
    errors = []

    def poller():
        n = 0
        while not stop.is_set():
            # insert NEW keys (forces dict growth/rehash) + update existing
            k = atm + ((n % 40) - 20) * config.STRIKE_STEP
            try:
                e.update_strike(k, "call", 50000.0 + n, now)
                e.update_strike(k, "put", 50000.0 + n, now)
            except Exception as ex:                       # writer must never throw
                errors.append(("writer", repr(ex)))
            n += 1

    t = threading.Thread(target=poller)
    t.start()
    try:
        for _ in range(4000):                             # hammer the reader
            try:
                e.oi_divergence("CE", 25030.0)
                e.oi_divergence("PE", 24970.0)
            except Exception as ex:                       # the bug would surface here
                errors.append(("reader", repr(ex)))
                break
    finally:
        stop.set()
        t.join(timeout=5)
    assert not errors, f"race crash: {errors[:3]}"


def test_kinematics_identical_at_1hz_and_no_longer_drops_fast_ticks():
    """The v→a→j chain feeds LIVE entry votes (Physics aligned, pcr_ok, Premium
    exploding/force-intact), so the fix must be a PROVABLE no-op at the engine's
    1 Hz cadence. Pin two things:
      (a) at ≥0.25s spacing the new code equals the old prev_s formula exactly;
      (b) a sub-0.25s tick now folds into the level instead of vanishing."""
    from mythos.flow import Kinematics

    # reference = the OLD algorithm (early-return BEFORE folding on dt<0.25)
    def old_update(st, x, now, alpha=0.30):
        if st["s"] is None:
            st["s"] = x; st["ts"] = now; return
        dt = max(now - st["ts"], 1e-3)
        if dt < 0.25:
            return
        prev_s, prev_v, prev_a = st["s"], st["v"], st["a"]
        st["s"] += alpha * (x - st["s"])
        v_raw = (st["s"] - prev_s) / dt
        st["v"] += alpha * (v_raw - st["v"])
        a_raw = (st["v"] - prev_v) / dt
        st["a"] += alpha * (a_raw - st["a"])
        j_raw = (st["a"] - prev_a) / dt
        st["j"] += alpha * (j_raw - st["j"])
        st["ts"] = now

    # (a) identical at 1 Hz on a non-trivial path
    k = Kinematics()
    ref = {"s": None, "ts": 0.0, "v": 0.0, "a": 0.0, "j": 0.0}
    path = [25000, 25004, 25011, 25009, 25020, 25035, 25030, 25048, 25060, 25055]
    for i, x in enumerate(path):
        t = 100.0 + i * 1.0
        k.update(float(x), t)
        old_update(ref, float(x), t)
    assert abs(k.v - ref["v"]) < 1e-12
    assert abs(k.a - ref["a"]) < 1e-12
    assert abs(k.j - ref["j"]) < 1e-12

    # (b) a sub-0.25s tick is no longer discarded — it moves the smoothed level
    k2 = Kinematics()
    k2.update(100.0, 100.0)        # init (non-zero ts: update() treats 0.0 falsy)
    k2.update(100.0, 101.0)        # establishes _s near 100, _ts=101.0
    s_before = k2._s
    k2.update(140.0, 101.1)        # dt=0.1 < 0.25: no differentiation...
    assert k2._s > s_before        # ...but the level DID absorb the jump
    assert k2._ts == 101.0         # and the derivative clock did NOT advance


def test_sacred_constants_pinned():
    """The +12/-10 binary exit and lot=65 are sacred and must NEVER drift. Pin
    them explicitly (the teardown noted tests used these via config indirection
    but never asserted the actual values)."""
    assert config.LOT_SIZE == 65
    assert config.SL_POINTS == 10.0
    assert config.TARGET_POINTS == 12.0


def test_kinematics_reset_clears_derivative_clock():
    """reset() must clear _ts (teardown HIGH): otherwise the now<=_ts guard
    rejects fresh observations after an ATM roll until the (now>old_ts) init, and
    a 0.0 timestamp is no longer mis-read as 'missing'."""
    from mythos.flow import Kinematics
    k = Kinematics()
    for i, x in enumerate([100.0, 101.0, 102.0, 103.0]):
        k.update(x, 1000.0 + i)
    assert k._ts == 1003.0
    k.reset()
    assert k._ts == 0.0 and k._s is None and k._s_mark == 0.0
    # a 0.0 timestamp is honored, not treated as falsy/missing
    k2 = Kinematics()
    k2.update(50.0, 0.0)
    assert k2._ts == 0.0 and k2._s == 50.0


def test_multiframe_survives_concurrent_recompute_append():
    """multiframe() (push thread) snapshots each _strike_hist deque; pin that a
    concurrent recompute() appending to those deques can't raise 'deque mutated
    during iteration' (teardown missing-test for the sibling of the oi_divergence
    race)."""
    import threading
    from mythos.oi_engine import OIEngine
    from mythos import clk
    e = OIEngine()
    atm = 25000.0
    now = clk.now()
    for off in range(-12, 13):
        k = atm + off * config.STRIKE_STEP
        e.update_strike(k, "call", 40000.0, now)
        e.update_strike(k, "put", 40000.0, now)
    e.recompute(atm, atm)                          # seed _strike_hist deques

    stop = threading.Event()
    errors = []

    def writer():
        n = 0
        while not stop.is_set():
            try:
                e.recompute(atm, atm + (n % 3) * 50)   # appends to _strike_hist
            except Exception as ex:
                errors.append(("writer", repr(ex)))
            n += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            try:
                e.multiframe(atm, atm)
                e.ladder(atm)
            except Exception as ex:
                errors.append(("reader", repr(ex)))
                break
    finally:
        stop.set()
        t.join(timeout=5)
    assert not errors, f"race crash: {errors[:3]}"


# ─────────────────────────────────────────────────────────────────────────────
# Bare-python runner (used when pytest is absent)
# ─────────────────────────────────────────────────────────────────────────────
def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed  ({len(tests)} money-path "
          f"invariants)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
