"""
MYTHOS — Zone-Hunter entry engine.

USER'S DOCTRINE (verbatim intent, 2026-06-12 night):
    "Your job is to identify zones. Strong zones. Keep looking at various
     indices and stocks. Keep building conviction along with the price
     movement and then enter at the correct time."
    "Enter at the cheapest possible price where most factors favour the trade."
    "No minimum gating system."  (the old 0.70 weighted-score gate is gone —
     it bought moves AFTER they started, at inflated premium)
    "Be very sure the defense is confirmed... price and velocity of premium
     prices can confirm that."  (confirmation = premium velocity, not distance)
    "Confirm multiple times first."  (evidence must SUSTAIN, not blink once)
    "Once a defense zone has broken, it starts acting as the other side."
     (role reversal: broken support → resistance, broken resistance → support)

THE MACHINE — per direction, a state ladder:
    SCANNING   no qualifying zone in play
    STALKING   strong zone within reach; price approaching; premium cheapening
    ARMED      price has TOUCHED the zone band — defense evidence collection on
    CONFIRMING evidence majority present; must sustain N consecutive seconds
    FIRE       majority sustained + premium still cheap (≤ touch-low + cap)

BOUNCE (primary):  buy CE at defended support, PE at rejected resistance.
BREAK  (secondary): buy CE the moment a strong resistance WALL snaps with
    order-flow thrust (mirror for PE) — the cheapest moment of an expansion
    leg on trend days that never pull back.

Defense evidence (CE at support — PE mirrors):    "most factors" = ≥ 4 sustained
    1. price holding the band (no fresh extreme in the last 20 s)
    2. price has turned (bounced ≥ 4 pts off the touch extreme)
    3. OUR premium velocity flipped positive   ← user's explicit trigger
    4. OPPOSITE premium stalling (velocity ≤ 0)
    5. futures selling pressure exhausting (CVD deceleration or flip)
    6. defenders adding OI at the zone strikes
    7. order book: our option bid-stacked / opposite being dumped
    8. heavyweight basket tailwind (weighted sentiment on our side)

Safety vetoes stay (they are not scoring): market hours, premium band,
spread, fresh quotes, one position, 90 s cooldown. The chop filter and the
0.70 threshold and the consec-SL conviction ramp are deleted — zone structure
itself (and role-reversal blacklisting) replaces them.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import clk, config
from .config import IST
from .flow import Kinematics, _safe_snap


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Evidence:
    name:   str
    ok:     bool
    detail: str = ""


@dataclass
class ZoneView:
    """One direction's hunt, for the engine and the cockpit."""
    state:        str = "SCANNING"     # SCANNING/STALKING/ARMED/CONFIRMING/FIRE
    kind:         str = ""             # BOUNCE / BREAK
    zone_level:   float = 0.0
    zone_strength: float = 0.0
    distance:     float = 0.0          # spot − zone (signed)
    evidence:     List[Evidence] = field(default_factory=list)
    ok_count:     int = 0
    needed:       int = 0
    sustain:      int = 0              # consecutive seconds majority held
    sustain_need: int = 0
    premium_low:  float = 0.0          # our option's low since zone touch
    premium_now:  float = 0.0


@dataclass
class Decision:
    direction: str = "NEUTRAL"
    allowed:   bool = False
    blocked:   str = ""
    cross_blocked: str = ""            # cross-instrument consensus veto reason (task #39)
    kind:      str = ""                # BOUNCE / BREAK
    ce: ZoneView = field(default_factory=ZoneView)
    pe: ZoneView = field(default_factory=ZoneView)
    ts: float = 0.0

    # compatibility for trader/trade-log: evidence list of the fired side
    def components_for(self, direction: str):
        v = self.ce if direction == "CE" else self.pe
        return [type("C", (), {"name": e.name, "fired": e.ok,
                               "detail": e.detail})() for e in v.evidence]

    @property
    def score(self) -> float:
        v = self.ce if self.direction == "CE" else self.pe
        return round(v.ok_count / max(1, len(v.evidence)), 2)


class PremiumVelocity:
    """ATM CE/PE premium history — velocity is the user's confirmation tool."""

    def __init__(self):
        self.ce: deque = deque(maxlen=30)
        self.pe: deque = deque(maxlen=30)

    def update(self, ce_ltp: float, pe_ltp: float):
        if ce_ltp > 0:
            self.ce.append(ce_ltp)
        if pe_ltp > 0:
            self.pe.append(pe_ltp)

    def velocity(self, side: str, n: int = 5) -> float:
        h = self.ce if side == "CE" else self.pe
        if len(h) < n + 1:
            return 0.0
        return (h[-1] - h[-(n + 1)]) / n

    def reset(self):
        self.ce.clear()
        self.pe.clear()


class _Hunt:
    """Mutable per-direction hunt state."""
    __slots__ = ("zone_level", "zone_kind", "touched_ts", "spot_extreme",
                 "extreme_ts", "premium_low", "sustain", "stalk_ts",
                 "approach_extreme")

    def __init__(self):
        self.reset()

    def reset(self):
        self.zone_level = 0.0
        self.zone_kind = ""
        self.touched_ts = 0.0
        self.spot_extreme = 0.0
        self.extreme_ts = 0.0
        self.premium_low = 0.0
        self.sustain = 0
        self.stalk_ts = 0.0
        # the price extreme on the APPROACH side — for CE the HIGH price fell
        # FROM to reach this support (mirror for PE). The drop from here to the
        # touch low is how we tell an EXHAUSTION reversal (big fall into support,
        # the user's doctrine) from a shallow trend-pullback higher-low (which
        # the forensic proved the engine was buying and losing on).
        self.approach_extreme = 0.0


