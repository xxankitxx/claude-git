"""
MYTHOS — Configuration
======================
Every tunable of the system lives here. Nothing is buried in module code.

Design decisions (first-principles, not inherited defaults):
  * Trade scope     : NIFTY weekly options ONLY (user decision 2026-06-12).
                      Bank Nifty / Fin Nifty lost weekly expiries — monthly
                      contracts don't suit 10-12 point premium scalps.
  * Execution       : PAPER ONLY. This system never places a real order.
  * Heavyweights    : analyzed as *inputs* to the Nifty decision (sentiment,
                      constituent-implied S/R), never traded.
  * Expiries        : NSE moved all index derivative expiries to TUESDAY
                      (weekly Nifty = every Tuesday, monthly = last Tuesday).
                      Auto-computed below; override via EXPIRY_OVERRIDE when a
                      trading holiday shifts an expiry.
"""

import os
from datetime import datetime, timedelta, timezone, date

IST = timezone(timedelta(hours=5, minutes=30))

_DIR      = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR  = os.path.dirname(_DIR)
DATA_DIR  = os.path.join(_DIR, "data")
LOG_DIR   = os.path.join(_DIR, "logs")
ARCHIVE_DIR = os.path.join(_DIR, "archive")
ASSETS_DIR  = os.path.join(_DIR, "assets")
STATIC_DIR  = os.path.join(_DIR, "static")
DB_PATH     = os.path.join(DATA_DIR, "mythos.db")
TRADES_JSON = os.path.join(DATA_DIR, "trades_today.json")
ARCHIVE_PREFIX = ""   # becomes "sim_" in simulation so sim never touches live records

for _d in (DATA_DIR, LOG_DIR, ARCHIVE_DIR, ASSETS_DIR):
    os.makedirs(_d, exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# INSTRUMENT
# ═════════════════════════════════════════════════════════════════════════════
STOCK_CODE     = "NIFTY"        # Breeze code for the index
SPOT_FRAGMENT  = "NIFTY 50"     # WS tick name fragment identifying the spot
EXCHANGE       = "NFO"
STRIKE_STEP    = 50
LOT_SIZE       = 65             # confirmed by user 2026-06-12 (broker shows 65)
NUM_STRIKES    = 8              # subscribe ATM ± 8 strikes (CE+PE = 34 feeds)

# Expiry overrides — set to "YYYY-MM-DD" string to force, else None for auto.
EXPIRY_OVERRIDE         = "2026-06-23"  # weekly options expiry (next Tuesday; set 2026-06-16)
FUTURES_EXPIRY_OVERRIDE = None  # monthly futures expiry


def _next_tuesday(today: date) -> date:
    """Next Tuesday including today (weekly Nifty expiry day since Sep-2025)."""
    days_ahead = (1 - today.weekday()) % 7   # Tuesday = weekday 1
    return today + timedelta(days=days_ahead)


def _last_tuesday_of_month(today: date) -> date:
    """Monthly derivative expiry = last Tuesday of the month. If already past,
    roll to last Tuesday of next month."""
    def last_tue(year: int, month: int) -> date:
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() != 1:
            d -= timedelta(days=1)
        return d
    lt = last_tue(today.year, today.month)
    if lt < today:
        m, y = (today.month % 12) + 1, today.year + (1 if today.month == 12 else 0)
        lt = last_tue(y, m)
    return lt


def expiry_date() -> str:
    """Weekly options expiry as YYYY-MM-DD. After 15:30 on expiry Tuesday the
    contracts are dead — roll to next week (teardown finding: the system was
    subscribing expired strikes on Tuesday evenings)."""
    if EXPIRY_OVERRIDE:
        return EXPIRY_OVERRIDE
    now = datetime.now(IST)
    d = _next_tuesday(now.date())
    if d == now.date() and (now.hour * 60 + now.minute) >= (15 * 60 + 30):
        d = d + timedelta(days=7)
    return d.strftime("%Y-%m-%d")


def futures_expiry_date() -> str:
    """Monthly futures expiry as YYYY-MM-DD."""
    if FUTURES_EXPIRY_OVERRIDE:
        return FUTURES_EXPIRY_OVERRIDE
    return _last_tuesday_of_month(datetime.now(IST).date()).strftime("%Y-%m-%d")


_MONTHS_EN = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def ws_expiry(d: str) -> str:
    """Breeze websocket expiry format: 16-Jun-2026. Built from an explicit
    English month list — strftime('%b') is LOCALE-dependent and on a
    non-English Windows locale would yield e.g. 'juin', making every option
    subscription silently fail to resolve a token (audit finding)."""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day:02d}-{_MONTHS_EN[dt.month]}-{dt.year}"


def rest_expiry(d: str) -> str:
    """Breeze REST expiry format: 2026-06-16T06:00:00.000Z."""
    return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%dT06:00:00.000Z")


def is_expiry_day() -> bool:
    return datetime.now(IST).strftime("%Y-%m-%d") == expiry_date()


def expiry_dt_ist() -> datetime:
    """Expiry moment (15:30 IST on expiry day) — used for time-to-expiry in Greeks."""
    d = datetime.strptime(expiry_date(), "%Y-%m-%d")
    return d.replace(hour=15, minute=30, tzinfo=IST)


# ═════════════════════════════════════════════════════════════════════════════
# HEAVYWEIGHT BASKET  (analysis inputs — never traded)
# NSE symbol -> approximate Nifty weight %. Edit when index weights are
# rebalanced (NSE publishes monthly). ISEC codes are resolved at runtime via
# breeze.get_names() — no hardcoded broker codes.
# ═════════════════════════════════════════════════════════════════════════════
# Official weights from NSE Indexogram ind_nifty50.pdf dated 29-May-2026
# (top-10 exact; TCS/M&M/BAJFINANCE/HINDUNILVR estimated — outside the
# published top-10, refresh after NSE's semi-annual rebalance).
HEAVYWEIGHTS = {
    "HDFCBANK":   10.56,
    "ICICIBANK":   8.32,
    "RELIANCE":    8.27,
    "BHARTIARTL":  5.20,
    "LT":          4.43,
    "INFY":        3.77,
    "SBIN":        3.71,
    "AXISBANK":    3.42,
    "KOTAKBANK":   2.62,
    "ITC":         2.56,
    "TCS":         2.50,   # estimate
    "M&M":         2.40,   # estimate
    "BAJFINANCE":  2.20,   # estimate
    "HINDUNILVR":  1.80,   # estimate
}

# Sister indices read as SENTIMENT inputs (never traded): Bank Nifty moves
# ~35% of Nifty's weight; Fin Nifty overlaps it. Candidate Breeze codes are
# tried at startup and the first that answers is used (verified by preflight).
SENTIMENT_INDICES = {
    "BANKNIFTY": ["CNXBAN", "BANKNIFTY", "NIFBAN"],
    "FINNIFTY":  ["NIFFIN", "FINNIFTY", "NIFSER"],
}
SENTIMENT_POLL_SEC = 20.0       # REST poll cadence (2 calls / 20 s — tiny)

# Heavyweight option-chain REST polling: one stock every POLL_STAGGER seconds,
# round-robin (2 calls per stock: CE chain + PE chain). With 14 stocks and 20s
# stagger a full pass takes ~4.7 min at ~6 calls/min — far inside Breeze's
# 100 req/min limit even with the Nifty chain + VIX pollers running.
HW_POLL_STAGGER_SEC   = 20.0
NIFTY_CHAIN_POLL_SEC  = 90.0    # full Nifty chain refresh (OI beyond WS strikes)
VIX_POLL_SEC          = 60.0


