"""
MYTHOS — paper trader.

MONEY RULES AS THEY STAND (v3.1 + forensic mandate — config.py is the law,
this header is the map):
    capital   : ₹1,00,000 fresh every trading day; lots = capital/(prem×65)
    hard stop : entry − 10, checked before ALL other exit logic
    breakeven : peak ≥ +10 → stop ratchets to entry+1 (a trade that showed
                +10 may never become a loser — audited mandate)
    hold-first: NO profit exit below peak +20 — trades ride trend breathing
    floor     : peak ≥ +20 arms entry+13 (≥ +12 net) + tiered chandelier
                (~70% of peak protected; widens with trend/gamma/conviction)
    blind hold: 30 s (hard SL only), escape at +12
    stall kill: theta-aware time-stop for dead trades (180/100/75 s by hour)
    cooldowns : asymmetric — 15 s after losses, 90 s after wins (audited)
    EOD       : flatten 15:25; fills = stop level − 1 slip (gaps at market)

Strike selection: among ATM and first two OTM strikes — nearest to
(expected_move + 10) from spot, then tightest spread, premium 40–350,
avoiding strikes just behind same-side OI walls (theta trap).
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import clk, config
from .config import IST
from .flow import ATR, CandleAggregator

log = logging.getLogger("mythos.trader")


@dataclass
class Trade:
    id:            int
    direction:     str            # 'CE' | 'PE'
    strike:        float
    right:         str            # 'call' | 'put'
    lots:          int
    qty:           int            # lots × LOT_SIZE
    entry_price:   float
    entry_time:    str
    entry_epoch:   float
    stop_loss:     float
    target:        float
    entry_score:   float
    entry_components: list = field(default_factory=list)
    peak_price:    float = 0.0
    trail_sl:      float = 0.0
    trail_active:  bool = False
    weakened:      bool = False
    hold_escaped:  bool = False
    exit_price:    float = 0.0
    exit_time:     str = ""
    exit_reason:   str = ""
    pnl_pts:       float = 0.0
    pnl_cash:      float = 0.0
    status:        str = "OPEN"
    weak_count:    int = 0
    last_peak_epoch: float = 0.0   # when peak last advanced (no-progress stall)
    limit_price:   float = 0.0     # resting buy-limit (cheaper entry; fill ref)
    limit_epoch:   float = 0.0     # when the limit was placed (monotonic)
    pending:       bool = False    # True = limit resting, not yet filled
    strike_delta_used: float = 0.0 # the |delta| target this entry aimed at (ATM≈0.50)
    strike_delta_achieved: float = 0.0 # the |delta| of the strike actually chosen


class PaperTrader:
    def __init__(self, prices, store=None, on_event=None):
        """on_event(kind, payload) — kind in {'entry','exit_win','exit_loss'};
        wired to audio + commentary by the app layer."""
        self.prices = prices
        self.store = store
        self.on_event = on_event or (lambda k, p: None)
        self.bypass_time = False           # set True by sim mode only
        self._lock = threading.Lock()
        self._next_id = 1
        self.day: str = datetime.now(IST).strftime("%Y-%m-%d")
        self.capital: float = config.STARTING_CAPITAL
        self.open: List[Trade] = []
        self.closed: List[Trade] = []
        self.consec_sl: Dict[str, int] = {"CE": 0, "PE": 0}
        self._last_exit_epoch: float = 0.0
        self._last_exit_pnl: float = 0.0
        self._safety_exits: int = 0       # STALE QUOTE / EOD closes this session
        self._doctrine_breaches: int = 0  # MUST stay 0 — a sub-+12 exit on a trade
        #                                   that NEVER earned its lock (a real scratch)
        self._gap_throughs: int = 0       # earned the +12 lock, then price gapped
        #                                   straight through it — involuntary, NOT a breach
        # per-trade option-premium ATR for the chandelier
        self._opt_candles = CandleAggregator(15.0, max_candles=80)
        self._opt_atr = ATR(config.TRAIL_ATR_PERIOD)
        self._load_today()

    # ── daily lifecycle ──────────────────────────────────────────────────────
    def roll_day_if_needed(self) -> bool:
        """Reset capital at the first call on a new trading day (§11)."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if today == self.day:
            return False
        with self._lock:
            # a position surviving into a new day means yesterday crashed
            # before EOD flatten — close it on the record, never vaporize it
            for t in list(self.open):
                px = self.prices.option_price(t.strike, t.right) or t.entry_price
                t.exit_price = round(px, 2)
                t.exit_time = "00:00:00"
                t.exit_reason = "ORPHANED (day roll — session crashed?)"
                t.pnl_pts = round(t.exit_price - t.entry_price, 2)
                t.pnl_cash = round(t.pnl_pts * t.qty, 2)
                t.status = "CLOSED"
                self.closed.append(t)
                log.critical("ORPHANED POSITION closed on day roll: #%d %s %g "
                             "%+.1f pts — investigate yesterday's shutdown",
                             t.id, t.direction, t.strike, t.pnl_pts)
                if self.store:
                    self.store.save_trade(self.day, t.id, asdict(t))
            log.info("NEW DAY %s — capital reset to ₹%.0f (was ₹%.0f, %d trades)",
                     today, config.STARTING_CAPITAL, self.capital,
                     len(self.closed))
            self.day = today
            self.capital = config.STARTING_CAPITAL
            self.open.clear()
            self.closed.clear()
            self.consec_sl = {"CE": 0, "PE": 0}
            self._next_id = 1
            self._save_today()
        return True

    # ── strike selection ─────────────────────────────────────────────────────
    def select_strike(self, direction: str, spot: float, atm: float,
                      expected_move: float, oi_engine,
                      conviction: float = 0.0) -> Tuple[float, str]:
        """DELTA-AWARE, ATM by DEFAULT (user: "I trade the ATM generally, or just
        OTM if my conviction is very strong"). Pick the strike whose |delta| is
        closest to the target among the candidate band, subject to liquidity
        (premium in band, tight spread). The target is ATM (≈0.50) unless this
        entry's `conviction` (= ok_count / len(evidence)) is ≥ STRIKE_OTM_CONVICTION,
        in which case it steps to just-OTM (≈0.45). delta is solved from the live
        premium. (Default conviction=0.0 ⇒ pure ATM for any legacy/test caller.)"""
        from . import greeks as gk
        right = "call" if direction == "CE" else "put"
        sign = 1 if direction == "CE" else -1
        T = gk.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))
        # ATM/ITM-THAT-MOVES mode (flag-gated; OFF ⇒ legacy path verbatim). ON: kill
        # the OTM-on-conviction step (high score is anti-predictive), target a delta
        # at/just-inside the money, round TOWARD the money, floor |Δ|≥0.50.
        force = getattr(config, "STRIKE_FORCE_ATM_OR_ITM", False)
        if force:
            target = config.STRIKE_TARGET_DELTA_LIVE
            lo, hi = config.STRIKE_DELTA_BAND_ATM_ITM
        else:
            very_strong = conviction >= config.STRIKE_OTM_CONVICTION
            target = (config.STRIKE_TARGET_DELTA_STRONG if very_strong
                      else config.STRIKE_TARGET_DELTA_ATM)
            lo, hi = config.STRIKE_DELTA_BAND
        self._last_strike_target = target
        self._last_strike_achieved = 0.0
        # toward-money push engages only when the ATM premium is rich enough that
        # fewer-lots actually results (else it just adds a loser on a cheap chain).
        push = force and self.prices.option_price(atm, right) >= config.STRIKE_ITM_PUSH_MIN_PREMIUM

        best_k, best_cost, best_d = atm, float("inf"), 0.0
        for i in range(lo, hi):                     # offsets (sign-aware)
            k = atm + sign * i * config.STRIKE_STEP
            ltp = self.prices.option_price(k, right)
            if ltp < config.MIN_PREMIUM or ltp > config.MAX_PREMIUM:
                continue
            bid = self.prices.opt_bid.get((k, right), 0.0)
            ask = self.prices.opt_ask.get((k, right), 0.0)
            spread = (ask - bid) if (bid > 0 and ask > 0) else config.MAX_SPREAD
            if spread > config.MAX_SPREAD:
                continue
            g = gk.single_greeks(ltp, spot, k, T, right)
            try:
                d = abs(g.delta)
            except Exception:                       # unsolvable IV → single_greeks=None
                if force:
                    continue                         # ON: never award a fake 0.5
                d = 0.5
            # nudge away from a strike with a same-side OI wall one step beyond
            # (price tends to stall there, choking the move toward +12)
            wall_pen = 0.0
            zones = (oi_engine.resistance_zones if direction == "CE"
                     else oi_engine.support_zones)
            for z in zones:
                if abs(z.level - k) <= config.STRIKE_STEP and z.strength >= 0.6:
                    wall_pen = 0.12
            cost = abs(d - target) + spread * 0.01 + wall_pen
            if push:                                 # toward-money tiebreak
                inside = (k <= spot) if direction == "CE" else (k >= spot)
                if inside:
                    cost -= config.STRIKE_ITM_BONUS
            if cost < best_cost:
                best_cost, best_k, best_d = cost, k, d
        # HARD FLOOR (ON only): never settle for a sub-0.50 (OTM) delta. Walk toward
        # the money one strike at a time and take the FIRST that quotes with
        # |delta| >= 0.50. A single step is not enough on a sparse/thin chain: the
        # in-band best can be OTM and the adjacent strike may not quote, so we keep
        # walking (tracking the richest mover we can actually buy) until we land a
        # genuine ATM/ITM mover or run out of quoting strikes.
        if force and best_d < 0.50:
            k = best_k
            band = config.NUM_STRIKES * config.STRIKE_STEP   # the SUBSCRIBED universe
            for _ in range(config.NUM_STRIKES + 2):   # bounded walk toward the money
                k -= sign * config.STRIKE_STEP
                if abs(k - atm) > band:
                    break                             # ran out of SUBSCRIBED strikes —
                                                      # do not query unquoted 0-price strikes
                sl = self.prices.option_price(k, right)
                if sl <= 0:
                    continue                          # strike silent — keep walking
                sg = gk.single_greeks(sl, spot, k, T, right)
                if sg is None:
                    continue
                dd = abs(sg.delta)
                if dd > best_d:                       # richest buyable mover so far
                    best_k, best_d = k, dd
                if dd >= 0.50:                        # first true ATM/ITM mover — take it
                    break
        self._last_strike_achieved = round(best_d, 3)
        return best_k, right

    # ── entry ────────────────────────────────────────────────────────────────
    def try_enter(self, decision, expected_move: float, oi_engine) -> Optional[Trade]:
        with self._lock:
            if len(self.open) >= config.MAX_OPEN:
                return None
            if self.cooldown_remaining() > 0:
                return None     # asymmetric pause — see cooldown_remaining()
            spot, fut, atm, _, _ = self.prices.freeze_core()
            if spot <= 0:
                return None
            # conviction = ok_count / len(evidence) (== decision.score) drives
            # ATM-vs-OTM: ATM by default, OTM only when very strong.
            conviction = float(getattr(decision, "score", 0.0) or 0.0)
            strike, right = self.select_strike(
                decision.direction, spot, atm, expected_move, oi_engine,
                conviction=conviction)
            # ATM/ITM MANDATE (ON-mode only): never send an OTM order. If the book
            # is so thin that no at/inside-money strike quotes (achieved |Δ| stayed
            # below the floor even after the toward-money walk), SKIP — wait for a
            # real mover rather than buy a decaying OTM. Inert on liquid Nifty.
            if (config.STRIKE_FORCE_ATM_OR_ITM
                    and getattr(self, "_last_strike_achieved", 0.0)
                    < config.STRIKE_MIN_DELTA_TO_ENTER):
                log.info("SKIP %s %g — only OTM quotes (got Δ%.2f < %.2f); "
                         "waiting for an ATM/ITM mover", decision.direction, strike,
                         getattr(self, "_last_strike_achieved", 0.0),
                         config.STRIKE_MIN_DELTA_TO_ENTER)
                return None
            ltp = self.prices.option_price(strike, right)
            if ltp < config.MIN_PREMIUM or ltp > config.MAX_PREMIUM:
                return None
            # CHEAPER ENTRY (user mandate) — rest a BUY LIMIT this far BELOW the
            # LTP for a margin of safety, instead of paying market the instant we
            # decide. The trade is created PENDING; it only counts as taken if
            # the option actually trades down to the limit within the wait
            # window (filled in _handle_pending), else it LAPSES — no chase.
            off = max(config.ENTRY_LIMIT_MIN_OFFSET,
                      config.ENTRY_LIMIT_OFFSET_PTS,
                      ltp * config.ENTRY_LIMIT_OFFSET_FRAC)
            limit = round(ltp - off, 2)
            if limit < config.MIN_PREMIUM:      # don't let the discount underflow
                limit = round(ltp, 2)
            # CAPITAL UTILISATION — FULL CAPITAL every trade, exactly as
            # Requirement.txt §7.3 (line 155): lots = floor(capital / (premium *
            # lot_size)). Sized on the LIMIT (the real fill price) so the lot
            # math reflects what we actually pay. An earlier risk-cap /
            # notional-cap / daily-breaker I added WITHOUT being asked is
            # REVERTED — it contradicted this spec.
            lots = int(self.capital // (limit * config.LOT_SIZE))
            if lots < 1:
                return None

            now_ist = datetime.now(IST)
            t = Trade(
                id=self._next_id,
                direction=decision.direction,
                strike=strike,
                right=right,
                lots=lots,
                qty=lots * config.LOT_SIZE,
                entry_price=limit,              # provisional; confirmed on fill
                entry_time=now_ist.strftime("%H:%M:%S"),
                entry_epoch=clk.mono(),
                stop_loss=limit - config.SL_POINTS,
                target=limit + config.TARGET_POINTS,
                entry_score=decision.score,
                strike_delta_used=getattr(self, "_last_strike_target",
                                          config.STRIKE_TARGET_DELTA_ATM),
                strike_delta_achieved=getattr(self, "_last_strike_achieved", 0.0),
                entry_components=[
                    {"name": c.name, "fired": c.fired, "detail": c.detail}
                    for c in decision.components_for(decision.direction)],
                peak_price=limit,
                limit_price=limit,
                limit_epoch=clk.mono(),
                pending=True,
            )
            self.open.append(t)
            self._next_id += 1
            # fresh ATR tracker per position (reset again at fill)
            self._opt_candles = CandleAggregator(15.0, max_candles=80)
            self._opt_atr = ATR(config.TRAIL_ATR_PERIOD)
            log.info("ORDER #%d %s %g LIMIT ₹%.2f (LTP ₹%.2f, −%.2f) ×%d lots "
                     "(score %.2f, target Δ%.2f, got Δ%.2f %s)",
                     t.id, t.direction, strike, limit, ltp, off, lots,
                     decision.score, t.strike_delta_used, t.strike_delta_achieved,
                     "ATM/ITM" if t.strike_delta_achieved >= 0.50 else "OTM")
            self._save_today()
        self.on_event("entry_working", t)    # SILENT — no chime, no position yet
        return t

    # ── exit management ──────────────────────────────────────────────────────
    def check_exits(self, live_score: float = 1.0,
                    trend_agrees: bool = False,
                    gamma_ride: bool = False,
                    prem_vel: float = 0.0,
                    live_dir: str = "") -> List[Trade]:
        """Called every EXIT_CHECK_SEC. live_score = conviction of the held
        direction; trend_agrees = futures flow with the trade; gamma_ride =
        high-gamma regime with trend agreed (convexity earns extra room).
        prem_vel = our-side premium velocity, live_dir = the live decision's
        direction — both used ONLY by the pending-fill knife guard."""
        out = []
        lapsed = []
        with self._lock:
            for t in list(self.open):
                reason = self._check_one(t, live_score, trend_agrees,
                                         gamma_ride, prem_vel, live_dir)
                if reason == "PENDING_LAPSED":
                    self.open.remove(t)          # never filled → NOT a trade, no P&L
                    lapsed.append(t)
                elif reason:
                    self.open.remove(t)
                    self.closed.append(t)
                    out.append(t)
            if out or lapsed:
                self._save_today()
        for t in out:
            self.on_event("exit_win" if t.pnl_pts >= 0 else "exit_loss", t)
        for t in lapsed:
            self.on_event("entry_lapsed", t)     # free the hunt, no trade recorded
        return out

    def _handle_pending(self, t: Trade, live_score: float = 1.0,
                        prem_vel: float = 0.0, live_dir: str = "") -> Optional[str]:
        """A working buy-limit (cheaper entry). Fill cheap if the option dips to
        the limit; otherwise, once the short window elapses, TAKE IT AT MARKET —
        the trade is ALWAYS taken, never lapsed (user: "enter it, don't miss the
        move"). The entry chime + position only happen here, at the real fill.
        Returns None always (the order never leaves `open` except as a fill)."""
        px = self.prices.option_price(t.strike, t.right)
        if px <= 0:
            # the chosen strike isn't quoting. Keep working briefly — but NEVER
            # forever: a never-quoting strike would hold the single MAX_OPEN slot
            # and silently block ALL entries for the session. After 3× the work
            # window, LAPSE the order (it never filled → no trade, no P&L) and
            # free the slot so the engine keeps hunting. (Audit fix, 2026-06-14.)
            if clk.mono() - t.limit_epoch > config.ENTRY_LIMIT_WAIT_SEC * 3:
                log.warning("PENDING LAPSED #%d %s %g — strike never quoted in "
                            "%.0fs; freeing the slot", t.id, t.direction,
                            t.strike, config.ENTRY_LIMIT_WAIT_SEC * 3)
                return "PENDING_LAPSED"
            return None                      # no quote yet — keep working
        bid = self.prices.opt_bid.get((t.strike, t.right), 0.0)
        cheap = (0 < px <= t.limit_price) or (0 < bid <= t.limit_price)
        timed_out = clk.mono() - t.limit_epoch > config.ENTRY_LIMIT_WAIT_SEC
        if not cheap and not timed_out:
            return None                      # still working the cheaper price
        # KNIFE GUARD (cheap-entry fix): a "cheap" dip means premium fell to the
        # limit — for a long option, the underlying moving AGAINST us. Take a
        # shallow dip (thesis intact); HOLD a genuine knife — our-side premium in
        # FREE-FALL **and** the live thesis turned against this side (direction
        # flipped OR conviction collapsed). It never lapses: it keeps working, so a
        # clean market-take still fires at the window end ("don't miss the move").
        if (config.KNIFE_GUARD_ON and cheap and not timed_out
                and prem_vel <= -config.KNIFE_PREM_VEL
                and (live_dir not in ("", t.direction)
                     or live_score < config.KNIFE_MIN_SCORE)):
            log.info("HOLD FILL #%d %s %g — cheap dip is a KNIFE (premVel %.2f/s, "
                     "live %s score %.2f); waiting for a real bottom or the window",
                     t.id, t.direction, t.strike, prem_vel, live_dir or "?",
                     live_score)
            return None                      # don't buy the knife — keep working
        if cheap:
            fill = round(min(t.limit_price, px), 2)
            how = "limit"
        else:                                # window elapsed → take market, don't miss it
            fill = round(px, 2)
            how = "market"
        # LIVE DELTA RE-CHECK (delta/greeks fix). Delta was validated at SELECTION;
        # this fill is up to ENTRY_LIMIT_WAIT_SEC later (or at market), so an adverse
        # drift can leave the chosen strike OTM. Re-solve delta on the LIVE chain at
        # the real fill; if it drifted below the ATM/ITM floor, LAPSE (free the slot,
        # no trade) — the mandate is ATM/ITM that MOVES, never OTM. Only when the
        # mandate is ON; touches NO SL / target / sizing line.
        if config.STRIKE_FORCE_ATM_OR_ITM:
            from . import greeks as gk
            spot, _f, _a, _c, _p = self.prices.freeze_core()
            if spot > 0:
                T = gk.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))
                sg = gk.single_greeks(fill, spot, t.strike, T, t.right)
                dd = abs(sg.delta) if sg else 0.0
                if dd < config.STRIKE_MIN_DELTA_TO_ENTER:
                    log.warning("PENDING LAPSED #%d %s %g — strike drifted OTM at "
                                "fill (Δ%.2f < %.2f); freeing the slot", t.id,
                                t.direction, t.strike, dd,
                                config.STRIKE_MIN_DELTA_TO_ENTER)
                    return "PENDING_LAPSED"
                t.strike_delta_achieved = round(dd, 3)   # record the LIVE achieved delta
        now = clk.mono()
        t.entry_price = fill
        t.stop_loss = round(fill - config.SL_POINTS, 2)
        t.target = round(fill + config.TARGET_POINTS, 2)
        t.peak_price = fill
        t.entry_epoch = now                  # clock starts at FILL, not at order
        t.last_peak_epoch = now
        t.pending = False
        self._opt_candles = CandleAggregator(15.0, max_candles=80)
        self._opt_atr = ATR(config.TRAIL_ATR_PERIOD)
        log.info("FILL #%d %s %g @ ₹%.2f (%s) SL %.2f TGT %.2f",
                 t.id, t.direction, t.strike, fill, how, t.stop_loss, t.target)
        self.on_event("entry", t)            # the real entry chime + position + WHY
        return None

    def _check_one(self, t: Trade, live_score: float,
                   trend_agrees: bool = False,
                   gamma_ride: bool = False,
                   prem_vel: float = 0.0,
                   live_dir: str = "") -> Optional[str]:
        if t.pending:                        # not filled yet — fill or lapse only
            return self._handle_pending(t, live_score, prem_vel, live_dir)
        price = self.prices.option_price(t.strike, t.right)
        if price <= 0:
            return None

        # safety net (the ONLY pre-SL exit): quote went silent (subscription lost
        # / strike out of feed range) → exit at the last known price rather than
        # fly blind. This is a SAFETY exit, not a chosen trade. There is NO hold
        # cap and NO stall kill — the binary doctrine holds to −10 or +12.
        if self.prices.option_age(t.strike, t.right) > config.STALE_QUOTE_EXIT_SEC:
            log.warning("STALE QUOTE #%d: %s %g silent %.0fs — closing at last px",
                        t.id, t.right, t.strike,
                        self.prices.option_age(t.strike, t.right))
            return self._close(t, price, "STALE QUOTE")

        # feed the option-premium ATR (15s candles — premium moves fast)
        closed = self._opt_candles.update(price)
        if closed:
            self._opt_atr.on_candle(closed)

        if price > t.peak_price:
            t.peak_price = price
            t.last_peak_epoch = clk.mono()   # peak advanced — trade alive
        if t.last_peak_epoch == 0.0:
            t.last_peak_epoch = t.entry_epoch
        peak_profit = t.peak_price - t.entry_price
        elapsed = clk.mono() - t.entry_epoch
        since_peak = clk.mono() - t.last_peak_epoch

        # HARD SL FIRST — nothing (blind hold, anything) may see a price at/under
        # the stop before this does. The −10 is a promise. stop_loss is ALWAYS
        # entry−10 (never ratcheted ≥ entry; the breakeven guard is deleted under
        # the binary doctrine), so this is always an honest SL HIT at ≤ −10.
        if price <= t.stop_loss:
            return self._close(t, price, "SL HIT", stop_level=t.stop_loss)

        # blind hold — hard SL only
        if elapsed < config.MIN_HOLD_SEC and not t.hold_escaped:
            if price - t.entry_price >= config.HOLD_ESCAPE_PTS:
                t.hold_escaped = True
            else:
                # hard SL already enforced above — blind hold just waits
                return None

        # NO STALL KILL. The binary doctrine (user, restated 2026-06-14) is
        # absolute: a non-runner is NOT scratched flat — it rides to the −10 hard
        # stop (theta carries a true dud there) or to the +12 trail. A trade that
        # never reaches +12 is an HONEST signal of a bad entry (or a bad sim), and
        # that signal must stay VISIBLE, not masked by a +1.2/-0.3 scratch. The
        # learning loop grades the outcome retrospectively instead.

        # NO small-profit banking below the +12 trail. Hold-first: while the trade
        # has not reached TRAIL_ACTIVATE (+14), the ONLY exit is the −10 hard stop
        # (checked above). It is given time to run to +12. (Progressive lock and
        # breakeven guard REMOVED per profit rule v4 — see config.)

        # dynamic trailing — tiered chandelier (see config for the philosophy)
        # USER RULE (v3.1): no scraps, give winners room. The trail does not exist
        # below peak +20 (TRAIL_ACTIVATE); at +20 it locks entry+13 (≥ +12 net).
        # Below +20 the only exits are the −10 hard stop and the +10 breakeven
        # guard (which ratchets the stop to entry+1). A +19 that fully reverses
        # rides to the −10 stop — holding through is the doctrine, chosen knowingly.
        if not t.trail_active and peak_profit >= config.TRAIL_ACTIVATE:
            t.trail_active = True
            t.trail_sl = t.entry_price + config.TRAIL_INITIAL_LOCK
            log.info("TRAIL ON #%d: peak +%.1f → minimum profit locked at "
                     "entry+%.1f (≥ +6 net)", t.id, peak_profit,
                     config.TRAIL_INITIAL_LOCK)

        # weakening needs to be SUSTAINED (≈3 s), not a one-second score dip —
        # the engine score oscillates every pass and a momentary dip must not
        # permanently choke a runner. Recovers when the score comes back.
        if t.trail_active:
            if live_score < config.SCORE_WEAK:
                t.weak_count += 1
                if t.weak_count >= 12 and not t.weakened:
                    t.weakened = True
                    log.info("WEAKENING #%d: score %.2f sustained → trail ×%.1f",
                             t.id, live_score, config.TRAIL_WEAK_TIGHTEN)
            else:
                t.weak_count = 0
                if t.weakened and live_score >= config.SCORE_WEAK + 0.10:
                    t.weakened = False
                    log.info("RECOVERED #%d: score %.2f → normal trail", t.id,
                             live_score)

        if t.trail_active and peak_profit >= config.CHANDELIER_MIN_PEAK:
            # wider tiers (v3): the trade earned its place — give it air
            pct = (0.30 if peak_profit <= 20.0 else
                   0.24 if peak_profit <= 30.0 else 0.20)
            floor = max(config.TRAIL_MIN_OFFSET,
                        t.entry_price * config.TRAIL_FLOOR_PCT)
            offset = max(floor, pct * peak_profit, self._opt_atr.value)
            if trend_agrees and not t.weakened:
                offset *= config.TRAIL_TREND_WIDEN     # let the runner RUN
            if gamma_ride and not t.weakened:
                offset *= 1.15      # gamma regime: convexity pays the patient
            # user's principle: once the trail has locked a convincing profit,
            # relax the strictness — the trade has earned room to breathe
            if t.trail_sl - t.entry_price >= config.RELAX_LOCK_PTS and not t.weakened:
                offset *= config.RELAX_WIDEN
            # live conviction strong → the data says the move is alive — hold
            if live_score >= 0.60 and not t.weakened:
                offset *= 1.2
            if t.weakened:
                offset *= config.TRAIL_WEAK_TIGHTEN
            # cap: protect ≥70% of the peak no matter how the multipliers
            # stack (also guards against ATR ballooning on spikes)
            offset = min(offset, peak_profit * 0.30)
            offset = max(3.0, offset)
            t.trail_sl = max(t.trail_sl, t.peak_price - offset)

        effective_sl = max(t.stop_loss, t.trail_sl if t.trail_active else 0.0)

        now = datetime.now(IST)
        mins = now.hour * 60 + now.minute
        eod = config.EOD_FLATTEN[0] * 60 + config.EOD_FLATTEN[1]

        if mins >= eod and not self.bypass_time:
            return self._close(t, price, "EOD CLOSE")
        if price <= effective_sl:
            if t.trail_active and t.trail_sl > t.stop_loss:
                return self._close(t, price, "TRAIL SL", stop_level=t.trail_sl)
            return self._close(t, price, "SL HIT", stop_level=t.stop_loss)
        # NOTE: no price-touch lock at +12 — locking entry+13 while price sits
        # at +12.3 put the trail ABOVE market and exited instantly, turning the
        # +12 FLOOR into a take-profit (three straight +12 caps seen live).
        # The peak ≥ +14 engagement above is the only lock: entry+13 then sits
        # safely under price, the floor protects, and runners can develop.
        return None

    def _close(self, t: Trade, price: float, reason: str,
               stop_level: float = 0.0) -> str:
        # fill model for stop-type exits: a real stop order rests at the
        # exchange and triggers the moment price TOUCHES the level — not at
        # this loop's 4 Hz sampling. So small breaches fill at stop − slippage
        # (run7's battle-tested model; also what guarantees the user's ≥+6
        # profit floor: lock entry+7 − 1 slip = +6.0). A breach deeper than
        # 5 pts is a genuine gap — those fill at the observed price, honestly.
        if stop_level > 0 and (stop_level - price) <= 5.0:
            exit_px = max(price, stop_level - config.EXEC_SLIPPAGE)
        else:
            exit_px = price
        exit_px = max(exit_px, 0.05)
        t.exit_price = round(exit_px, 2)
        t.exit_time = datetime.now(IST).strftime("%H:%M:%S")
        t.exit_reason = reason
        t.pnl_pts = round(t.exit_price - t.entry_price, 2)
        t.pnl_cash = round(t.pnl_pts * t.qty, 2)
        t.status = "CLOSED"
        self.capital = round(self.capital + t.pnl_cash, 2)
        self._last_exit_epoch = clk.mono()
        self._last_exit_pnl = t.pnl_pts

        if t.pnl_pts < 0:
            self.consec_sl[t.direction] = min(self.consec_sl[t.direction] + 1, 6)
            self.consec_sl["PE" if t.direction == "CE" else "CE"] = 0
        else:
            self.consec_sl[t.direction] = 0

        log.info("EXIT #%d %s %+0.1fpts ₹%+.0f [%s] peak +%.1f → capital ₹%.0f",
                 t.id, t.direction, t.pnl_pts, t.pnl_cash, reason,
                 t.peak_price - t.entry_price, self.capital)
        # binary-doctrine instrumentation. Three honest buckets for a sub-+12 exit:
        #   • SAFETY  (STALE QUOTE / EOD)            — allowed at any pnl.
        #   • GAP-THROUGH                            — the trade EARNED its lock
        #     (peak ≥ TRAIL_ACTIVATE, so the +12 floor was armed) and price then
        #     gapped straight down through it in one sample. Involuntary: the
        #     market never traded at +12 on the way down, so the fill landed
        #     below the floor. This is NOT a doctrine breach — the doctrine bans
        #     voluntary scratches, not the market gapping you out.
        #   • DOCTRINE BREACH                        — a sub-+12 exit on a trade
        #     that NEVER reached the lock. This must stay 0; the binary exit has
        #     no code path that voluntarily banks in the dead zone, so a nonzero
        #     count means a real regression — flag it loudly.
        peak_earned = (t.peak_price - t.entry_price) >= config.TRAIL_ACTIVATE
        if reason in config.SAFETY_EXIT_REASONS:
            self._safety_exits += 1
        elif -10.0 < t.pnl_pts < 10.0:
            if peak_earned:
                self._gap_throughs += 1
                log.info("GAP-THROUGH #%d %s exited %+.1f — locked +12 floor but "
                         "price gapped through (peak was +%.1f); involuntary",
                         t.id, reason, t.pnl_pts, t.peak_price - t.entry_price)
            else:
                self._doctrine_breaches += 1
                log.warning("DOCTRINE BREACH #%d %s exited %+.1f between -10 and "
                            "+12 with peak only +%.1f — investigate", t.id, reason,
                            t.pnl_pts, t.peak_price - t.entry_price)
        if self.store:
            self.store.save_trade(self.day, t.id, asdict(t))
        return reason

    def snapshot_open(self) -> List[Trade]:
        """Locked copy — safe for any thread to iterate."""
        with self._lock:
            return list(self.open)

    def snapshot_closed(self, n: int = 8) -> List[Trade]:
        with self._lock:
            return list(self.closed[-n:]) if n else list(self.closed)

    def cooldown_remaining(self) -> float:
        if self._last_exit_epoch <= 0:
            return 0.0
        # asymmetric: chasing a WIN's exhausted momentum is the toxic pattern
        # (25% WR audited); re-attempting after a LOSS is fine (56% WR)
        base = (config.ENTRY_COOLDOWN_AFTER_WIN if self._last_exit_pnl >= 5.0
                else config.ENTRY_COOLDOWN_SEC)
        return max(0.0, base - (clk.mono() - self._last_exit_epoch))

    # ── stats ────────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            closed = list(self.closed)
        n = len(closed)
        wins = sum(1 for t in closed if t.pnl_pts >= 0)
        pnl_pts = sum(t.pnl_pts for t in closed)
        pnl_cash = sum(t.pnl_cash for t in closed)
        win_pnls = [t.pnl_cash for t in closed if t.pnl_pts >= 0]
        loss_pnls = [t.pnl_cash for t in closed if t.pnl_pts < 0]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
        wr = wins / n if n else 0.0
        expectancy = wr * avg_win + (1 - wr) * avg_loss if n else 0.0
        # max drawdown on the equity path
        eq, peak, max_dd = config.STARTING_CAPITAL, config.STARTING_CAPITAL, 0.0
        for t in closed:
            eq += t.pnl_cash
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        return {
            "capital": round(self.capital, 2),
            "day_pnl_cash": round(pnl_cash, 2),
            "day_pnl_pts": round(pnl_pts, 2),
            "trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wr * 100, 1),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "expectancy": round(expectancy, 0),
            "max_drawdown": round(max_dd, 0),
        }

    def equity_curve(self) -> List[dict]:
        with self._lock:
            closed = list(self.closed)
        eq = config.STARTING_CAPITAL
        out = [{"time": "09:15:00", "equity": eq}]
        for t in closed:
            eq += t.pnl_cash
            out.append({"time": t.exit_time, "equity": round(eq, 2)})
        return out

    # ── persistence ──────────────────────────────────────────────────────────
    def _save_today(self):
        try:
            data = {
                "day": self.day,
                "capital": self.capital,
                "next_id": self._next_id,
                "consec_sl": self.consec_sl,
                # a PENDING limit is ephemeral (≤8s) and its limit_epoch is a
                # monotonic stamp meaningless in a new process — never persist it
                "open": [asdict(t) for t in self.open if not t.pending],
                "closed": [asdict(t) for t in self.closed],
            }
            tmp = config.TRADES_JSON + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, config.TRADES_JSON)
        except Exception as e:
            log.warning("trade save failed: %s", e)

    def _load_today(self):
        try:
            if not os.path.exists(config.TRADES_JSON):
                return
            with open(config.TRADES_JSON) as f:
                data = json.load(f)
            if data.get("day") != self.day:
                return                      # stale file from a previous day
            self.capital = data.get("capital", config.STARTING_CAPITAL)
            self._next_id = data.get("next_id", 1)
            self.consec_sl = data.get("consec_sl", {"CE": 0, "PE": 0})
            fields = set(Trade.__dataclass_fields__)
            for td in data.get("closed", []):
                self.closed.append(Trade(**{k: v for k, v in td.items()
                                            if k in fields}))
            for td in data.get("open", []):
                self.open.append(Trade(**{k: v for k, v in td.items()
                                          if k in fields}))
            log.info("Restored session: %d closed, %d open, capital ₹%.0f",
                     len(self.closed), len(self.open), self.capital)
        except Exception as e:
            log.warning("trade load failed: %s", e)