class SignalEngine:
    def __init__(self, oi_engine, flow, vol_engine, basket, prices):
        self.oi = oi_engine
        self.flow = flow
        self.vol = vol_engine
        self.basket = basket
        self.prices = prices
        self.bypass_time_gates = False
        self.prem = PremiumVelocity()
        self.last: Decision = Decision()

        self._hunt: Dict[str, _Hunt] = {"CE": _Hunt(), "PE": _Hunt()}
        self._entry_zone: Dict[str, float] = {"CE": 0.0, "PE": 0.0}
        self._pos_conv: dict = {}          # cached position conviction
        self._conv_ema: Optional[float] = None
        self._conv_tone: str = ""
        self._idx_hist: Dict[str, deque] = {}   # sister-index price history
        # calculus engine: v/a/j of the quantities that decide trades
        self.kin: Dict[str, Kinematics] = {
            "spot": Kinematics(), "ce": Kinematics(), "pe": Kinematics(),
            "pcr": Kinematics(alpha=0.2),
        }
        # zones that stopped us out today: (direction, level) -> count.
        # Not a timer — a burned zone simply demands OVERWHELMING evidence
        # (no fast path) before it may be trusted again.
        self.burned: Dict[Tuple[str, float], int] = {}
        # recent stop-losses (direction, epoch) — whipsaw chop detection:
        # alternating CE/PE stop streaks burned 32% of the audited session's
        # loss points inside 15 combined minutes
        self._stops: deque = deque(maxlen=8)
        # role-reversed zones: level -> ("support"|"resistance", flip_epoch)
        self.flipped_zones: Dict[float, Tuple[str, float]] = {}
        # 15-min spot history for the FLAT/regime classifier. MUST hold the full
        # 900s window — was maxlen=40 (~20-40s), so len(win) could NEVER reach the
        # 60 the classifier requires, leaving market_state() permanently stuck at
        # "building range history" and the FLAT veto DEAD all day (audit finding;
        # the user's "never trade flattish markets" mandate was non-functional).
        self._spot_window: deque = deque(maxlen=1800)  # (ts, spot), 900s @ ~2/s
        self._last_spot_note = 0.0

    # ── zone selection ───────────────────────────────────────────────────────
    def _zones_for(self, direction: str, spot: float,
                   side_allow: float = None) -> List[Tuple[float, float]]:
        """[(level, strength)] candidates: OI zones + role-flipped zones.
        CE hunts supports below/at spot; PE hunts resistances above/at.

        side_allow = how far on the WRONG side of spot a zone may still sit.
        Default ZONE_SIDE_TOL (a few pts) for BOUNCE qualification. The BREAK
        path passes a wider value because a broken-through wall is, by
        definition, behind spot (see _evaluate_direction)."""
        out = []
        # a candidate must sit on the CORRECT side of spot (CE→support at/below,
        # PE→resistance at/above). A few pts of tolerance for the touch band, but
        # NOT the whole ZONE_BAND: letting a "support" 20pt ABOVE spot qualify for
        # CE was firing calls at what is really resistance (entry forensic: 47%
        # of CE fires were this wrong-side geometry — the chasing pathology).
        tol = config.ZONE_SIDE_TOL if side_allow is None else side_allow
        if direction == "CE":
            for z in self.oi.support_zones:
                if z.strength >= config.ZONE_MIN_STRENGTH and z.level <= spot + tol:
                    if self.flipped_zones.get(z.level, ("", 0))[0] != "resistance":
                        out.append((z.level, z.strength))
            for lvl, (side, _) in self.flipped_zones.items():
                if side == "support" and lvl <= spot + tol:
                    out.append((lvl, 0.6))            # flipped zones earn 0.6
            # swing-pivot lows: genuine legs launch from price structure, not
            # only OI walls (user: "not catching genuine 30-35 pt moves")
            for lvl in self.flow.swings.supports():
                if lvl <= spot + tol and \
                        not any(abs(lvl - o[0]) <= 30 for o in out):
                    out.append((lvl, 0.55))
        else:
            for z in self.oi.resistance_zones:
                if z.strength >= config.ZONE_MIN_STRENGTH and z.level >= spot - tol:
                    if self.flipped_zones.get(z.level, ("", 0))[0] != "support":
                        out.append((z.level, z.strength))
            for lvl, (side, _) in self.flipped_zones.items():
                if side == "resistance" and lvl >= spot - tol:
                    out.append((lvl, 0.6))
            for lvl in self.flow.swings.resistances():
                if lvl >= spot - tol and \
                        not any(abs(lvl - o[0]) <= 30 for o in out):
                    out.append((lvl, 0.55))
        # nearest first
        out.sort(key=lambda t: abs(t[0] - spot))
        return out

    def _hw_agrees(self, direction: str, level: float) -> bool:
        hw = (self.basket.implied_support if direction == "CE"
              else self.basket.implied_resistance)
        return hw > 0 and abs(hw - level) <= 2 * config.STRIKE_STEP

    # ── evidence (bounce defense) ────────────────────────────────────────────
    def _defense_evidence(self, d: str, spot: float, atm: float,
                          h: _Hunt) -> List[Evidence]:
        ev: List[Evidence] = []
        bull = (d == "CE")
        right = "call" if bull else "put"
        opp_right = "put" if bull else "call"
        now = clk.now()

        # 1. holding the band: no FRESH extreme in the last 15 s.
        # (Audit fix: the old check compared the window minimum against the
        # all-time extreme — a tautology that always passed. The honest test
        # is the AGE of the extreme: if the low keeps getting lower, the zone
        # is not holding.)
        holding = h.extreme_ts > 0 and (now - h.extreme_ts) >= 15.0
        ev.append(Evidence("Zone holding", holding,
                           f"extreme {h.spot_extreme:.0f} "
                           f"({now - h.extreme_ts:.0f}s old)" if h.extreme_ts
                           else "no touch yet"))

        # 2. CONFIRMED REVERSAL PIVOT off the extreme — a higher-low (CE) /
        # lower-high (PE) that actually PRINTED and HELD. This is the mandatory
        # turn gate and it must be NOISE-ROBUST. The old "Price turned >= 4pt"
        # bar forced entry AFTER the bounce (the forensic's +5pt chase); a draft
        # replacement used 2nd-derivative deceleration (ks.a > 0.012), but an
        # adversarial test driving the real Kinematics proved that fires on ~1/3
        # of mid-FALL ticks (0.012 is below the EMA noise floor) — it would buy
        # into a still-breaking support. A STRUCTURAL pivot is the real signature
        # of a reversal: price has retraced >= TURN_CONFIRM_PTS off the extreme
        # AND has NOT re-made the extreme for >= TURN_HOLD_SEC. It still enters
        # far cheaper (~+2pt) than the old chase, and it cannot fire while price
        # is still making new lows. Pure acceleration physics is now an OPTIONAL
        # vote only (evidence #14), never the mandatory rail.
        held = h.extreme_ts > 0 and (now - h.extreme_ts) >= config.TURN_HOLD_SEC
        retrace = (spot - h.spot_extreme) if bull else (h.spot_extreme - spot)
        pivot = held and retrace >= config.TURN_CONFIRM_PTS
        ev.append(Evidence("Price turning", pivot,
                           f"{retrace:+.0f} off extreme, held "
                           f"{now - h.extreme_ts:.0f}s" if h.extreme_ts
                           else "no touch yet"))

        # 3. our premium velocity positive (user's confirmation).
        # EXPERIMENT #6 (DROP_PREMIUM_RISING_VOTE): a rising premium means the move
        # has ALREADY left — the purest "late" vote. When the flag is on, keep it
        # visible for telemetry but stop it counting toward ok_count (len unchanged).
        v_our = self.prem.velocity(d)
        ev.append(Evidence("Our premium rising",
                           (v_our > 0.05) and not config.DROP_PREMIUM_RISING_VOTE,
                           f"{v_our:+.2f}/s"))

        # 4. opposite premium stalling
        v_opp = self.prem.velocity("PE" if bull else "CE")
        ev.append(Evidence("Their premium stalling", v_opp <= 0.05,
                           f"{v_opp:+.2f}/s"))

        # 5. futures pressure exhausting
        s30 = self.flow.cvd.slope(30)
        s120 = self.flow.cvd.slope(120)
        if bull:
            exhausted = s30 > 0 or (s120 < 0 and s30 > s120 * 0.5)
        else:
            exhausted = s30 < 0 or (s120 > 0 and s30 < s120 * 0.5)
        ev.append(Evidence("Pressure exhausting", exhausted,
                           f"CVD 30s {s30:+.0f} vs 2m {s120:+.0f}"))

        # 6. defenders adding OI at the zone strikes — BOTH the 3-min and the
        # 1-min windows must be positive: building NOW, not just historically
        # (the requirement's "increasing with positive acceleration", §4.1;
        # audit fix — the fast/slow windows were computed but never compared)
        side = "put" if bull else "call"
        fast_w, mid_w = config.OI_EMA_WINDOWS[0], config.OI_EMA_WINDOWS[1]
        rate_fast = rate_mid = 0.0
        for off in (-1, 0, 1):
            tr = self.oi._tracks.get((h.zone_level + off * config.STRIKE_STEP, side))
            if tr:
                rate_fast += tr.emas[fast_w]
                rate_mid += tr.emas[mid_w]
        ev.append(Evidence("Defenders adding OI",
                           rate_mid > 0 and rate_fast > 0,
                           f"{side} OI {rate_mid:+.0f}/s (1m {rate_fast:+.0f})"))

        # 7. order book on our side
        bq = self.prices.opt_bqty.get((atm, right), 0.0)
        aq = self.prices.opt_aqty.get((atm, right), 0.0)
        obq = self.prices.opt_bqty.get((atm, opp_right), 0.0)
        oaq = self.prices.opt_aqty.get((atm, opp_right), 0.0)
        ours_stacked = aq > 0 and bq / aq >= 1.5
        theirs_dumped = obq > 0 and oaq / obq >= 1.5
        ev.append(Evidence("Book favours us", ours_stacked or theirs_dumped,
                           f"our {bq / aq if aq else 0:.1f}× · their offers "
                           f"{oaq / obq if obq else 0:.1f}×"))

        # 8. heavyweight tailwind
        sent = self.basket.sentiment
        ev.append(Evidence("Heavyweights agree",
                           sent >= 55 if bull else sent <= 45,
                           f"basket {sent:.0f}/100"))

        # 9. unwinding fuel — the user's "best money" tell: a reversal off a
        # zone accelerates when the OTHER side is forced out of futures
        # (long unwinding fuels falls, short covering fuels rises)
        quad = self.flow.fut_oi.quadrant
        if bull:
            fuel = quad in ("SHORT_COVERING", "LONG_BUILDUP")
        else:
            fuel = quad in ("LONG_UNWINDING", "SHORT_BUILDUP")
        ev.append(Evidence("Unwinding fuel", fuel,
                           quad.replace("_", " ").lower()))

        # 10. AVWAP reclaim — trapped-trader lens: CE fires when price is above
        # the average price of everyone who sold the day's high (bears under
        # water); PE fires when price is below the day-low buyers' average.
        av = self.flow.avwap
        fut = self.prices.futures or spot
        if bull:
            ok = av.from_high > 0 and fut > av.from_high
            det = f"fut {fut:.0f} vs AVWAP-high {av.from_high:.0f}"
        else:
            ok = av.from_low > 0 and fut < av.from_low
            det = f"fut {fut:.0f} vs AVWAP-low {av.from_low:.0f}"
        ev.append(Evidence("AVWAP reclaim", ok, det))

        # 11. near-ATM PCR shift — the user's "most important aspect": put
        # writers stepping in around ATM (PCR rising) = bulls underwriting
        # the move. Fires on EITHER the 3-min change OR an accelerating PCR
        # velocity (the kinematics were computed but unread — audit fix).
        d_pcr = self.oi.near_pcr_change(180)
        kp = self.kin["pcr"]
        if bull:
            pcr_ok = d_pcr >= 0.05 or (kp.v > 0.0003 and kp.a > 0)
        else:
            pcr_ok = d_pcr <= -0.05 or (kp.v < -0.0003 and kp.a < 0)
        ev.append(Evidence("ATM±6 PCR shift", pcr_ok,
                           f"{self.oi.near_pcr:.2f} ({d_pcr:+.3f}/3m · "
                           f"v {kp.v:+.4f})"))

        # 12. sister indices (Bank Nifty / Fin Nifty) pulling the same way —
        # banks are ~35% of Nifty; Nifty rarely sustains a move they refuse
        ev.append(self._sister_alignment(bull))

        # 13. OI-vs-price divergence (Requirement §4.3 — audit found it
        # implemented but never wired): price rising while near-money call OI
        # falls = bears covering (CE fuel); mirror for PE
        ev.append(Evidence("OI divergence", self.oi.oi_divergence(d, spot),
                           "covering" if bull else "long-unwind"))

        # 14. TURN PHYSICS (2nd derivative) — the calculus tell of a bottom:
        # price still falling but DECELERATING (a > 0 while v ≤ 0) means the
        # turn is forming before it prints — the cheapest moment to be ready.
        # Mirror for tops. Rising-and-accelerating also qualifies.
        ks = self.kin["spot"]
        if bull:
            phys = (ks.v <= 0 and ks.a > 0.012) or (ks.v > 0 and ks.a > -0.008)
        else:
            phys = (ks.v >= 0 and ks.a < -0.012) or (ks.v < 0 and ks.a < 0.008)
        ev.append(Evidence("Turn physics d²/dt²", phys,
                           f"v {ks.v:+.2f}/s · a {ks.a:+.3f} · j {ks.j:+.4f}"))
        return ev

    def _sister_alignment(self, bull: bool) -> Evidence:
        now = clk.now()
        agree = avail = 0
        details = []
        # snapshot with retry — the index dict is written by a poller thread
        for _ in range(3):
            try:
                idx_items = list(self.prices.idx_ltp.items())
                break
            except RuntimeError:
                continue
        else:
            idx_items = []
        for name, ltp in idx_items:
            if ltp <= 0:
                continue
            # staleness gate (audit fix): a dead poller must not keep voting
            # with a frozen price — skip indices silent for >60 s
            ts = self.prices.idx_ts.get(name, 0.0)
            if ts > 0 and (clk.mono() - ts) > 60.0:
                continue
            h = self._idx_hist.setdefault(name, deque(maxlen=300))
            if not h or now - h[-1][0] >= 1.0:
                h.append((now, ltp))
            past = next((v for t, v in h if now - t <= 180), None)
            if past and past > 0:
                chg = (ltp - past) / past * 100.0
                avail += 1
                if (chg >= 0.03) if bull else (chg <= -0.03):
                    agree += 1
                details.append(f"{name[:4]} {chg:+.2f}%")
        fired = avail > 0 and agree >= 1 and agree * 2 >= avail
        return Evidence("BankNifty/FinNifty agree", fired,
                        " · ".join(details) or "no sister data")

    def _lead_vote(self, d: str) -> int:
        """CROSS-INSTRUMENT LEAD (task #37): +1 strong-agree, -1 strong-disagree,
        0 neutral. BankNifty's short-window momentum is the LEADER (banks turn
        before Nifty); basket sentiment confirms. Used to fire a near-ready Nifty
        setup ONE PASS EARLIER when banks have already turned — the cure for "the
        move is already done when the trade comes". Reads only recorded-and-
        replayable state (idx_ltp/idx_ts + price-only sentiment). DEGRADES TO 0 on
        stale/absent/unresolved BankNifty so a dead poller never blocks or forces
        an entry. Flag-gated (CROSS_LEAD_ON); returns 0 when off = byte-identical."""
        if not config.CROSS_LEAD_ON:
            return 0
        bull = (d == "CE")
        now = clk.now()
        for _ in range(3):
            try:
                hist = self._idx_hist.get("BANKNIFTY")
                ltp = self.prices.idx_ltp.get("BANKNIFTY", 0.0)
                ts = self.prices.idx_ts.get("BANKNIFTY", 0.0)
                break
            except RuntimeError:
                continue
        else:
            return 0
        if ltp <= 0 or not hist or (ts > 0 and (clk.mono() - ts) > 60.0):
            return 0
        win = config.CROSS_LEAD_WINDOW_SEC
        past = next((v for t, v in list(hist) if 0 < now - t <= win), None)
        if not past or past <= 0:
            return 0
        bn_mom = (ltp - past) / past * 100.0
        # Nifty's OWN move over the same window — the lead must EXCEED it, else
        # banks are merely tagging along (coincident), not leading.
        sw = _safe_snap(self._spot_window)
        nif_past = next((s for t, s in sw if 0 < now - t <= win), None)
        nif_now = sw[-1][1] if sw else 0.0
        nif_mom = ((nif_now - nif_past) / nif_past * 100.0) \
            if (nif_past and nif_past > 0) else 0.0
        sent = self.basket.sentiment
        pct, edge = config.CROSS_LEAD_BN_PCT, config.CROSS_LEAD_BN_EDGE_PCT
        req = config.CROSS_LEAD_REQUIRE_SENT       # sentiment is OPTIONAL (coincident +
        hi, lo = config.CROSS_LEAD_SENT_HI, config.CROSS_LEAD_SENT_LO  # unprovable on tape)
        if bull:
            if bn_mom >= pct and (bn_mom - nif_mom) >= edge and (not req or sent >= hi):
                return +1
            if bn_mom <= -pct and (not req or sent <= lo):
                return -1
        else:
            if bn_mom <= -pct and (nif_mom - bn_mom) >= edge and (not req or sent <= lo):
                return +1
            if bn_mom >= pct and (not req or sent >= hi):
                return -1
        return 0

    def _vis_inflection(self, d: str) -> bool:
        """VELOCITY-INFLECTION SNAP core (task #38): True when the SPOT and our
        ATM PREMIUM 2nd-derivatives have BOTH turned up (for CE; mirror for PE),
        freshly (jerk still building) — the leading reversal signature read AT the
        inflection, before any higher-low prints. A two-instrument agreement an
        EMA wiggle on one series cannot fake. False if the premium series is cold
        after an ATM roll (prem/kin were reset). The novel, testable trigger; the
        context floors (exhaustion/cheap/strength/pressure) are applied by caller."""
        bull = (d == "CE")
        ks = self.kin["spot"]
        ka = self.kin["ce" if bull else "pe"]
        prem = self.prem.ce if bull else self.prem.pe
        if len(prem) < config.VIS_PREM_READY_PASSES + 1:
            return False                              # cold premium post ATM-roll
        v_our = self.prem.velocity(d)
        prem_ok = (ka.a >= config.VIS_PREM_A_MIN
                   and v_our >= -config.VIS_PREM_V_TOL)
        if bull:                                       # CE: spot stops falling, turns up
            spot_ok = (ks.v <= 0.0 and ks.a >= config.VIS_SPOT_A_MIN
                       and ks.j >= config.VIS_SPOT_J_MIN)
        else:                                          # PE: spot stops rising, turns down
            spot_ok = (ks.v >= 0.0 and ks.a <= -config.VIS_SPOT_A_MIN
                       and ks.j <= -config.VIS_SPOT_J_MIN)
        return spot_ok and prem_ok

    def _consensus(self, spot: float, atm: float):
        """CO-EQUAL CROSS-INSTRUMENT CONSENSUS (task #39): fuse BankNifty/FinNifty
        (day momentum + OI), the stock basket (price+PCR+walls via sentiment),
        futures FLOW, price-action TREND and Nifty OI STRUCTURE into one net C via
        the single-source consensus_core. Returns the cons dict or None when the
        gate is off (lazy import → flag-off path never loads it = byte-identical)."""
        if not config.CONSENSUS_GATE_ON:
            return None
        try:
            import consensus_core as cc
            cons = cc.consensus_for(spot, atm, self.oi, self.flow, self.basket,
                                    self.prices, self, self._idx_hist, clk.now())
        except Exception:
            return None
        cons["_cmin"] = config.CONSENSUS_MIN
        cons["_cmax"] = config.CONTESTED_MAX
        return cons

    def _consensus_blocks(self, d: str, cons: dict) -> bool:
        """True = the bloc REFUSES direction d — it leans confidently AGAINST d on
        a non-split board. A weak or contested board does NOT veto (let Nifty's own
        engine decide on a quiet/split tape). This is the co-equal semantics: the
        cross-instrument bloc gets veto power equal to Nifty's signal, but only when
        it genuinely and confidently disagrees."""
        try:
            import consensus_core as cc
        except Exception:
            return False
        cmin = cons.get("_cmin", cc.CONSENSUS_MIN)
        cmax = cons.get("_cmax", cc.CONTESTED_MAX)
        if cons.get("contested", 1.0) >= cmax:
            return False                          # split board abstains
        C = cons.get("C", 0.0)
        return (C <= -cmin) if d == "CE" else (C >= cmin)

    # ── thrust evidence (break entries) ──────────────────────────────────────
    def _thrust_evidence(self, d: str, spot: float, atm: float,
                         wall: float) -> List[Evidence]:
        ev: List[Evidence] = []
        bull = (d == "CE")
        s30 = self.flow.cvd.slope(30)
        ev.append(Evidence("Flow thrust",
                           s30 > 0 and self.flow.cvd.accelerating() if bull
                           else s30 < 0 and self.flow.cvd.accelerating(),
                           f"CVD {s30:+.0f}/s"))
        v_our = self.prem.velocity(d, 3)
        ka = self.kin["ce" if bull else "pe"]
        ev.append(Evidence("Premium exploding", v_our > 0.3 and ka.a > 0,
                           f"v {v_our:+.2f}/s · a {ka.a:+.3f}"))
        # wall defenders capitulating: the broken side's OI at the wall falling
        side = "call" if bull else "put"
        mid_w = config.OI_EMA_WINDOWS[1]
        tr = self.oi._tracks.get((wall, side))
        ev.append(Evidence("Wall capitulating", bool(tr and tr.emas[mid_w] < 0),
                           f"{side} OI at {wall:.0f}"))
        right = "call" if bull else "put"
        bq = self.prices.opt_bqty.get((atm, right), 0.0)
        aq = self.prices.opt_aqty.get((atm, right), 0.0)
        ev.append(Evidence("Buyers queuing", aq > 0 and bq / aq >= 1.5,
                           f"book {bq / aq if aq else 0:.1f}×"))
        quad = self.flow.fut_oi.quadrant
        fuel = (quad in ("SHORT_COVERING", "LONG_BUILDUP") if bull
                else quad in ("LONG_UNWINDING", "SHORT_BUILDUP"))
        ev.append(Evidence("Unwinding fuel", fuel,
                           quad.replace("_", " ").lower()))
        return ev

    # ── market regime (user mandate: trade trends, narrate always, REFUSE
    #     flat tape — but never go silent) ───────────────────────────────────
    def market_state(self) -> Tuple[str, str]:
        """Returns (state, narration). States:
        FLAT     — narrow + weak ADX + no flow: entries BLOCKED
        COILING  — narrow but OI building: breakout watch, entries allowed
        TRENDING — directional flow: prime conditions
        ACTIVE   — normal two-way movement"""
        spot = self.prices.spot
        if spot <= 0:
            return "ACTIVE", "warming up"
        now = clk.now()
        win = [s for t, s in _safe_snap(self._spot_window) if now - t <= 900]  # 15m
        if len(win) < 60:
            return "ACTIVE", "building range history"
        rng = max(win) - min(win)
        rng_pct = rng / spot
        adx = self.flow.adx.value if self.flow.adx.ready else 25.0
        slope = abs(self.flow.cvd.slope(60))
        oi_building = abs(self.oi.near_pcr_change(300)) >= 0.04

        if rng_pct < config.FLAT_RANGE_PCT and adx < config.FLAT_ADX_MAX \
                and slope < config.FLAT_CVD_SLOPE:
            if oi_building:
                return "COILING", (f"COILING — {rng:.0f}-pt squeeze with OI "
                                   f"building: breakout loading, stay alert")
            return "FLAT", (f"FLAT — {rng:.0f} pts in 15 min, ADX {adx:.0f}, "
                            f"no flow: refusing entries until the tape wakes")
        if rng_pct < config.COIL_RANGE_PCT and oi_building:
            return "COILING", (f"COILING — narrow {rng:.0f}-pt range but "
                               f"writers repositioning: breakout watch")
        if adx >= 25 and slope >= config.FLAT_CVD_SLOPE:
            return "TRENDING", (f"TRENDING — ADX {adx:.0f}, flow "
                                f"{self.flow.cvd.slope(60):+.0f}/s: prime tape")
        return "ACTIVE", f"ACTIVE — {rng:.0f}-pt range, two-way tape"

    def _swing_ref(self, bull: bool, window_sec: float):
        """Prior-swing reference for EXPERIMENT #1: max (CE) / min (PE) spot over
        the trailing window, reusing the same _spot_window market_state uses.
        Returns None when history is too short (<60 samples) so the caller falls
        back to the local anchor — never fabricates a swing from thin data."""
        now = clk.now()
        win = [s for t, s in _safe_snap(self._spot_window) if now - t <= window_sec]
        if len(win) < 60:
            return None
        return max(win) if bull else min(win)

    # ── vetoes (safety, not scoring) ─────────────────────────────────────────
    def _time_gate(self) -> Optional[str]:
        if self.bypass_time_gates:
            return None
        now = datetime.now(IST)
        o_h, o_m = config.MARKET_OPEN
        c_h, c_m = config.MARKET_CLOSE
        mins = now.hour * 60 + now.minute
        if mins < o_h * 60 + o_m:
            return "PRE-MARKET"
        if mins >= c_h * 60 + c_m:
            return "MARKET CLOSED"
        if mins < o_h * 60 + o_m + config.AVOID_FIRST_MINS:
            return f"OPENING VOLATILITY (first {config.AVOID_FIRST_MINS} min)"
        ne_h, ne_m = config.NO_ENTRY_AFTER
        if mins >= ne_h * 60 + ne_m:
            return f"NO NEW ENTRIES AFTER {ne_h:02d}:{ne_m:02d}"
        return None

    # ── main evaluation (1 Hz) ───────────────────────────────────────────────
    def evaluate(self) -> Decision:
        spot, fut, atm, ce_ltp, pe_ltp = self.prices.freeze_core()
        dec = Decision(ts=clk.now())
        if spot <= 0 or atm <= 0:
            dec.blocked = "WAITING FOR DATA"
            self.last = dec
            return dec

        now = clk.now()
        # ATM strike shift: premium velocity must NOT span two different
        # option series (audit fix — false confirmations on fast tape)
        if atm != getattr(self, "_last_atm", 0.0):
            if getattr(self, "_last_atm", 0.0) > 0:
                self.prem.reset()
                self.kin["ce"].reset()
                self.kin["pe"].reset()
            self._last_atm = atm
        self.prem.update(ce_ltp, pe_ltp)
        # feed the calculus engine (1 Hz cadence)
        self.kin["spot"].update(spot, now)
        self.kin["ce"].update(ce_ltp, now)
        self.kin["pe"].update(pe_ltp, now)
        if self.oi.near_pcr > 0:
            self.kin["pcr"].update(self.oi.near_pcr, now)
        if now - self._last_spot_note >= 0.5:
            self._spot_window.append((now, spot))
            self._last_spot_note = now

        self._mark_breaks(spot, now)

        gate = self._time_gate()
        views = {}
        for d in ("CE", "PE"):
            views[d] = self._evaluate_direction(d, spot, atm,
                                                ce_ltp if d == "CE" else pe_ltp)
        dec.ce, dec.pe = views["CE"], views["PE"]

        if gate:
            dec.blocked = gate
            self.last = dec
            return dec

        # FLAT MARKET: hunts keep narrating, but no entry fires on dead tape
        # (user mandate: never trade flattish markets — and say so out loud)
        mstate, mnote = self.market_state()
        if mstate == "FLAT":
            for v in views.values():
                if v.state == "FIRE":
                    v.state = "CONFIRMING"      # show the readiness, hold fire
            dec.ce, dec.pe = views["CE"], views["PE"]
            dec.blocked = mnote
            self.last = dec
            return dec

        # EXPERIMENT #2 (VETO_BOUNCE_IN_TREND): the forensic showed ~70% of CE
        # fires are with-move chases — a BOUNCE fired while the tape is TRENDING is
        # buying a pullback the trend resumes through. When on, demote bounce FIREs
        # to CONFIRMING in a trend; BREAK fires (the legitimate trend-day thrust)
        # proceed untouched. Default off = byte-identical.
        if config.VETO_BOUNCE_IN_TREND and mstate == "TRENDING":
            for v in views.values():
                if v.state == "FIRE" and v.kind == "BOUNCE":
                    v.state = "CONFIRMING"
            dec.ce, dec.pe = views["CE"], views["PE"]

        # CROSS-INSTRUMENT LEAD veto (task #37): if BankNifty is ripping the OTHER
        # way with the basket against us, demote that side's FIRE to CONFIRMING.
        # Applies to BOTH bounce and break (a trend-day BREAK with banks against it
        # is the false-thrust case). Separately gated by CROSS_LEAD_VETO = no-op off.
        if config.CROSS_LEAD_ON and config.CROSS_LEAD_VETO:
            for _d, v in views.items():
                if v.state == "FIRE" and self._lead_vote(_d) < 0:
                    v.state = "CONFIRMING"
            dec.ce, dec.pe = views["CE"], views["PE"]

        # CO-EQUAL CROSS-INSTRUMENT CONSENSUS GATE (task #39, USER MANDATE, default
        # ON). The broad tape — BankNifty/FinNifty (day momentum + OI), the stock
        # basket (price+PCR+walls via sentiment), futures FLOW, price-action TREND,
        # Nifty OI STRUCTURE — is fused by consensus_core into net C in [-1,+1] with
        # a CONTESTED measure. A FIRE that fights a CONFIDENT, NON-SPLIT bloc is
        # DEMOTED to CONFIRMING — the cross-instrument bloc holds veto power equal to
        # Nifty's own signal. Computed from ALWAYS-DEFINED inputs every pass, so it
        # ACTS (unlike the inert CROSS_LEAD spike gate). DEMOTE-ONLY; never fires.
        cons = self._consensus(spot, atm)
        if cons is not None:
            for _d, v in views.items():
                if v.state != "FIRE":
                    continue
                if v.kind == "BREAK" and not config.CONSENSUS_GATE_BREAK:
                    continue                       # trend-day thrust runs (default)
                if self._consensus_blocks(_d, cons):
                    v.state = "CONFIRMING"
                    why = (f"{_d} fights the broad tape (C {cons.get('C', 0):+.2f}, "
                           f"contested {cons.get('contested', 0):.2f})")
                    dec.cross_blocked = why
                    dec.blocked = f"CROSS-TAPE VETO — {why}; held to CONFIRMING"
            dec.ce, dec.pe = views["CE"], views["PE"]

        # FIRE: bounce first (cheaper), break as the trend-day fallback
        for d in ("CE", "PE"):
            v = views[d]
            if v.state == "FIRE":
                # quote freshness on the instrument we'd buy
                right = "call" if d == "CE" else "put"
                if self.prices.option_age(atm, right) > 15.0:
                    v.state = "CONFIRMING"
                    dec.blocked = "STALE OPTION QUOTE"
                    continue
                dec.direction = d
                dec.kind = v.kind
                dec.allowed = True
                dec.blocked = ""
                self.last = dec
                return dec

        # not firing — report the most advanced hunt for the banner
        order = {"CONFIRMING": 3, "ARMED": 2, "STALKING": 1, "SCANNING": 0}
        lead = max(("CE", "PE"), key=lambda d: order[views[d].state])
        v = views[lead]
        dec.direction = lead if v.state != "SCANNING" else "NEUTRAL"
        dec.blocked = {
            "SCANNING": "SCANNING — no strong zone in play",
            "STALKING": f"STALKING {lead} zone {v.zone_level:.0f} "
                        f"({v.distance:+.0f} pts away)",
            "ARMED": f"ARMED at {v.zone_level:.0f} — defense "
                     f"{v.ok_count}/{v.needed} confirming",
            "CONFIRMING": f"CONFIRMING at {v.zone_level:.0f} — "
                          f"{v.ok_count}/{v.needed} held {v.sustain}/"
                          f"{v.sustain_need}s",
        }[v.state]
        self.last = dec
        return dec

    def _evaluate_direction(self, d: str, spot: float, atm: float,
                            our_ltp: float) -> ZoneView:
        h = self._hunt[d]
        bull = (d == "CE")
        view = ZoneView(needed=config.EVIDENCE_NEED,
                        sustain_need=config.EVIDENCE_SUSTAIN,
                        premium_now=our_ltp)

        zones = self._zones_for(d, spot)
        # ── BREAK check first: opposing wall snapping ─────────────────────────
        # the broken wall is BEHIND spot (up to BREAK_CHASE_MAX past it), so the
        # break lookup must reach onto the wrong side — the tight BOUNCE side-tol
        # would make every breakout structurally impossible (regression fix).
        opp_zones = self._zones_for("PE" if bull else "CE", spot,
                                    side_allow=config.BREAK_CHASE_MAX)
        for lvl, strength in opp_zones:
            beyond = (spot - lvl) if bull else (lvl - spot)
            if config.BREAK_BEYOND <= beyond <= config.BREAK_CHASE_MAX \
                    and strength >= config.ZONE_MIN_STRENGTH:
                # sustain counters must not cross-contaminate between BREAK
                # and BOUNCE assessments of the same hunt (teardown finding:
                # an inherited count let entries fire 1-2 s early)
                if h.zone_kind != "BREAK":
                    h.sustain = 0
                    h.zone_kind = "BREAK"
                ev = self._thrust_evidence(d, spot, atm, lvl)
                ok = sum(1 for e in ev if e.ok)
                view.kind = "BREAK"
                view.zone_level = lvl
                view.zone_strength = strength
                view.distance = spot - lvl
                view.evidence = ev
                view.ok_count = ok
                view.needed = config.BREAK_THRUST_NEED
                if ok >= config.BREAK_THRUST_NEED:
                    h.sustain += 1
                    view.sustain = h.sustain
                    view.sustain_need = 2          # breaks move fast — 2 s
                    if h.sustain >= 2:
                        view.state = "FIRE"
                        return view
                    view.state = "CONFIRMING"
                    return view
                h.sustain = 0
                view.state = "ARMED"
                return view

        # ── BOUNCE ladder ─────────────────────────────────────────────────────
        if not zones:
            h.reset()
            view.state = "SCANNING"
            return view

        lvl, strength = zones[0]
        # ZONE HYSTERESIS: once hunting a zone, stick with it unless the new
        # nearest is meaningfully elsewhere (>25 pts) or out of range — the
        # nearest-zone choice flickering between close candidates was
        # resetting hunts and discarding touch progress (no-trade stretches)
        if h.zone_level > 0 and abs(h.zone_level - lvl) <= 25:
            lvl = h.zone_level
        view.kind = "BOUNCE"
        view.zone_level = lvl
        view.zone_strength = round(strength, 2)
        view.distance = round(spot - lvl, 1)

        dist = (spot - lvl) if bull else (lvl - spot)   # +ve = right side
        in_band = abs(spot - lvl) <= config.ZONE_BAND
        if dist > config.ZONE_STALK_DIST:
            h.reset()
            view.state = "SCANNING"
            return view

        # kind switch BREAK→BOUNCE on same hunt: confirmation starts fresh
        if h.zone_kind != "BOUNCE":
            h.sustain = 0
            h.zone_kind = "BOUNCE"
        # genuinely new zone → re-arm
        if h.zone_level != lvl:
            h.reset()
            h.zone_kind = "BOUNCE"
            h.zone_level = lvl
            h.stalk_ts = clk.now()
            h.spot_extreme = spot
            h.approach_extreme = spot      # start tracking the approach high/low

        # track the APPROACH extreme (the price we are falling FROM into a CE
        # support / rising FROM into a PE resistance) every pass while hunting —
        # the drop/rise from here to the touch is the exhaustion measure.
        if h.approach_extreme == 0.0:
            h.approach_extreme = spot
        if bull:
            h.approach_extreme = max(h.approach_extreme, spot)
        else:
            h.approach_extreme = min(h.approach_extreme, spot)

        if not in_band and h.touched_ts == 0.0:
            view.state = "STALKING"
            return view

        # touched
        if h.touched_ts == 0.0:
            h.touched_ts = clk.now()
            h.spot_extreme = spot
            h.extreme_ts = clk.now()
            h.premium_low = our_ltp
        # track extremes (with timestamp — zone-holding = extreme aging)
        # & cheapest premium since touch
        if bull and spot < h.spot_extreme:
            h.spot_extreme = spot
            h.extreme_ts = clk.now()
        elif not bull and spot > h.spot_extreme:
            h.spot_extreme = spot
            h.extreme_ts = clk.now()
        if our_ltp > 0:
            h.premium_low = min(h.premium_low or our_ltp, our_ltp)
        view.premium_low = round(h.premium_low, 2)

        ev = self._defense_evidence(d, spot, atm, h)
        view.evidence = ev
        view.ok_count = sum(1 for e in ev if e.ok)

        # FAST PATH: overwhelming defense = a genuine leg igniting — act in
        # one pass with a wider cheapness cap, or the best moves escape.
        strong = view.ok_count >= config.EVIDENCE_STRONG
        cap = config.CHEAP_CAP_STRONG if strong else config.CHEAP_CAP_PTS
        sustain_need = config.SUSTAIN_STRONG if strong else config.EVIDENCE_SUSTAIN
        # BURNED ZONE / WHIPSAW: either demands overwhelming evidence + full
        # sustain (data-driven escalation, not a block). Burn keys rounded to
        # 5 pts so pivot drift can't evade.
        need = config.EVIDENCE_NEED
        if (self.burned.get((d, round(lvl / 5) * 5), 0) > 0
                or self.whipsaw_active()):
            need = config.EVIDENCE_STRONG
            sustain_need = config.SUSTAIN_BURNED      # full confirm on a burned
            view.needed = need                         # zone, decoupled from the
                                                       # normal-entry sustain
        view.sustain_need = sustain_need
        # CROSS-INSTRUMENT LEAD (task #37, flag-gated; default off = byte-identical).
        # When BankNifty has ALREADY turned our way (strong agree), let a near-ready
        # setup fire ONE PASS EARLIER: drop the evidence bar by one (floored at
        # CROSS_LEAD_MIN_NEED) and require only single-pass sustain. The three
        # safety rails (pressure/turn/exhaustion) below stay AND-ed, so this can
        # never fire into a still-falling knife — it only shaves Nifty's own lag.
        lead = self._lead_vote(d)
        if lead > 0:
            need = max(config.CROSS_LEAD_MIN_NEED, need - 1)
            sustain_need = config.SUSTAIN_STRONG
            view.sustain_need = sustain_need
            if config.CROSS_LEAD_WIDEN_CAP:
                cap = config.CHEAP_CAP_STRONG
        cheap = (our_ltp > 0 and h.premium_low > 0
                 and our_ltp <= h.premium_low + cap)

        # REQUIRED RAIL #1 — down-pressure (CE) must have actually STOPPED or
        # flipped, not merely decelerated. The 'Pressure exhausting' evidence
        # uses a ratio clause (s30 > s120*0.5) that stays true while selling is
        # still net-negative — fine as a vote, too weak as a safety rail (an
        # adversarial review proved both old rails were satisfiable mid-break).
        # The mandatory rail now requires the 30s CVD slope SIGN to flip.
        pressure_ok = True
        if config.REQUIRE_PRESSURE_OK:
            s30_now = self.flow.cvd.slope(30)
            pressure_ok = (s30_now >= 0.0) if bull else (s30_now <= 0.0)

        # REQUIRED RAIL #2 — a confirmed reversal pivot has printed and held
        # (evidence #2). Together with rail #1 this guarantees we never fire
        # while price is still falling and selling is still active = no knife.
        turn_ok = any(e.name == "Price turning" and e.ok for e in ev)
        # CROSS-INSTRUMENT rail relaxation (task #37, flag-gated, default off): when
        # BankNifty has STRONGLY led our way (lead>0) and RAIL#1 (CVD sign-flip)
        # already holds, the bank's confirmed turn stands in for Nifty's own not-yet-
        # printed pivot — firing earlier. RAIL#1 (pressure) and RAIL#3 (exhaustion,
        # below) stay AND-ed, so this still can't buy into active selling or mid-rally.
        if (not turn_ok and lead > 0 and pressure_ok
                and config.CROSS_LEAD_RELAX_TURN):
            turn_ok = True

        # REQUIRED RAIL #3 — EXHAUSTION. Price must have genuinely DECLINED into
        # this support before turning (mirror for PE), not merely pulled back a
        # few pts in an up-move. The forensic proved the engine was firing CE at
        # shallow higher-lows DURING rallies (entered +5pt into a rise, 70%
        # with-move) — trend-pullback buying, the INVERSE of the user's
        # exhaustion-reversal doctrine. This rail enforces a real fall into the
        # zone, so we buy the bottom of a move that died, not the dip of a rally.
        # EXPERIMENT #1 (EXHAUSTION_PRIOR_SWING_ON): h.approach_extreme is RESET to
        # spot the instant a new zone is hunted (see ~line 695) and only extended
        # over the few in-band passes, so in a steady rally the "fall into zone" is
        # measured from a high printed seconds earlier — a shallow pullback fakes a
        # 12pt drop. When the flag is on, anchor instead to a genuine prior swing
        # (max/min spot over a trailing window); fall back to the old local anchor
        # if history is too short, so it degrades safely. Default off = byte-identical.
        anchor = h.approach_extreme
        if config.EXHAUSTION_PRIOR_SWING_ON:
            ref = self._swing_ref(bull, config.EXHAUSTION_SWING_WINDOW_SEC)
            if ref is not None:
                anchor = max(ref, h.approach_extreme) if bull \
                    else min(ref, h.approach_extreme)
        drop_in = ((anchor - h.spot_extreme) if bull
                   else (h.spot_extreme - anchor))
        exhaustion_ok = drop_in >= config.EXHAUSTION_DROP_PTS
        view.evidence.append(Evidence(
            "Exhaustion (fall into zone)", exhaustion_ok,
            f"{drop_in:+.0f} pts into zone (need {config.EXHAUSTION_DROP_PTS:.0f})"))

        # EXPERIMENT #4 (ENTRY_SCORE_CEILING): high evidence is anti-predictive
        # (WR collapses 0.7→0%, 0.8→18%). When the ceiling is >0, VETO a fire whose
        # ok_count exceeds it — removing the late high-confirmation cohort while the
        # four mandatory rails still gate every fire. 0 = off (byte-identical).
        score_ok = (config.ENTRY_SCORE_CEILING <= 0
                    or view.ok_count <= config.ENTRY_SCORE_CEILING)

        # ── VELOCITY-INFLECTION SNAP (task #38, flag-gated; OFF = skipped, then
        # the legacy predicate below runs byte-identically). The SMART single
        # leading decision: fire the instant spot+premium inflect up TOGETHER at a
        # strong exhausted cheap zone — no ok_count tally, no held pivot (RAIL#2),
        # no sustain. Keeps exhaustion(RAIL#3)+strength+cheap+CVD-flip(RAIL#1) as
        # hard floors and skips burned/whipsaw zones (those route through the
        # strict legacy path). The −10 stop is the risk control for being early.
        if (config.VIS_ENTRY_ON and in_band and exhaustion_ok and cheap
                and score_ok and strength >= config.ZONE_MIN_STRENGTH
                and view.ok_count >= config.VIS_MIN_OK
                and ((not config.VIS_KEEP_PRESSURE) or pressure_ok)
                and not (self.burned.get((d, round(lvl / 5) * 5), 0) > 0
                         or self.whipsaw_active())
                and self._vis_inflection(d)):
            ks = self.kin["spot"]
            ka = self.kin["ce" if bull else "pe"]
            view.evidence.append(Evidence(
                "VIS inflection snap", True,
                f"spot v{ks.v:+.2f} a{ks.a:+.3f} j{ks.j:+.4f} · "
                f"prem a{ka.a:+.3f} · drop {drop_in:+.0f} into zone"))
            view.state = "FIRE"
            return view

        if (view.ok_count >= need and score_ok and cheap and pressure_ok
                and turn_ok and exhaustion_ok):
            h.sustain += 1
            view.sustain = h.sustain
            if h.sustain >= sustain_need:
                view.state = "FIRE"
                return view
            view.state = "CONFIRMING"
            return view

        # BANK-LED EARLY ENTRY (task #37, flag-gated, default off): the normal
        # bounce confluence above did NOT fire — usually because Nifty's own
        # turn-pivot (RAIL#2) or full ok_count hasn't printed yet. If BankNifty is
        # LEADING us, selling pressure has flipped (RAIL#1) and price exhausted into
        # the zone (RAIL#3) and we can still buy cheap, fire NOW on a reduced bar —
        # acting WHILE the move forms instead of after Nifty's slow confluence.
        # RAIL#1 + RAIL#3 + cheapness stay hard floors so it can't buy a falling
        # knife or a mid-rally pullback. The cure a relaxation could not deliver.
        if (config.BANK_LED_ENTRY_ON and lead > 0 and pressure_ok
                and exhaustion_ok and cheap and score_ok
                and view.ok_count >= config.BANK_LED_MIN_OK):
            h.sustain += 1
            view.sustain = h.sustain
            view.evidence.append(Evidence(
                "BANK-LED early entry", True,
                f"BankNifty leads · {view.ok_count}/{config.BANK_LED_MIN_OK} bar"))
            if h.sustain >= config.BANK_LED_SUSTAIN:
                view.state = "FIRE"
                return view
            view.state = "CONFIRMING"
            return view

        h.sustain = 0
        view.sustain = 0
        if not cheap and view.ok_count >= config.EVIDENCE_NEED:
            view.state = "ARMED"
            view.evidence.append(Evidence(
                "Cheapness", False,
                f"premium {our_ltp:.1f} > low {h.premium_low:.1f} + "
                f"{cap:.0f} — too late, waiting"))
            return view
        view.state = "ARMED"
        return view

    # ── zone break bookkeeping (role reversal) ───────────────────────────────
    def _mark_breaks(self, spot: float, now: float):
        for z in list(self.oi.support_zones):
            if spot < z.level - config.ZONE_BREAK_PTS:
                if self.flipped_zones.get(z.level, ("", 0))[0] != "resistance":
                    self.flipped_zones[z.level] = ("resistance", now)
        for z in list(self.oi.resistance_zones):
            if spot > z.level + config.ZONE_BREAK_PTS:
                if self.flipped_zones.get(z.level, ("", 0))[0] != "support":
                    self.flipped_zones[z.level] = ("support", now)

    # ── held-position conviction ─────────────────────────────────────────────
    # (user: "after taking the trade it should build or lose conviction from
    #  everything it keeps reading, and tell me to keep holding or be
    #  prepared to square off")
    def position_conviction(self, direction: str) -> dict:
        """Re-reads the market FOR the held position every analytics pass.
        Nine live factors → verdict. Also cached so live_score() feeds the
        trailing logic with a real number instead of a binary."""
        spot, fut, atm, ce_ltp, pe_ltp = self.prices.freeze_core()
        bull = (direction == "CE")
        zone = self._entry_zone.get(direction, 0.0)
        factors: List[Evidence] = []

        # 1. entry zone intact
        intact = True
        if zone > 0:
            if bull and spot < zone - config.ZONE_BREAK_PTS:
                intact = False
            if not bull and spot > zone + config.ZONE_BREAK_PTS:
                intact = False
        factors.append(Evidence("Entry zone intact", intact,
                                f"zone {zone:.0f}" if zone else "no zone ref"))

        # 2/3. premium velocities
        v_our = self.prem.velocity(direction)
        v_opp = self.prem.velocity("PE" if bull else "CE")
        factors.append(Evidence("Our premium rising", v_our > 0,
                                f"{v_our:+.2f}/s"))
        factors.append(Evidence("Their premium weak", v_opp <= 0,
                                f"{v_opp:+.2f}/s"))

        # 4. futures flow with us
        s60 = self.flow.cvd.slope(60)
        factors.append(Evidence("Futures flow with us",
                                s60 > 0 if bull else s60 < 0,
                                f"CVD {s60:+.0f}/s"))

        # 5. positioning fuel
        quad = self.flow.fut_oi.quadrant
        fuel = (quad in ("SHORT_COVERING", "LONG_BUILDUP") if bull
                else quad in ("LONG_UNWINDING", "SHORT_BUILDUP"))
        factors.append(Evidence("Unwinding fuel", fuel,
                                quad.replace("_", " ").lower()))

        # 6. AVWAP side
        av = self.flow.avwap
        f_ref = fut or spot
        av_ok = (av.from_high > 0 and f_ref > av.from_high) if bull \
            else (av.from_low > 0 and f_ref < av.from_low)
        factors.append(Evidence("AVWAP side", av_ok, "trapped traders ours"))

        # 7. near-ATM PCR shifting our way (the user's key metric)
        d_pcr = self.oi.near_pcr_change(180)
        factors.append(Evidence("ATM±6 PCR shift",
                                d_pcr >= 0.03 if bull else d_pcr <= -0.03,
                                f"{self.oi.near_pcr:.2f} ({d_pcr:+.3f}/3m)"))

        # 8. order book still ours
        right = "call" if bull else "put"
        bq = self.prices.opt_bqty.get((atm, right), 0.0)
        aq = self.prices.opt_aqty.get((atm, right), 0.0)
        factors.append(Evidence("Book with us", aq > 0 and bq / aq >= 1.2,
                                f"{bq / aq if aq else 0:.1f}×"))

        # 9. heavyweights still pulling our way
        sent = self.basket.sentiment
        factors.append(Evidence("Heavyweights with us",
                                sent >= 52 if bull else sent <= 48,
                                f"basket {sent:.0f}/100"))

        # 10. sister indices still with us (BankNifty ≈ 35% of Nifty's pull)
        factors.append(self._sister_alignment(bull))

        # 11. premium force intact (2nd derivative) — a premium still rising
        # but DECELERATING hard is a move dying before the reversal prints;
        # this factor degrades conviction before your eyes see the turn
        ka = self.kin["ce" if bull else "pe"]
        factors.append(Evidence("Premium force intact", ka.a > -0.05,
                                f"v {ka.v:+.2f}/s · a {ka.a:+.3f} pts/s²"))

        # 12. opposite side quiet — if the OTHER direction's hunter is
        # confirming at a zone while we hold, conviction must NOT read
        # "strong" (user caught a CE exit flipping instantly to PE entry
        # while the panel still said hold — the panel was blind to this)
        opp_view = (self.last.pe if direction == "CE" else self.last.ce)
        opp_threat = (opp_view.state in ("CONFIRMING", "FIRE")
                      or (opp_view.state == "ARMED"
                          and opp_view.ok_count >= config.EVIDENCE_NEED))
        factors.append(Evidence("Opposite side quiet", not opp_threat,
                                f"{'PE' if direction == 'CE' else 'CE'} hunter "
                                f"{opp_view.state.lower()}"
                                f" {opp_view.ok_count}/{opp_view.needed}"))

        ok = sum(1 for f in factors if f.ok)
        total = len(factors)
        raw = ok / total

        # CONVICTION IS SMOOTHED, NOT TWITCHY (user: "if it is conviction it
        # should not flip-flop"). Raw factors are instantaneous and noisy —
        # real conviction builds and erodes over ~20 s, so:
        #   1. EMA of the factor fraction (≈12 s half-life at 1 Hz)
        #   2. hysteresis: the verdict only changes when the smoothed value
        #      crosses a band by a clear margin — no oscillation at an edge.
        # Zone break is the one exception: that snaps to danger instantly.
        if self._conv_ema is None:
            self._conv_ema = raw
        else:
            self._conv_ema += 0.08 * (raw - self._conv_ema)
        frac = self._conv_ema

        if not intact:
            tone = "danger"
            verdict = "SQUARE-OFF ALERT — thesis zone broken"
        else:
            prev = self._conv_tone or "ok"
            order = ["danger", "warn", "ok", "strong"]
            lo_edge = {"strong": 0.60, "ok": 0.42, "warn": 0.28}
            # target tone from plain bands
            tone = ("strong" if frac >= 0.60 else
                    "ok" if frac >= 0.42 else
                    "warn" if frac >= 0.28 else "danger")
            # hysteresis: require a 0.05 overshoot to CHANGE level
            if tone != prev:
                if order.index(tone) > order.index(prev):      # upgrading
                    if frac < lo_edge.get(tone, 0) + 0.05:
                        tone = prev
                else:                                          # downgrading
                    if frac > lo_edge.get(prev, 1) - 0.05:
                        tone = prev
            verdict = {
                "strong": "KEEP HOLDING — conviction strong",
                "ok": "HOLD — conviction adequate",
                "warn": "CAUTION — conviction fading, be ready",
                "danger": "BE PREPARED TO SQUARE OFF — conviction lost",
            }[tone]
        self._conv_tone = tone

        self._pos_conv = {
            "direction": direction, "ok": ok, "total": total,
            "frac": round(frac, 2), "verdict": verdict, "tone": tone,
            "factors": [{"name": f.name, "ok": f.ok, "detail": f.detail}
                        for f in factors],
        }
        return self._pos_conv

    def live_score(self, direction: str) -> float:
        """Conviction fraction for the held direction (feeds trail weakening
        via SCORE_WEAK). Falls back to zone-intact binary pre-first-compute."""
        if self._pos_conv.get("direction") == direction:
            return float(self._pos_conv.get("frac", 1.0))
        zone = self._entry_zone.get(direction, 0.0)
        if zone <= 0:
            return 1.0
        spot = self.prices.spot
        if direction == "CE" and spot < zone - config.ZONE_BREAK_PTS:
            return 0.0
        if direction == "PE" and spot > zone + config.ZONE_BREAK_PTS:
            return 0.0
        return 1.0

    def note_entry(self, direction: str):
        """Freeze the fired zone as the position's thesis reference."""
        self._entry_zone[direction] = self._hunt[direction].zone_level
        self._hunt[direction].sustain = 0
        self._pos_conv = {}
        self._conv_ema = None      # conviction starts fresh per trade
        self._conv_tone = ""

    def note_stop(self, direction: str):
        """A stop-loss at this zone — mark it burned for the day."""
        zone = self._entry_zone.get(direction, 0.0)
        if zone > 0:
            key = (direction, round(zone / 5) * 5)
            self.burned[key] = self.burned.get(key, 0) + 1
        self._stops.append((direction, clk.now()))

    def whipsaw_active(self) -> bool:
        """≥2 stop-losses within the window, the latest still fresh — the
        market is chopping through both sides; demand overwhelming evidence
        from everything until 5 calm minutes pass."""
        now = clk.now()
        recent = [t for _, t in self._stops
                  if now - t <= config.WHIPSAW_WINDOW_SEC]
        return (len(recent) >= config.WHIPSAW_STOPS
                and now - recent[-1] <= config.WHIPSAW_COOL_SEC)

    def note_exit(self):
        """ANY exit: every hunt must rebuild its sustained confirmation from
        zero. Kills the instant CE→PE flip — an opposite entry after an exit
        re-earns its full multi-second confirmation with live evidence."""
        for h in self._hunt.values():
            h.sustain = 0

    def reset_session(self):
        self.prem.reset()
        for h in self._hunt.values():
            h.reset()
        self.flipped_zones.clear()
        self.burned.clear()
        self._spot_window.clear()
        self.last = Decision()