# ═════════════════════════════════════════════════════════════════════════════
# CAPITAL & RISK  (Requirement §7.3, §11)
# ═════════════════════════════════════════════════════════════════════════════
STARTING_CAPITAL  = 100_000.0   # resets every trading day at 09:15
MAX_OPEN          = 1           # one position at a time — option buyer scalping
SL_POINTS         = 10.0        # hard stop: entry premium − 10
TARGET_POINTS     = 12.0        # baseline target: entry + 12 (dynamic ratchets up)
# RISK CONTROL (live-money safety — review finding). Sizing by full buying power
# made one −10 stop a ~25% capital hit (4 stops ≈ ruin), and the −10 cannot even
# be honored on a fast gap-through. Size by RISK instead: a single −SL_POINTS
# loss is capped to RISK_PER_TRADE_FRAC of current capital. Dial up if you want
# more aggression — but know that raises blow-up risk.
LIVE_ORDERS         = False     # PAPER ONLY — never place a real broker order.
                                 # Load-bearing invariant; do NOT flip to True
                                 # until replay.py shows positive expectancy on
                                 # >=3 real recorded sessions (config provenance).
RISK_PER_TRADE_FRAC = 0.03      # target max loss per trade ≈ 3% of capital (a
                                 # 3×-slippage budget, not an absolute cap — a
                                 # deep gap fills at market; the notional cap +
                                 # intra-trade equity stop below are the hard rails)
DAILY_MAX_LOSS_FRAC = 0.04      # halt NEW entries AND force-flatten once the day's
                                 # realized+unrealized loss hits this (was 0.06 —
                                 # the breaker was pre-entry only and a day closed
                                 # -11.5%; now it is a true equity stop, see trader)
STOP_SLIPPAGE_MULT  = 3.0       # size assuming a stop can SLIP/GAP to 3× SL_POINTS
                                 # (was 2.0 — the lifecycle's worst fill was -29,
                                 # which at 2× was 3.8% > the 3% target; 3× keeps
                                 # even a -30 fill inside budget). Premiums gap
                                 # THROUGH the -10 stop on jumps; sizing for it is
                                 # the primary defense.
NOTIONAL_CAP_FRAC   = 0.50      # qty × entry_premium ≤ this × capital. Bounds the
                                 # PREMIUM-COLLAPSE tail: without it, sizing is
                                 # premium-independent so a high-premium option
                                 # collapsing toward zero could be a 45% single-
                                 # trade hit (audit finding). This caps total
                                 # exposure so even a full collapse is bounded.
MAX_ENTRIES_PER_DAY = 8         # soft anti-overtrading cap on a non-edge chop day
                                 # (one position at a time + cooldowns already, but
                                 # nothing capped total round-trips). 8 is ample to
                                 # gather replay data; beyond that is theta bleed.
# USER'S PROFIT RULE v4 (2026-06-14, verbatim): "hold on to a trade EITHER if
# it books a loss of 10 points OR it books a minimum profit of 12 points. The
# minimum profit has to be booked only after you have given it enough time to
# run. If it still doesn't run, there is a problem in the trade or the SIM."
# => BINARY outcome. The ONLY exits are the −10 hard stop and a +12-minimum
# trail. There is NO small-profit banking (the progressive lock and breakeven
# guard are REMOVED). Below the trail the trade is HELD (only −10 acts), giving
# it time to run to +12. Once peak ≥ TRAIL_ACTIVATE the floor secures ≥ +12 and
# the chandelier lets it run HIGHER (protect ~70% of peak). A trade that neither
# reaches the trail nor hits −10 is a NON-RUNNER — a signal the entry or the sim
# is wrong (fixed at the source), NOT a reason to bank a scrap.
TRAIL_ACTIVATE    = 14.0        # the +12 floor arms once peak ≥ +14 (2-pt buffer
                                 # over the lock avoids the instant-lock bug; and
                                 # "given time to run" = it must reach +14 first)
TRAIL_INITIAL_LOCK = 12.0       # floor = entry+12 → the minimum profit, secured
EXEC_SLIPPAGE     = 1.0         # assumed fill slippage on stop exits (pts)

MIN_PREMIUM       = 40.0        # don't buy lottery tickets
MAX_PREMIUM       = 350.0       # don't blow the whole capital on 1-2 lots
MAX_SPREAD        = 2.0         # bid/ask wider than this = illiquid, skip
# STRIKE SELECTION — ATM by DEFAULT (user: "I trade the ATM generally, or just
# OTM if my conviction is very strong"). Pick the strike whose |delta| is closest
# to the target; the target is ATM unless this entry's conviction is very strong.
STRIKE_TARGET_DELTA_ATM    = 0.50   # default — ATM
STRIKE_TARGET_DELTA_STRONG = 0.45   # just-OTM, ONLY when conviction is very strong
STRIKE_TARGET_DELTA = STRIKE_TARGET_DELTA_ATM   # back-compat alias (default/test callers → ATM)
# "very strong" ⇔ entry_score (= ok_count / len(evidence)) ≥ this. SIM2 autopsy
# showed 0.60 == the median score → 2/3 of trades went OTM, the opposite of "ATM
# generally". Raised to 0.75 so OTM is genuinely RARE (only top-decile conviction):
# a BOUNCE now needs ~11/14, a BREAK 4/5 to earn the cheaper OTM strike.
STRIKE_OTM_CONVICTION = 0.75
STRIKE_DELTA_BAND   = (-1, 4)   # candidate offsets scanned: 1 ITM .. 3 OTM
# ── ATM/ITM-THAT-MOVES strike mode (user 2026-06-17: "I need ATM or ITM which will
#    MOVE", after two live trades came OTM). Default OFF → select_strike is BYTE-
#    IDENTICAL; ON kills the (anti-predictive) OTM-on-conviction step, targets a
#    delta at/just-inside the money, rounds TOWARD the money, and floors |Δ|≥0.50.
#    Strike SELECTION only — never direction/sizing/exit. A/B-proven per-tape first.
STRIKE_FORCE_ATM_OR_ITM   = True    # ON 2026-06-17 (user sign-off): every entry ATM/ITM Δ≥0.50,
                                     # never OTM; thin-book-only-OTM is skipped. Strike-correctness
                                     # fix, NOT a P&L claim — verified P&L-neutral, edge still open.
STRIKE_TARGET_DELTA_LIVE  = 0.55    # ATM-to-just-ITM target used only when ON (0.55 A/B-best)
STRIKE_DELTA_BAND_ATM_ITM = (-2, 2) # flag-ON scan band: 2 ITM .. 1 OTM (toward the money)
STRIKE_ITM_BONUS          = 0.02    # cost tiebreak: at/inside-money strike preferred on a delta tie
STRIKE_ITM_PUSH_MIN_PREMIUM = 80.0  # toward-money push engages only when the ATM premium is this
                                     # rich (so the lot-cancelled edge exists; on cheap chains the
                                     # push is a no-op — that's a sizing pathology, out of scope)
STRIKE_MIN_DELTA_TO_ENTER = 0.50    # ON-mode safety: if the book is so thin that NO at/inside-money
                                     # strike quotes (achieved |Δ| stays below this after the walk),
                                     # SKIP the entry rather than send an OTM order. Inert on liquid
                                     # Nifty (every 50-pt strike quotes); a thin-book net only.


# ═════════════════════════════════════════════════════════════════════════════
# CHEAPER ENTRY — try cheap, but NEVER miss the move (user mandate: "you have a
# conviction to enter a trade, enter it. Just try to buy cheap but don't miss
# the move and don't confuse me").  When conviction fires, MYTHOS works a BUY
# LIMIT just below the LTP for a few seconds — if the option dips to it we get
# the cheaper fill; if it doesn't, we TAKE IT AT MARKET so the trade is ALWAYS
# taken (no lapse, no missed move).  Until the fill actually happens there is NO
# entry chime and NO position shown — the panel shows a distinct "WORKING ENTRY"
# state, never a phantom trade.  Sizing is on the limit so full-capital lot math
# reflects the (cheaper) fill.
# ═════════════════════════════════════════════════════════════════════════════
ENTRY_LIMIT_OFFSET_PTS  = 2.0    # bid this many pts under LTP …
ENTRY_LIMIT_OFFSET_FRAC = 0.015  # … or 1.5% of LTP, whichever is larger
ENTRY_LIMIT_MIN_OFFSET  = 1.0    # never tighter than this (a tick of safety)
ENTRY_LIMIT_WAIT_SEC    = 5.0    # work the cheaper limit this long, THEN take market
# KNIFE GUARD (cheap-entry fix): a "cheap" limit fill means premium fell to the
# limit — for a long option that is the underlying moving AGAINST us. Take it on a
# shallow dip (thesis intact), but HOLD the fill on a genuine knife: our-side
# premium in free-fall AND the live thesis turned against this side. It never
# lapses — it keeps working, so a clean market-take still happens at the window
# end (honours "always take the trade, don't miss the move"). Entry-fill only —
# touches NO SL / +12 / sizing.
KNIFE_GUARD_ON   = True    # hold a cheap fill that is a confirmed knife (default on)
KNIFE_PREM_VEL   = 0.6     # our-side premium fall (pts/s) past which a cheap dip is a knife
                          # (routine dips are ~-0.05/s, so 0.6 is a real collapse)
KNIFE_MIN_SCORE  = 0.30    # live conviction below this = thesis collapsed (a knife trigger)


# ═════════════════════════════════════════════════════════════════════════════
# ENRICHED ACTIVE-POSITION HEART  (anti-noise).  The position panel speaks in
# THREE slots: A = mood (owns colour/stance), B = the live "why" drawn from the
# 12 conviction factors, C = a flash when a SIGNIFICANT sudden event hits (Nifty
# or the basket).  These constants are the discipline that keeps it signal, not
# chatter — dwell/confirm hysteresis on B, hard cooldown + freshness + magnitude
# gates on C.  Empty slot C is the normal state; that emptiness is what makes a
# flash mean something.
# ═════════════════════════════════════════════════════════════════════════════
HEART_B_MIN_DWELL   = 8.0    # a "why" line can't be replaced for 8s
HEART_B_CONFIRM     = 3.0    # a NEW top factor must persist 3s before promotion
HEART_B_MAX_HOLD    = 14.0   # re-phrase same key after 14s so it doesn't read frozen
HEART_C_COOLDOWN    = 25.0   # at most one event flash per 25s (the key spam guard)
HEART_C_TTL         = 6.0    # a flash shows 6s then slot C empties
HEART_C_FRESH_SEC   = 12.0   # only flash events that JUST happened
HEART_C_MAG_BASE    = 2.0    # radar magnitude bar for a THREAT to our side
HEART_C_MAG_CONFIRM = 3.0    # higher bar for events CONFIRMING our side
HEART_C_MAG_BASKET  = 1.3    # multiplier on the bar for non-NIFTY instruments


# ═════════════════════════════════════════════════════════════════════════════
# SESSION TIMING (IST)
# ═════════════════════════════════════════════════════════════════════════════
MARKET_OPEN   = (9, 15)
MARKET_CLOSE  = (15, 30)
AVOID_FIRST_MINS = 5            # Requirement §7.1 — no trades in first 5 min
NO_ENTRY_AFTER   = (15, 15)     # no NEW positions after this (raised from 14:30 on
                                 # 2026-06-15: the user never asked to stop at 2:30 —
                                 # 14:30 silently killed all afternoon entries. 15:15
                                 # leaves a 10-min buffer before EOD_FLATTEN 15:25 so a
                                 # fresh -10/+12 trade still has room to resolve. The
                                 # blocked reason IS surfaced on the dashboard, not silent.
EOD_FLATTEN      = (15, 25)     # force-close any open position


# ═════════════════════════════════════════════════════════════════════════════
# ZONE-HUNTER ENTRY ENGINE  (user doctrine 2026-06-12: "identify strong zones,
# build conviction with the price movement, enter at the cheapest price where
# most factors favour the trade — NO minimum grading system")
# The old 0.70 weighted-score gate is deleted: it confirmed momentum late and
# bought inflated premium. Entries now happen AT defended zones (bounce) or at
# the snap of a defended wall (break).
# ═════════════════════════════════════════════════════════════════════════════
SCORE_WEAK        = 0.40        # held-position zone-health below this → tighten
                                 # (live_score is 1.0 zone intact / 0.0 broken)

ZONE_MIN_STRENGTH = 0.50        # OI zone strength needed to qualify as "strong"
ZONE_BAND         = 20.0        # pts around the level that count as a touch
ZONE_SIDE_TOL     = 5.0         # a zone must be on the CORRECT side of spot to
                                 # qualify: CE hunts supports at/below spot, PE
                                 # resistances at/above. The old code let a
                                 # "support" up to ZONE_BAND (20pt) ABOVE spot
                                 # qualify for CE — that is a RESISTANCE, and
                                 # the entry forensic traced 47% of CE fires to
                                 # exactly this wrong-side geometry (chasing the
                                 # top). Only a few pts of tolerance for the
                                 # touch band; never the whole band.
ZONE_STALK_DIST   = 60.0        # start stalking when price is this close
ZONE_BREAK_PTS    = 25.0        # beyond this the zone is broken → ROLE FLIPS
                                 # (broken support acts as resistance, etc.)
EVIDENCE_NEED     = 6           # "most factors": ≥6 of 14 defense evidences (was
                                 # 5 — raised for the unproven-edge live window:
                                 # fewer, higher-conviction setups = cleaner replay
                                 # data and less theta bleed on a coin-flip)
EVIDENCE_SUSTAIN  = 1           # the CONFIRMED-PIVOT rail (retrace held
                                 # TURN_HOLD_SEC) already IS a multi-second
                                 # confirmation, so stacking extra sustain on top
                                 # just delayed entry ~2s deeper into the bounce —
                                 # the proven chasing cause (forensic: fired +5.7
                                 # into a move). Fire as soon as pivot+evidence+
                                 # cheap+pressure align; the held pivot is the
                                 # multi-tick guard, not a single-tick noise fire.
SUSTAIN_BURNED    = 3           # burned-zone / whipsaw entries demand a FULL 3 s
                                 # confirm — decoupled from EVIDENCE_SUSTAIN so
                                 # tuning normal entries can't quietly relax the
                                 # chop guard (review finding).
# CONFIRMED-PIVOT turn gate (the mandatory, noise-robust reversal signal). A
# reversal is real only when price has retraced off the touch extreme AND held
# without re-making it — NOT a 2nd-derivative blip (an adversarial test proved
# the deceleration proxy fired on ~1/3 of mid-fall ticks, i.e. into knives).
TURN_CONFIRM_PTS  = 1.5         # pts price must retrace off the extreme (enter
                                 # nearer the touch — the sim's support bounce is
                                 # small/slow, so a +2 confirm ate the whole edge)
TURN_HOLD_SEC     = 2.0         # ...and hold without a fresh extreme this long
                                 # (this is the knife guard — keep it)
EXHAUSTION_DROP_PTS = 12.0      # price must have FALLEN this far into a CE support
                                 # (risen into a PE resistance) before we'll buy
                                 # the turn — the doctrine's exhaustion REVERSAL,
                                 # not a shallow trend-pullback higher-low (which
                                 # the forensic proved the engine bought & lost on)
# ── ENTRY-CURE EXPERIMENT FLAGS (default OFF — the live entry path is BYTE-
#    IDENTICAL until one is flipped). Each gates a SELECTION change behind an
#    off-by-default switch so a recorded day can be A/B'd via `replay.py --set
#    FLAG=...` without touching live behaviour. Slate generated + screened
#    2026-06-14 (entry-cure-search). DO NOT enable for live without a replay-
#    proven net/PF win AND re-proof on a faithful LIVE recording AND user sign-off.
DROP_PREMIUM_RISING_VOTE  = False   # #6: stop the "premium rising" momentum vote
                                     #     (#3) counting toward ok_count — it turns
                                     #     true only once the move has already left.
ENTRY_SCORE_CEILING       = 0       # #4: 0=off; when >0 VETO a bounce FIRE whose
                                     #     ok_count EXCEEDS it — drops the anti-
                                     #     predictive high-evidence cohort (WR
                                     #     collapses 0.7→0%, 0.8→18%).
VETO_BOUNCE_IN_TREND      = False   # #2: refuse BOUNCE fires while regime==TRENDING
                                     #     (the ~70% with-move chase); BREAK fires
                                     #     untouched (legitimate trend-day play).
EXHAUSTION_PRIOR_SWING_ON = False   # #1: anchor the exhaustion drop to a real prior
                                     #     swing over EXHAUSTION_SWING_WINDOW_SEC,
                                     #     not the freshly-reset local approach high
                                     #     (root cause of a shallow rally faking 12pt).
EXHAUSTION_SWING_WINDOW_SEC = 120.0 # trailing spot window for the prior-swing anchor

# ── LIVE ENTRY CURE (#5 structural-purity) — ACTIVE for live PAPER validation ──
#    (2026-06-15, user-directed). The cure search's best lead: tighten the touch
#    band so h.spot_extreme is a TRUE rejected level, and require a REAL 4pt
#    rejection (not a 1.5pt wiggle). Replay-proven across both recorded days, BUT
#    the edge is UNPROVEN as robust — on the hard day its sim profit was outlier-
#    carried (1-2 runner wins), and the dominant live risk (a tight band missing
#    fast touches at the 1 Hz analytics cadence) is PHYSICALLY untestable on a 1 Hz
#    sim tape. So this is a LIVE PAPER VALIDATION run, NOT a proven cure — it places
#    NO real orders (LIVE_ORDERS stays False) and risks no capital.
#    BAND = 12, NOT 7: band 7 is on the wrong side of the 1 Hz-aliasing cliff (it
#    can silently miss touches live); band 12 is the guardrail-safe value and scored
#    BETTER on the sim. The two knobs MUST move together (each alone is null/weak).
#    ROLLBACK: set LIVE_ENTRY_CURE = False (restores baseline ZONE_BAND=20 /
#    TURN_CONFIRM_PTS=1.5 exactly). POST-SESSION A/B on the faithful live recording:
#      python replay.py <day> --set ZONE_BAND=20 --set TURN_CONFIRM_PTS=1.5 --dump
#    (replays the SAME real tape at baseline vs the live cure; --dump shows whether
#    any edge is broad or outlier-carried, and trade-count vs band=20 = touch-catch).
LIVE_ENTRY_CURE = True
if LIVE_ENTRY_CURE:
    ZONE_BAND        = 12.0
    TURN_CONFIRM_PTS = 4.0

# ── CROSS-INSTRUMENT LEAD (task #37, 2026-06-17) — flag-gated, default OFF =
#    byte-identical baseline. The user's repeated mandate: "the move is already
#    done when the trade comes — look at BankNifty/FinNifty/stocks and decide."
#    BankNifty is higher-beta and LEADS Nifty intraday; the basket sentiment
#    confirms. When the lead STRONGLY AGREES with the hunted side, a near-ready
#    Nifty setup (ok_count one short of the bar) fires ONE PASS EARLIER — but the
#    three safety rails (pressure/turn/exhaustion) stay AND-ed, so it CANNOT
#    re-open the knife-catching defect the audit closed. Replayable from recorded
#    idx_ltp/idx_ts + (price-only) basket sentiment. PROVE via replay --set first.
#    NOTE: implied_support/resistance + heavyweight flow are NOT on existing tapes
#    (dropped by replay), so this lead is BankNifty-momentum-primary by design.
CROSS_LEAD_ON          = False  # master switch (OFF -> _lead_vote()==0 -> no-op)
CROSS_LEAD_VETO        = False  # also demote a FIRE when the lead strongly DISAGREES
CROSS_LEAD_WIDEN_CAP   = False  # on strong-agree, widen the cheapness cap to CHEAP_CAP_STRONG
CROSS_LEAD_WINDOW_SEC  = 45.0   # momentum lookback (shorter than _sister_alignment's 180s)
CROSS_LEAD_BN_PCT      = 0.08   # |BankNifty %move| over the window to count as strong
CROSS_LEAD_BN_EDGE_PCT = 0.02   # BankNifty must LEAD Nifty's own %move by this much
CROSS_LEAD_SENT_HI     = 58.0   # basket sentiment >= this confirms a CE (bull) lead
CROSS_LEAD_SENT_LO     = 42.0   # basket sentiment <= this confirms a PE (bear) lead
# Basket sentiment is COINCIDENT (memory) and — critically — NOT reconstructable
# on the recorded tapes (heavyweight prev_close isn't revived, so sentiment is a
# constant-50 stub on replay). Requiring it made _lead_vote untestable (0 votes /
# 24k passes). BankNifty MOMENTUM is the genuine, replayable leader, so sentiment
# is an OPTIONAL confirmation, default OFF. Turn ON only after hw prev_close is
# recorded AND the sentiment-gated signal is proven on a tape that can move it.
CROSS_LEAD_REQUIRE_SENT = False  # require basket-sentiment confirmation (default off)
CROSS_LEAD_MIN_NEED    = 5      # floor on `need` after the -1 agree relaxation
# The -1 need shave above is a NO-OP on the recorded tapes (entries that don't
# fire fail the RAILS, not ok_count). CROSS_LEAD_RELAX_TURN is the genuine
# earliness lever: when BankNifty has strongly led AND RAIL#1 (CVD sign-flip)
# already holds, let the bank's confirmed turn STAND IN for Nifty's own not-yet-
# printed pivot (RAIL#2). RAIL#1 (pressure) and RAIL#3 (exhaustion) stay AND-ed,
# so it still cannot buy into active selling or mid-rally. Riskier (this is the
# knife-edge the audit guarded) — A/B separately, default OFF.
CROSS_LEAD_RELAX_TURN  = False  # substitute the strong lead for RAIL#2 (turn pivot)

# ── BANK-LED ENTRY PATH (task #37) — the genuine lateness fix, flag-gated OFF.
#    A SEPARATE entry trigger (peer to BOUNCE/BREAK): when BankNifty is LEADING
#    our way AND Nifty has exhausted into a zone (RAIL#3) AND selling pressure has
#    flipped (RAIL#1), fire on a REDUCED evidence bar WITHOUT waiting for Nifty's
#    own turn-pivot (RAIL#2) or full ok_count — i.e. act WHILE the move forms.
#    This is the only mechanism that actually fires earlier (a relaxation of the
#    monolithic bounce gate is provably inert — rails arrive together, late). It
#    is RISKIER (lower bar = the knife-catching edge the audit guarded), so it
#    keeps RAIL#1 + RAIL#3 + cheapness as hard floors and MUST clear a tape A/B
#    (more runners, not knife-catch stops; P&L not worse) before any live flip.
BANK_LED_ENTRY_ON = False  # master switch for the bank-led early-entry path
BANK_LED_MIN_OK   = 4      # reduced evidence bar (vs EVIDENCE_NEED=6) for a bank-led fire
BANK_LED_SUSTAIN  = 1      # passes the bank-led confluence must hold to fire

# ── VELOCITY-INFLECTION SNAP (task #38) — the SMART SINGLE LEADING entry, flag-
#    gated OFF = byte-identical. User mandate: the multi-evidence gate is late by
#    construction (it fires at the speed of the SLOWEST lagging confirmation; our
#    own data: evidence is ANTI-PREDICTIVE). VIS replaces the 6-of-15 vote +
#    held-pivot (RAIL#2) + sustain tally with ONE read at a strong exhausted zone:
#    spot's 2nd derivative has turned UP (accel>=floor while velocity still<=0 —
#    the turn caught AT the inflection, before any higher-low prints) AND our ATM
#    premium's own accel has turned up with velocity no longer bleeding (the SAME
#    buyers lifting futures + option together — a two-instrument discriminator an
#    EMA wiggle can't fake). Keeps exhaustion(RAIL#3)+zone-strength+cheap+CVD-flip
#    (RAIL#1) as HARD FLOORS so it can't buy a knife or a mid-rally pullback; the
#    −10 stop insures the early entries (user doctrine: "be ruthless early").
#    NOT a de-gate (proven to lose): it ADDS a sharper LEADING discriminator and
#    drops only the LAGGING confirmations. PROVE on the faithful tape before live.
VIS_ENTRY_ON          = False  # master switch (OFF -> legacy fire predicate only)
VIS_SPOT_A_MIN        = 0.030  # spot accel floor — 2.5x the 0.012 EMA noise floor
VIS_SPOT_J_MIN        = 0.0    # jerk>=this (mirror for PE): upturn must still be BUILDING
VIS_PREM_A_MIN        = 0.0    # our ATM premium accel must be >= this (turning up)
VIS_PREM_V_TOL        = 0.05   # allowed residual premium bleed (pts/sample): v_our >= -tol
VIS_KEEP_PRESSURE     = True   # retain RAIL#1 (CVD 30s sign-flip) as the one kept floor
VIS_PREM_READY_PASSES = 3      # suppress fire for N passes after an ATM roll (cold premium)
VIS_MIN_OK            = 0      # context floor checked AT the inflection (NOT waited-for, so
                               # no added lateness): refuse a NAKED inflection on a weak-evidence
                               # zone (the dead-cat signature — a strong inflection the legacy gate
                               # rejected on low ok_count). 0 = off; a modest 3-4 filters dead-cats
                               # while keeping strong-context turns. NOT the late 6-vote gate.

# ── CROSS-INSTRUMENT CO-EQUAL CONSENSUS GATE (task #39, 2026-06-19) — USER
#    MANDATE, DEFAULT ON. The broad tape — BankNifty/FinNifty (day momentum + OI),
#    the heavyweight stock basket (price + PCR + walls, folded into sentiment),
#    futures FLOW, price-action TREND, and Nifty OI STRUCTURE — is fused by the
#    SINGLE-SOURCE consensus_core.py (BREADTH carries the sisters+stocks CO-EQUAL
#    with FLOW/TREND) into a net C in [-1,+1] with a CONTESTED measure. A Nifty
#    FIRE that fights a CONFIDENT, NON-SPLIT bloc is DEMOTED to CONFIRMING — the
#    cross-instrument bloc gets veto power equal to Nifty's own signal. Computed
#    from ALWAYS-DEFINED inputs every pass, so it ACTS (unlike the inert CROSS_LEAD
#    spike gate). DEMOTE-ONLY: it can NEVER fabricate a trade; sizing/exit untouched.
#    Rollback = CONSENSUS_GATE_ON=False (byte-identical: consensus_core not imported).
CONSENSUS_GATE_ON    = True    # master switch (False = no demote = byte-identical)
CONSENSUS_MIN        = 0.30    # |C| the bloc must reach to veto the opposing side
CONTESTED_MAX        = 0.55    # board more split than this → bloc ABSTAINS (no veto)
CONSENSUS_GATE_BREAK = False   # also veto BREAK fires (default off: trend-day thrust runs)

CHEAP_CAP_PTS     = 4.0         # premium must be within +4 of its zone-touch
                                 # low — the "cheapest possible price" law.
                                 # WAS 10: the entry forensic proved +10 (≈20
                                 # spot-pts at ATM delta) let the engine BUY THE
                                 # TOP — CE fired on avg +5.5pt into a bounce and
                                 # then mean-reverted against it. +4 forces the
                                 # entry near the turn, as the doctrine demands.
# FAST PATH for genuine V-reversals (user: "not catching real 30-35 pt moves"):
# when the defense is OVERWHELMING, a real leg is igniting — waiting 3 s and
# demanding the bottom-tick price means missing exactly the best trades.
EVIDENCE_STRONG   = 7           # ≥7 of 14 = overwhelming defense
SUSTAIN_STRONG    = 1           # overwhelming defense + a confirmed-pivot rail
                                 # (held TURN_HOLD_SEC) fires in one pass — the
                                 # pivot is the multi-tick guard, so this is not a
                                 # single-tick noise fire (the earlier concern).
CHEAP_CAP_STRONG  = 5.0         # allow +5 from zone-low when overwhelming (was
                                 # 15, then 6 — tightened toward the +4 normal cap
                                 # so even strong setups enter near the pivot)
BREAK_BEYOND      = 10.0        # break entry arms once spot is 10 pts past wall
BREAK_CHASE_MAX   = 30.0        # ...and never chases beyond 30 pts past it
BREAK_THRUST_NEED = 3           # ≥3 of 5 thrust evidences for a break entry
# NO-PROGRESS STALL: a scalp must RESOLVE. If the premium PEAK hasn't advanced
# for this many seconds AND the trade never reached the +6 profit floor AND the
# trail isn't protecting it, the thesis is dead — cut it (it only bleeds theta).
# A trade still making new highs keeps resetting its peak-clock, so genuine
# winners are never cut. Theta-aware: shorter window in the afternoon.
STALL_PROFIT_FLOOR    = 6.0       # UNUSED — binary doctrine (no stall kill)
STALL_NOPROGRESS_SEC  = 90.0      # UNUSED — binary doctrine: a non-runner rides to
STALL_NOPROGRESS_AFT  = 60.0      # UNUSED   −10 or +12, never a scratch. Kept defined
STALL_NOPROGRESS_LATE = 45.0      # UNUSED   only to avoid import breaks.
MIN_HOLD_SEC       = 30.0       # blind-hold: only the hard SL may exit before 30s
                                 # (user: "should have been held for 30 s at least")
HOLD_ESCAPE_PTS    = 12.0       # +12 ends the blind hold (profit lock may begin)

# Dynamic trailing — tiered chandelier (run7 v27/v28 lessons, kept on user
# demand to HOLD big moves instead of scalping out):
#   peak 6   → SL locks at entry+1 (the requirement's minimum-6 guarantee)
#   peak ≥ 8 → chandelier engages: offset = max(floor, tier% × peak, optATR)
#       tier: ≤12 pts → 28% of peak (room to breathe into a runner)
#             ≤20 pts → 22%        (proven move, protect more)
#             >20 pts → 18%        (big runner, lock the bulk)
#       floor = max(3.5, 2.5% of entry premium) — scales with option price
#   trend agreement (futures CVD with the trade) widens offset ×1.35
#   thesis weakening tightens it ×0.6
TRAIL_ATR_PERIOD     = 14
TRAIL_MIN_OFFSET     = 3.5
CHANDELIER_MIN_PEAK  = 14.0     # chandelier runs once the +12 floor is armed
TRAIL_FLOOR_PCT      = 0.025
TRAIL_TREND_WIDEN    = 1.35
TRAIL_WEAK_TIGHTEN   = 0.6

# ─────────────────────────────────────────────────────────────────────────────
# PROVENANCE NOTE (read before trusting any threshold below).
# The rules in this block were shaped by a one-off forensic study of a SINGLE
# ~61-trade SIMULATION session. That sim is a closed loop (sim_feed.py derives
# price, premiums, OI and sister-index moves from one regime variable), and the
# session's trade file is overwritten every run — so those exact counterfactual
# numbers are NOT reproducible and are deliberately NOT quoted here as fact.
# General sim behaviour is roughly a coin-flip win rate on trend-rich synthetic
# tape. Therefore every value below is a PRIOR, not a proven optimum — it
# encodes a defensible trading ASYMMETRY, awaiting real out-of-sample
# calibration once the live SQLite store has accumulated genuine sessions.
# ─────────────────────────────────────────────────────────────────────────────

# Re-entry pauses — ASYMMETRIC by design intent: after a LOSS a fast re-attempt
# at the same turn is reasonable (15 s); after a WINNING trail exit the move's
# momentum has just stalled, so chasing it immediately is the worst window —
# wait 90 s. (Asymmetry is the principle; the exact seconds are priors.)
ENTRY_COOLDOWN_SEC        = 15.0   # after a losing exit
ENTRY_COOLDOWN_AFTER_WIN  = 90.0   # after a profitable (trail) exit

# PROGRESSIVE PROFIT LOCK (replaces the old "hold-first to +20 or scratch at
# entry+1" rule, which let a +15 winner round-trip to −10 or scratch — the user:
# "the profit taking is still very stupid"). From peak ≥ PROFIT_LOCK_START the
# stop RATCHETS UP to entry + (peak − PROFIT_GIVEBACK): a faded winner now BANKS
# most of its gain instead of giving it all back, while a still-climbing winner
# keeps raising its own floor (so it still runs — "give winners room"). Hands off
# smoothly to the +20 chandelier (peak 20 → entry+13 = TRAIL_INITIAL_LOCK).
#   peak +8 → lock entry+1   peak +12 → entry+5   peak +18 → entry+11
PROFIT_LOCK_START = 8.0            # start protecting once peak reaches +8
PROFIT_LOCK_FRAC  = 0.60           # lock entry + 60% of the peak (give back 40%).
                                   # Fractional, not a fixed giveback: a +8 peak
                                   # now banks +4.8 (not +1), a +15 peak banks +9
                                   # — directly fixes "profit taking is stupid".
                                   # Hands off to the +20 chandelier (which then
                                   # protects ~70%, tightening as the move grows).
BREAKEVEN_GUARD_PEAK = 10.0        # (legacy; superseded by the progressive lock)
BREAKEVEN_GUARD_LOCK = 1.0

# WHIPSAW GUARD — design intent: alternating CE/PE stop-outs in a tight window
# mean the tape is chopping through both sides; after 2 stops within 10 min,
# demand overwhelming evidence (no fast path) until 5 calm minutes pass.
WHIPSAW_STOPS      = 2
WHIPSAW_WINDOW_SEC = 600.0
WHIPSAW_COOL_SEC   = 300.0

# 'Pressure exhausting' (CVD deceleration) as REQUIRED evidence for bounce
# entries — design intent: do not buy a bounce while the selling pressure that
# made the low is still accelerating. In the sim study this separated winners
# from losers more than any other single evidence, but on ONE closed-loop
# session — treat as a strong prior, revalidate on ≥3 live sessions.
REQUIRE_PRESSURE_OK = True

# position safety nets (a position you cannot price is unmanageable):
STALE_QUOTE_EXIT_SEC = 15.0    # held option silent this long → exit at last
                                # price (tightened 120→45→15 s — a held position
                                # whose strike quote goes silent is BLIND to the
                                # hard stop for this long; 15 s bounds that window
                                # before the SL is uncheckable; audit finding;
                                # 45 s still tolerates a brief feed hiccup)
MAX_HOLD_SEC         = 900.0   # UNUSED — binary doctrine: NO timeout scratch. A
                                # non-runner rides to −10 (theta carries a true dud
                                # there) or +12. Kept defined to avoid import breaks.

# The ONLY exits allowed to land between −10 and +12 — both are SAFETY, never a
# chosen trade (feed went silent, or the session ended). Everything else MUST be
# ≤ −10 (SL HIT) or ≥ +12 (TRAIL SL). The binary-exit invariant test asserts this.
SAFETY_EXIT_REASONS = ("STALE QUOTE", "EOD CLOSE")

# user's principle: "Nifty devilishly hunts your stop right after entry, but
# once convincingly in profit you can relax the strictness."
# → entry side stays ruthless (hard −10, stall kill, persistence gates);
# → once the trail has LOCKED a real profit, the chandelier earns extra air:
RELAX_LOCK_PTS       = 4.0     # trail ≥ entry+4 = "convincingly in profit"
RELAX_WIDEN          = 1.20    # extra offset multiplier once relaxed

# ═════════════════════════════════════════════════════════════════════════════
# OPEN INTEREST ENGINE  (Requirement §4)
# ═════════════════════════════════════════════════════════════════════════════
OI_WALL_MULT       = 2.0        # strike OI > 2× neighbour average = wall
OI_EMA_WINDOWS     = (60.0, 180.0, 300.0)   # d(OI)/dt EMAs: 1, 3, 5 minutes
SUPPORT_PCR_MIN    = 1.0        # strike PCR above this + building put OI = support
RESIST_PCR_MAX     = 0.7        # strike PCR below this + building call OI = resistance
SR_ZONE_STRIKES    = 2          # cluster width (strikes) when merging zones
MAX_PAIN_SHIFT_ALERT = 100.0    # commentary trigger (index points)

# ═════════════════════════════════════════════════════════════════════════════
# VOLATILITY ENGINE  (Requirement §5)
# ═════════════════════════════════════════════════════════════════════════════
RISK_FREE_RATE     = 0.065
FALLBACK_IV        = 0.15       # vol used when the IV solve fails (deep-ITM premium≈intrinsic),
                                # so single_greeks returns a BOUNDED delta instead of None — a real
                                # ITM MOVER is never NaN-skipped in strike selection.
IV_HISTORY_DAYS    = 30         # IV rank/percentile lookback (persisted in SQLite)
EXPECTED_MOVE_K    = 0.8        # expected move = 0.8 × ATM straddle

# ═════════════════════════════════════════════════════════════════════════════
# ACTIVITY RADARS  (user mandate: highlight significant OI / buyer-seller
# changes in ANY contract of ANY instrument; never sit silent)
# ═════════════════════════════════════════════════════════════════════════════
RADAR_OI_PCT          = 8.0     # OI ≥8% away from its own baseline = event
RADAR_OI_MIN_ABS      = 40_000  # ...and at least this many contracts moved
RADAR_VOL_MULT        = 4.0     # volume rate ≥4× its own norm = spike
RADAR_BOOK_SHIFT      = 2.5     # bid/ask ratio ≥2.5× away from its norm
RADAR_EVENT_COOLDOWN  = 150.0   # per-contract dedup window (seconds)

# ═════════════════════════════════════════════════════════════════════════════
# FLIGHT RECORDER / REPLAY  (the keystone: record every session so any policy
# change can be A/B-tested on the EXACT same recorded tape before deploying —
# ends the "tune to the last bad session from memory" curve-fit cycle)
# ═════════════════════════════════════════════════════════════════════════════
REPLAY_RECORD    = True         # write a full market frame each analytics pass
REPLAY_FRAME_SEC = 1.0          # frame cadence (1 Hz = faithful to live loop)

# ═════════════════════════════════════════════════════════════════════════════
# MARKET REGIME  (user mandate: identify flattish markets and refuse trades
# there — but narrate continuously, never sit silent)
# ═════════════════════════════════════════════════════════════════════════════
FLAT_RANGE_PCT     = 0.0010     # 15-min futures range < 0.10% of spot ...
FLAT_ADX_MAX       = 18.0       # ...and ADX weak ...
FLAT_CVD_SLOPE     = 120.0      # ...and |CVD slope| small → FLAT: no entries
COIL_RANGE_PCT     = 0.0016     # narrow + OI building = COILING (breakout watch)

# ═════════════════════════════════════════════════════════════════════════════
# SERVER / UI
# ═════════════════════════════════════════════════════════════════════════════
HOST               = "127.0.0.1"
PORT               = 8765
UI_PUSH_MS         = 500        # full state-tree push cadence
PRICE_PUSH_MS      = 200        # FAST price-only push cadence (decoupled from the
                                # heavy build so a slow pass never delays prices)
OPT_STALE_SEC      = 45.0       # ATM option-feed staleness → dashboard STALE badge
ANALYTICS_SEC      = 1.0        # full analytics pass cadence
EXIT_CHECK_SEC     = 0.25       # open-position exit checks (near tick-speed)
SIM_SPEED          = 10.0       # `--sim2` time-warp factor: the whole engine runs
                                 # this many × faster than wall-clock (via clk.py)
                                 # so a full session can be tested in minutes

# ═════════════════════════════════════════════════════════════════════════════
# COMMENTARY THRESHOLDS  (Requirement §8 — extreme events only)
# ═════════════════════════════════════════════════════════════════════════════
COMMENT_PCR_SPIKE      = 0.35   # PCR jump within 5 min
COMMENT_CVD_SIGMA      = 2.0    # CVD/price divergence in std-devs
COMMENT_IVR_JUMP       = 30.0   # IV-rank points within 5 min
COMMENT_VOL_SURGE_MULT = 5.0    # single-strike volume vs its average
COMMENT_HW_MOVE_PCT    = 1.5    # heavyweight intraday % move alert
COMMENT_BOOK_IMBAL     = 3.0    # best-bid qty ≥ 3× best-ask qty (or inverse)
COMMENT_SPREAD_BLOWOUT = 3.0    # spread ≥ 3× its own average = liquidity pulled
COMMENT_COOLDOWN_SEC   = 180.0  # same event type can't repeat within this
# ── COMMENTARY DECLUTTER (task #37) — flag-gated, default OFF = byte-identical.
#    User: "you flood the commentary with stupid things and I miss the important
#    ones." A priority tier sits above the per-kind cooldown: CRITICAL tells
#    (seller-exhaustion, fall-warning distribution/accumulation, blaster ignite,
#    cross-index) are NEVER rate-capped; ROUTINE chatter (book/vol-surge/
#    liquidity/max-pain/iv/expiry/regime/heavyweight movers) gets a global
#    rolling-window budget so it can't crowd out the chime that matters.
COMMENT_PRIORITY_ON         = True   # ON 2026-06-17 (user sign-off): CRITICAL tells
                                      # (seller-exhaustion / fall-warning / cross-index)
                                      # never rate-capped; routine chatter budgeted.
                                      # Display+audio only — no trade-decision effect.
COMMENT_SIGNAL_ONLY         = True   # ON 2026-06-19 (user mandate): show ONLY the MOST
                                      # significant directional alerts — VERY bullish / VERY
                                      # bearish — and suppress ALL other chatter entirely.
                                      # Shown: fall/rip early-warning (DISTRIBUTION/ACCUMULATION,
                                      # RISING), seller-exhaustion/buyer-flight, broad-tape
                                      # consensus + gate veto, gamma ignition. Everything else
                                      # (book/vol/gex/gamma/iv/liquidity/PCR/max-pain/quad/hw)
                                      # is silenced. The WHY-THIS-TRADE rationale still prints
                                      # (it goes via note(), not _fire). Set False for the full feed.
COMMENT_ROUTINE_MAX_PER_WIN = 2      # max LOW-VALUE chimes per window (game-changers exempt)
COMMENT_ROUTINE_WINDOW_SEC  = 60.0   # the rolling window for the routine budget
COMMENT_LOWPRI_COOLDOWN     = 360.0  # gex/gamma/vol-expansion/iv/liquidity → 6 min, not 3
COMMENT_CROSS_PCT           = 0.25   # sister-index day-move %% to call the tape directional
COMMENT_CROSS_MOM_SEC       = 180.0  # window for the INTRADAY BankNifty momentum used to STEER
COMMENT_CROSS_MOM_PCT       = 0.12   # BankNifty move over that window to flip the steer (leads the
                                      # day-change, so the tell turns bullish the instant banks turn)

# ── OPTION BATTLE LINES (user request 2026-06-19) — premium S/R memory for the
#    ATM/ITM Nifty CE & PE: defended floors (buyers step in) + resisted ceilings
#    (sellers slam back), confirmed by order-flow. Display-only; zigzag pivots.
BATTLE_STRIKES_EACH = 2      # ATM + this many ITM strikes per side tracked
BATTLE_REBOUND_FRAC = 0.06   # premium must reverse this fraction off a leg extreme to print a pivot
BATTLE_REBOUND_PTS  = 3.0    # ...or this many points (whichever is larger) — floor for cheap premiums
BATTLE_BAND_FRAC    = 0.04   # pivots within this fraction cluster into ONE level
BATTLE_BAND_PTS     = 2.0    # ...or this many points (whichever larger)
BATTLE_MIN_TESTS    = 2      # tests before a level is "battle-tested" (defended/resisted)
BATTLE_FLOW_RATIO   = 1.8    # bid:ask (or ask:bid) quantity ratio = SERIOUS buying (or selling)
# instrument-PRICE tracks (Nifty/BankNifty/FinNifty/stocks) — tight % thresholds
# (an index/stock S/R level is ~0.1-0.2% apart, not the 4-6% an option premium swings)
BATTLE_INST_REBOUND_FRAC = 0.0015   # price must reverse 0.15% off a leg extreme to print a pivot
BATTLE_INST_REBOUND_PTS  = 1.0
BATTLE_INST_BAND_FRAC    = 0.0010   # pivots within 0.10% cluster into one level
BATTLE_INST_BAND_PTS     = 0.5
                                      # (the cross-instrument tell — BankNifty/FinNifty + basket
                                      # in plain language so a trade against the broad tape is
                                      # obvious to skip; CRITICAL tier, never throttled)
# ── TARGETED NOISE CUT (task #37): the actual repeat offenders get long per-kind
#    cooldowns so they collapse from spam to a rare note (these apply ALWAYS, not
#    just when COMMENT_PRIORITY_ON). The important tells (reversal/fall-risk/cross-
#    index/PCR/CVD/max-pain) keep their normal cadence.
COMMENT_BOOK_COOLDOWN     = 600.0    # top-of-book imbalance (spoofable, fired 6 ways/sec) → 10 min
COMMENT_VOLSURGE_COOLDOWN = 360.0    # single-strike volume surge (7 strikes x 2) → 6 min
COMMENT_QUAD_COOLDOWN     = 300.0    # futures-quadrant flip (oscillates near neutral) → 5 min
EXPIRY_WARN_COOLDOWN      = 1800.0   # expiry-day theta warning: once / 30 min, not every 3

# ── SISTER-INDEX OI RECORDER (task #37) — flag-gated, default OFF. Captures
#    BankNifty + FinNifty option-chain PCR + walls into the feed AND the frame
#    recorder so (1) the live engine can read their OI and (2) an OI-based entry
#    vote becomes PROVABLE on future tapes (it was never recorded before — that
#    is why "check their OI" kept slipping). OFF = no extra REST calls, no new
#    populated frame keys -> byte-identical. Off the WS hot path (REST daemon).
#    PROBE the exact expiry + NFO stock_code against a live Breeze response
#    before flipping ON (BankNifty weeklies were discontinued; sisters are
#    monthly in 2026). Recording is safe even if data is absent (.get-defaulted).
SISTER_CHAIN_ON       = True   # ON 2026-06-19 (task #39): BankNifty/FinNifty option OI flows into
                                # the BREADTH panel + the recorder. Degrades gracefully if the NFO
                                # code/expiry don't resolve live (gate still runs on the always-live
                                # legs); verify the chain returns rows in the live log on turn-on.
SISTER_CHAIN_POLL_SEC = 60.0   # round-robin cadence (well under the 100/min cap)


# ═════════════════════════════════════════════════════════════════════════════
# BLASTER FOREWARNING (two-tier gamma) + LEARNING LOOP
# Every value is a PRIOR, not a proven optimum — ship conservative (rare/quiet),
# loosen only after replay.py review on real tape. The user mandate: "if a gamma
# explosion is about to happen, tell me IN ADVANCE" — but never cry wolf.
#   LOADING  = quiet, silent pre-alert when a coil is winding up (note(), no chime)
#   IGNITING = loud, near-certain, on the break + 1 independent confirm (_fire(), chimes)
# ═════════════════════════════════════════════════════════════════════════════

# — persistent mistake journal (survives the day-roll that wipes trades_today.json)
MISTAKE_JOURNAL_JSON = os.path.join(DATA_DIR, "mistake_journal.json")  # sim redirects in app.__init__
JOURNAL_MAX_ENTRIES  = 400        # FIFO cap, newest kept (~weeks of trades; bounds the file)

# — gamma blaster, LOADING (quiet pre-alert) ----------------------------------
GAMMA_LOAD_HEAT      = 0.14       # gamma_heat coil gate — just under the 0.18 GAMMA-ZONE fire so LOADING leads
GAMMA_FLIP_NEAR_PTS  = 25.0       # spot within this of the flip strike = "pinned at the coil" (OPTIONAL gate)

# — gamma blaster, IGNITING (loud, near-certain) ------------------------------
GAMMA_BREAK_PTS      = 18.0       # spot must leave the coil band by this to count as a break (mandatory)
REALIZED_KICK_FRAC   = 0.25       # realized_vol_1m up >= +25% vs GAMMA_COIL_WINDOW ago = a real vol expansion
GAMMA_IGNITE_FROM_LOAD_SEC = 120.0  # IGNITING only valid if a confirmed LOAD occurred within this — never cold
GAMMA_COOLED_SEC     = 120.0      # post-IGNITING lockout so one coil fires the loud chime exactly once

# — blaster cooldowns (own _fire kinds; independent of COMMENT_COOLDOWN_SEC=180)
BLASTER_LOADING_COOLDOWN = 240.0  # calm pre-alert: at most once / 4 min
BLASTER_IGNITE_COOLDOWN  = 600.0  # loud tier rare BY CONSTRUCTION (rarest candidate value)

# — learning loop -------------------------------------------------------------
POSTEXIT_WATCH_SEC   = 60.0       # PROVISIONAL horizon: the quick "what happened next" read
POSTEXIT_WATCH_SEC_LONG = 300.0   # FINAL retrospective horizon — re-grade ~5 min later vs the
                                   # price + new conditions (user 2026-06-15: "not just after
                                   # one minute, a few mins later also refer the price"). A 60s
                                   # "looked fine" exit the tape then ran far beyond becomes a
                                   # logged BOOKED-EARLY; a win that reversed right after = GOOD_EXIT.
POSTEXIT_FRESH_SEC   = 3.0        # reject a post-exit price read older than this (strike drifted out of band)
BOOK_EARLY_PTS       = 15.0       # WIN ran >= this far beyond our exit in the window => "booked early"
BROKEN_FRAC          = 0.45       # >= this fraction of conviction factors broken at exit => "held a broken thesis"
GREY_MFE_PTS         = 5.0        # |post-exit move| smaller than this => ambiguous => GREY (flag, don't blame)
RECALL_REPEAT_MIN    = 2          # same (dir,zone,mistake_class) must recur >= this often before a recall whisper
EOD_SUMMARY_AT       = (15, 25)   # once-daily summary in 15:25–15:30 (before the day-roll wipes closed[])
POSTEXIT_MIN_SAMPLES = 4          # min FRESH post-exit path samples for a non-GREY path verdict (SIM2 noise guard)

# ═════════════════════════════════════════════════════════════════════════════
# CLOSED ADAPTIVE LOOP — the TrustBook (the system learns and gets better).
# A bounded, per-(zone,direction) EMA of doctrine-clean outcomes (+12 vs a −10
# WITH a broken thesis). It raises the entry evidence bar on contexts that keep
# failing relative to the book average — gradually, reversibly, and NEVER touching
# an exit. It can ONLY make entries stricter, never looser, and can never starve
# all trading. Anti-overfit by construction (see learning.TrustBook).
# ═════════════════════════════════════════════════════════════════════════════
ADAPT_ENTRY_GATE_ON   = True    # master switch: False = observe + render only (no gating)
ADAPT_STATE_JSON      = os.path.join(DATA_DIR, "adaptive_state.json")  # sim → adaptive_state_sim.json
ADAPT_EMA_ALPHA       = 0.15    # gradual: one outcome moves the ema ≤15% of the gap
ADAPT_PRIOR           = 0.5     # neutral start + decay target
ADAPT_MIN_SAMPLES     = 4       # no influence until 4 clean binary outcomes in a context
ADAPT_REL_BUMP1       = 0.12    # ema must be this far BELOW the book ema for a +1 bar
ADAPT_REL_BUMP2       = 0.22    # ... for +2
ADAPT_BUMP_MAX        = 2       # max evidence bump (never a hard skip)
ADAPT_MIN_HEADROOM    = 1       # effective_need ≤ len(evidence) − this (always clearable)
ADAPT_GLOBAL_THROTTLE = 0.50    # if >50% of eligible contexts would bump, suspend (base-rate moved)
ADAPT_DECAY           = 0.10    # per virtual-day reversion of each ema toward PRIOR
ADAPT_SAMPLE_DECAY    = 0.80    # per virtual-day shrink of n (unvisited contexts fall below the gate)
ADAPT_WATCH_MAXLEN    = 16      # max concurrent post-exit watch tickets (SIM2 flood guard)

# ── PERSISTENT MARKET MEMORY (v1 = DISPLAY-ONLY). memory.py is imported ONLY by
#    state.build_state — never by signals/trader — so it can NEVER change a trade.
#    Data accrues off the hot path (copy+enqueue → daemon writer); decision wiring
#    is DEFERRED behind a record-and-measure-predictiveness gate (see memory.py).
MEMORY_DIR          = os.path.join(DATA_DIR, "memory")  # sim → memory_sim/ in app.__init__
MEM_OBSERVE_SEC     = 15.0    # hot-path copy+enqueue cadence (most passes do nothing)
MEM_FLUSH_SEC       = 30.0    # daemon flushes dirty files at most this often
MEM_STRENGTH_ALPHA  = 0.25    # EMA step per qualifying level event (stays in [0,1])
MEM_DECAY_PER_DAY   = 0.08    # untouched level strength reverts ~8%/day toward 0
MEM_STALE_DAYS      = 10      # a level untouched this many days is evicted
MEM_MAX_LEVELS      = 40      # per instrument (evict lowest-strength over the cap)
MEM_EVENTS_RING     = 12      # per-level rolling event history
MEM_LEVEL_BAND_PCT  = 0.0006  # spot within 0.06% of a level counts as "at" it
MEM_POISE_HALF_FAST = 90.0    # poise fast-EMA half-life (s) → "nerve" (jumpiness)
MEM_POISE_HALF_SLOW = 1800.0  # poise slow-EMA half-life (s) → "conviction" (the lean)
MEM_POISE_DAILY_MAX = 60      # poise_daily.jsonl rolling-day cap

# ── FALL / RIP EARLY-WARNING (FallRiskMonitor in risk.py — READ-ONLY HUD + a
#    persistent commentary tell; NO flag, touches NO decision). Fuses already-
#    computed LEADING→LAGGING signals into a 0-100 risk that builds WHILE a
#    roll-over forms — the 2026-06-15 −51.6pt slide had zero advance warning. All
#    PRIORS, --set tunable. (A behaviour-changing FALL_RISK_VETO is separate,
#    flag-gated, A/B — see the round-2 plan; NOT wired here.)
FALLRISK_EMA_ALPHA = 0.10    # smoothing of the fused score (build/erode gradually)
FALLRISK_QUIET     = 50      # below this = calm tape
FALLRISK_LOUD      = 70      # at/above, with ≥2 independent axes lit = fire the tell
FALLRISK_WIN_SEC   = 180.0   # lookback for the breadth / divergence build
FALLRISK_W         = {"breadth": 0.28, "cvd": 0.25, "quadrant": 0.20,
                      "structure": 0.14, "volpcr": 0.13}

# BOOK-WIDE CIRCUIT BRAKE — the fix for the 74-trades-into-the-ground autopsy.
# The relative bump above is powerless when EVERY zone is equally bad (nothing is
# "below the book"), so the system kept trading a 31%-WR book full-tilt. This is
# an ABSOLUTE brake: once enough trades are graded and the whole book's trust is
# below the floor (the strategy is demonstrably a net loser), DEMAND much more
# evidence on EVERY entry — only the very best setups fire, so the engine stops
# digging. It is EVIDENCE-based, not a capital stop (full-capital sizing is
# untouched), and it RELEASES automatically the moment wins lift trust back up.
ADAPT_BOOK_MIN_N      = 12      # need this many graded outcomes before the brake can bite
ADAPT_BOOK_FLOOR      = 0.42    # global trust below this = losing book → brake +2
ADAPT_BOOK_FLOOR2     = 0.34    # ... below this = badly losing → brake +4 (only the best fire)
ADAPT_BOOK_BRAKE      = 2
ADAPT_BOOK_BRAKE2     = 4
