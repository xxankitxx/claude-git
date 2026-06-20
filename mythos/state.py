"""
MYTHOS — UI state assembly: one JSON document describing the entire market
picture, pushed to the dashboard every UI_PUSH_MS. Pure reads — never mutates
any engine.
"""

import math
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Dict

from . import clk, config
from .config import IST

# The position HEART carries read-modify-write hysteresis state on app._heart
# (st["a"]/["b"]/["c"]). build_state runs on TWO threads — the WS push's
# to_thread build and the /api/state route on the event loop — so without this
# lock the two can tear the dict / double-promote / KeyError mid-mutation (the
# very cross-thread class that froze prices on 06-15). One app per process, so a
# module-level lock fully serializes every heart read-modify-write and its reset.
_HEART_LOCK = threading.Lock()


def _f(v, nd=2):
    """JSON-safe rounded float. A NaN/Inf (a broker can send one) must NEVER reach
    json.dumps — it emits invalid JSON that wedges the client's JSON.parse and
    FREEZES the dashboard silently. Coerce any non-finite value to 0.0."""
    try:
        x = float(v)
        return round(x, nd) if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _copy(seq):
    """Snapshot a deque/list that another thread may be appending to.
    list(deque) raises RuntimeError if mutated mid-iteration — retry, then
    give up with empty (one missed UI frame is invisible at 2 fps)."""
    for _ in range(3):
        try:
            return list(seq)
        except RuntimeError:
            continue
    return []


def _smile_safe(app, atm):
    """vol.smile_points() iterates a live smile dict the analytics thread
    mutates; this runs in the WS-push worker thread, so a bare call can raise
    'dict changed size during iteration'. Retry, then degrade to []."""
    if atm <= 0:
        return []
    for _ in range(3):
        try:
            return app.vol.smile_points(atm)
        except RuntimeError:
            continue
    return []


def _memory_safe(app):
    """Read-only persistent-market-memory snapshot for the dashboard. Guarded so
    build_state never breaks if memory is absent or mid-update (it takes its own
    short lock + returns a copy)."""
    try:
        m = getattr(app, "memory", None)
        return m.snapshot() if m is not None else {}
    except Exception:
        return {}


def _risk_safe(app):
    """Read-only fall/rip early-warning snapshot for the dashboard HUD. Reads only
    result scalars (never iterates the monitor's live history) — tear-proof."""
    try:
        r = getattr(app, "risk", None)
        return r.snapshot() if r is not None else {}
    except Exception:
        return {}


def _battle_safe(app):
    """Read-only OPTION BATTLE LINES snapshot (defended floors / resisted ceilings
    of the ATM/ITM CE & PE premiums). Returns the frozen row list — tear-proof."""
    try:
        b = getattr(app, "battle", None)
        return b.snapshot() if b is not None else {}
    except Exception:
        return {}


def overlay_live_prices(app, state):
    """Refresh the TIME-CRITICAL price + freshness fields directly on the WS push
    path (lock-free freeze_core, microseconds) so the dashboard shows CURRENT
    prices on every push — even if the heavy state tree came from a slightly older
    pass or was served from the last-good cache. Requirement §3: no calculation may
    stop prices displaying on time. CRITICAL: spot_age / fut_buffer / ticks_dropped
    are recomputed LIVE here, never trusted from the cached frame, so the tick-loss
    watchdog and dead-engine detection can't be fooled by a stale-but-served frame.
    Never raises — degrades to the unmodified state."""
    try:
        p = app.prices
        spot, fut, atm, ce, pe = p.freeze_core()
        m = state.get("market")
        if isinstance(m, dict):
            if spot > 0:
                m["spot"] = _f(spot)
            if fut > 0:
                m["futures"] = _f(fut)
            if ce > 0:
                m["ce_ltp"] = _f(ce)
            if pe > 0:
                m["pe_ltp"] = _f(pe)
        h = state.get("health")
        if isinstance(h, dict):
            h["spot_age"] = _f((clk.mono() - p.spot_ts) if p.spot_ts else 9999, 1)
            h["opt_age"] = _f(p.atm_option_age(), 1)
            h["fut_buffer"] = len(p.fut_ticks)
            h["ticks_dropped"] = p.fut_ticks_dropped
            h["tick_count"] = p.tick_count
        state["ts"] = datetime.now(IST).strftime("%H:%M:%S")
    except Exception:
        pass
    return state


def price_frame(app):
    """Tiny price-only frame for the FAST push path (Requirement §3: prices must
    never wait behind a slow analytics pass). Built lock-free via freeze_core in
    microseconds — carries market + health only, tagged kind='price' so the client
    patches the header without a full re-render. Never raises."""
    frame = {"kind": "price", "market": {}, "health": {}}
    overlay_live_prices(app, frame)
    return frame


# ── position "HEART" — MYTHOS speaks its mind while a trade is live ───────────
# First-person and HONEST, tuned to the trader's two weaknesses: it pushes to
# HOLD a winner (so profits aren't booked cheap) and to CUT a loser (so a bad
# trade isn't held hoping for cost price). One line at a time, with hysteresis —
# never a flickering noise box. (stance, colour, [line variants])
_HEART = {
    "SECURED":      ("LET IT RUN", "good", [
        "+12 is locked in — anything from here is bonus. Sit on your hands, let me trail it.",
        "Our minimum is secured. DON'T book now — I'm hunting the bigger move.",
        "Worst case we bank +12. Relax, breathe, and let it run."]),
    "BLAST":        ("HOLD — DON'T TOUCH", "good", [
        "This is taking off — the move I wanted is here. HOLD. Do not touch it.",
        "Premium is surging. I'm winning this one big — hands off.",
        "It's blasting. Every point you hold now is yours — don't get nervous and book."]),
    "APPROACHING":  ("ALMOST THERE", "good", [
        "Almost at +12 — the second I hit it, it's locked. You're getting your profit, relax.",
        "Nearly there. Hold steady — +12 is in reach."]),
    "WINNING_HOLD": ("HOLD", "good", [
        "We're green and still building — I'm going to win this. Don't book early, ride it.",
        "This is working: my zone's holding and flow is with us. Hold for +12.",
        "Looking good. I know it's tempting to grab it — don't. Let me take it to +12."]),
    "FADING_GREEN": ("BOOK IT NOW", "warn", [
        "We're green but the flow is turning against me — I'd take this now rather than give it back.",
        "Something shifted; I don't trust this anymore. Book it here while we're up."]),
    "STALLING":     ("HOLDING — NO SCRATCH", "warn", [
        "It popped but it's hesitating. I hold for +12 or the −10 — I don't scratch. If it won't push, the entry or the sim was wrong.",
        "Stuck in the mud, but a scratch isn't an option: this rides to +12 or −10. A non-runner is a bad-entry signal, not a small loss to book."]),
    "WRONG_ENTRY":  ("WATCHING", "warn", [
        "It went against me right off the entry — I bought a touch rich, should've waited for cheaper. Watching.",
        "Not the start I wanted; I may have been early. Let me see if the zone still saves it."]),
    "DIP_HOLD":     ("HOLD THROUGH", "good", [
        "It's dipping but my support is holding — this is the shake before the move. The −10 has us. Holding.",
        "Don't panic — this pullback is normal, my level hasn't broken. I'm staying in."]),
    "MISTAKE_CUT":  ("CUT IT", "danger", [
        "I was wrong — the level broke. This is a mistake. Take the −10 and move on; don't hope for cost.",
        "My read failed here. Cut it. Waiting for your cost price is how small losses become big ones."]),
    "NEAR_STOP":    ("TAKE THE STOP", "danger", [
        "We're at the stop. It didn't work, and that's okay. −10, done, next. No hoping.",
        "Stop is here. Let it go cleanly — the plan was −10, honour it."]),
    "JUST_IN":      ("EASY", "neutral", [
        "Just in. Let it breathe — I'll speak up the moment it shows its hand.",
        "Fresh trade. Give it a few seconds; I'll tell you which way this is going."]),
}


def _heart_mood(app, t, cur: float, pnl: float) -> str:
    entry = t.entry_price
    peak = t.peak_price - entry
    since_peak = (clk.mono() - t.last_peak_epoch) if t.last_peak_epoch else 0.0
    age = clk.mono() - t.entry_epoch
    dist_stop = cur - t.stop_loss
    conv = app.signals._pos_conv or {}
    tone = conv.get("tone", "")
    try:
        vel = app.signals.prem.velocity(t.direction)      # premium pts/sec
    except Exception:
        vel = 0.0
    weak = (tone == "danger") or t.weakened
    if t.trail_active:                       return "SECURED"
    if dist_stop <= 2.0:                      return "NEAR_STOP"
    if weak and pnl < -0.5:                   return "MISTAKE_CUT"
    if vel > 0.6 and pnl > 2.0:               return "BLAST"
    if pnl >= 8.0:                            return "APPROACHING"
    if pnl > 1.5 and weak:                    return "FADING_GREEN"
    if pnl > 1.5 and since_peak < 10:         return "WINNING_HOLD"
    if pnl >= 0 and peak >= 3 and since_peak > 22:  return "STALLING"
    if peak < 1.5 and pnl < -1.0 and age < 80:      return "WRONG_ENTRY"
    if pnl < 0:                               return "DIP_HOLD"
    return "JUST_IN"


# ── SLOT B — the live "WHY" ──────────────────────────────────────────────────
# Drawn from the 12 real conviction factors (signals.position_conviction). While
# in a trade MYTHOS names, in plain words, the single most decisive reason it's
# going to WIN (green) or the reason it's at RISK (red) — and keeps the numbers
# inside live even when the sentence holds, so it reads alive, not frozen.
# Factors ranked high→low by how decisive they are for the call.
_B_RANK = [
    "Entry zone intact", "Opposite side quiet", "Our premium rising",
    "Premium force intact", "ATM±6 PCR shift", "Futures flow with us",
    "Unwinding fuel", "Their premium weak", "Book with us", "AVWAP side",
    "Heavyweights with us", "BankNifty/FinNifty agree",
]
# mood → which polarity of factor the "why" must agree with (Slot A/B coherence).
# True = name a SUPPORTING factor, False = name a BROKEN one, absent = auto.
_B_WANT = {
    "SECURED": True, "BLAST": True, "WINNING_HOLD": True, "APPROACHING": True,
    "DIP_HOLD": True,
    "MISTAKE_CUT": False, "NEAR_STOP": False, "FADING_GREEN": False,
    "WRONG_ENTRY": False,
}
_WHY = {
    # ── WINNING (ok=True) ──
    ("Entry zone intact", True): [
        "my {z} floor is holding clean — the whole reason I'm here is still true.",
        "we're sitting right on {z} and it's not cracking. The thesis is alive."],
    ("Our premium rising", True): [
        "premium's pushing {detail} — buyers are paying up for us.",
        "our option is bid higher every tick ({detail}) — that's real demand."],
    ("Premium force intact", True): [
        "the move still has thrust under it ({detail}) — not dying.",
        "acceleration is with us ({detail}); this isn't running out of gas."],
    ("ATM±6 PCR shift", True): [
        "near-ATM PCR is sliding our way ({detail}) — writers underwriting us.",
        "put writers are stepping in around ATM ({detail}); that's our floor."],
    ("Futures flow with us", True): [
        "futures order-flow is leaning our way ({detail}) — the big lots agree.",
        "CVD is with us ({detail}); the tape is doing the work."],
    ("Unwinding fuel", True): [
        "the other side is being forced out ({detail}) — that's our fuel.",
        "positioning is unwinding into us ({detail}); this can accelerate."],
    ("Opposite side quiet", True): [
        "no hunter on the other side right now — nothing threatening this.",
        "the opposite zone is dead quiet ({detail}); nothing's lining up against us."],
    ("Their premium weak", True): [
        "the other side's premium is going nowhere ({detail}) — no fight back.",
        "their option is limp ({detail}); the pressure's all on our side."],
    ("Book with us", True): [
        "the order book is stacked for us ({detail}) — buyers waiting underneath.",
        "bid size is dominating ({detail}); demand is real, not painted."],
    ("AVWAP side", True): [
        "the trapped traders are on our side of the average — they're underwater, we're not.",
        "we're on the right side of the day's trapped money; they fuel us."],
    ("Heavyweights with us", True): [
        "the basket is pulling our way ({detail}) — the big names agree.",
        "heavyweights are a tailwind here ({detail}); Nifty rarely fights them."],
    ("BankNifty/FinNifty agree", True): [
        "the banks are moving with us ({detail}) — that's a third of the index agreeing.",
        "sister indices confirm ({detail}); Nifty won't fight Bank Nifty for long."],
    # ── LOSING / AT-RISK (ok=False) ──
    ("Entry zone intact", False): [
        "the {z} floor just CRACKED — that was the whole reason I'm here. This is broken.",
        "my zone broke. I was wrong about {z}. Don't hope — this is a cut."],
    ("Opposite side quiet", False): [
        "the other side's hunter is WAKING UP ({detail}) — this can flip on us.",
        "I see the opposite zone arming ({detail}); I've been burned by this exact flip."],
    ("Our premium rising", False): [
        "our premium has stopped rising ({detail}) — buyers aren't paying up anymore.",
        "no bid behind us now ({detail}); the demand I needed has gone."],
    ("Premium force intact", False): [
        "the move is DECELERATING hard ({detail}) — it's dying before it pays.",
        "thrust is bleeding out ({detail}); this is stalling before +12."],
    ("ATM±6 PCR shift", False): [
        "near-ATM PCR turned against us ({detail}) — writers pulling the floor.",
        "the put-writer support I needed is fading ({detail})."],
    ("Futures flow with us", False): [
        "futures flow has turned against me ({detail}) — the big lots left.",
        "CVD flipped the wrong way ({detail}); the tape isn't helping now."],
    ("Unwinding fuel", False): [
        "the fuel I wanted isn't there ({detail}) — no forced flow behind this.",
        "positioning isn't feeding us ({detail}); this has to move on its own."],
    ("Their premium weak", False): [
        "the OTHER side's premium is firing up ({detail}) — they're winning the fight.",
        "their option is rising ({detail}); pressure has swung against us."],
    ("Book with us", False): [
        "the book has turned ({detail}) — sellers now leaning on us.",
        "bid support thinned out ({detail}); the queue isn't ours anymore."],
    ("Heavyweights with us", False): [
        "the basket has turned against us ({detail}) — big names dragging the wrong way.",
        "heavyweights flipped ({detail}); that's a real headwind."],
    ("BankNifty/FinNifty agree", False): [
        "the banks are pulling AGAINST us ({detail}) — Nifty rarely beats them.",
        "sister indices disagree now ({detail}); the move's losing its anchor."],
    ("AVWAP side", False): [
        "we slipped to the wrong side of the trapped-money average — momentum's against us.",
        "the trapped traders are now on the winning side, not us."],
}

# ── SLOT C — event flash lead-ins (frame a radar/commentary event for the held
# trade). The event text is already a full human sentence; we only prepend the
# position-relative framing.
_FLASH_LEAD = {
    "threat_nifty":  ["⚠ Watch this — ", "Careful — ", "Heads up, against us — "],
    "threat_basket": ["⚠ {inst} just moved against us — ",
                      "Basket warning ({inst}) — ", "{inst} is dragging the wrong way — "],
    "confirm_nifty": ["Good — ", "This helps us — ", "Tailwind just hit — "],
    "confirm_basket": ["{inst} swinging our way — ", "Basket tailwind ({inst}) — "],
    "warn_generic":  ["Note — ", "Tape event — "],
    "blaster_confirm": ["⚡ IGNITING with us — ", "The coil just fired our way — ",
                        "It's breaking our side — "],
    "blaster_threat":  ["⚡ IGNITING against us — ", "The coil broke the wrong way — ",
                        "Firing against us — "],
}
_NIFTY_INSTRUMENTS = {"NIFTY", "NIFTY 50", "NIFTY FUT", "NIFTY FUTURES", ""}


def _fill(tpl: str, z: str = "", detail: str = "", inst: str = "") -> str:
    return tpl.replace("{z}", z).replace("{detail}", detail).replace("{inst}", inst)


def _heart_slot_a(app, t, cur: float, pnl: float, st: Dict, now: float) -> Dict:
    """Slot A — the mood line (owns colour + stance). Hysteresis so it never
    flickers; re-rolls a variant only on mood change or after 22s."""
    mood = _heart_mood(app, t, cur, pnl)
    a = st.get("a")
    fresh = a is None or a.get("mood") != mood
    if fresh or (now - a.get("ts", 0.0)) > 22.0:
        pool = _HEART[mood][2]
        idx = 0 if fresh else (a.get("idx", 0) + 1) % len(pool)
        a = {"mood": mood, "idx": idx, "ts": now}
        st["a"] = a
    stance, color, pool = _HEART[mood]
    return {"mood": mood, "stance": stance, "color": color, "line": pool[a["idx"]]}


def _pick_factor(by_name: Dict, want_ok):
    """Highest-ranked factor of the wanted polarity (True=supporting,
    False=broken). want_ok=None → the single most decisive factor present.
    Returns None if nothing of the wanted polarity exists — the caller then
    speaks a coherent generic rather than contradict the mood."""
    if want_ok is not None:
        for name in _B_RANK:
            f = by_name.get(name)
            if f and bool(f.get("ok")) == want_ok:
                return f
        return None
    for name in _B_RANK:
        f = by_name.get(name)
        if f:
            return f
    return None


def _heart_slot_b(app, t, conv: Dict, pnl: float, mood: str,
                  st: Dict, now: float) -> Dict:
    """Slot B — the live 'why', with dwell+confirm hysteresis (anti-noise)."""
    factors = (conv or {}).get("factors") or []
    if not factors:
        return {"text": "reading the tape — give me a few ticks to call this.",
                "polarity": None}
    by_name = {f["name"]: f for f in factors}
    tone = (conv or {}).get("tone", "")

    # desired polarity: mood first (Slot A/B coherence), then PnL/tone
    if mood in _B_WANT:
        want_ok = _B_WANT[mood]
    elif tone in ("warn", "danger") or pnl < -0.3:
        want_ok = False
    elif pnl > 0.5:
        want_ok = True
    else:
        want_ok = None

    # HARD OVERRIDE: a broken thesis zone is never noise — promote instantly.
    zone_f = by_name.get("Entry zone intact")
    force_now = bool(zone_f) and not zone_f.get("ok")
    if force_now:
        chosen = zone_f
    else:
        chosen = _pick_factor(by_name, want_ok)
        if chosen is None:
            # nothing of the wanted polarity — stay coherent with the mood
            if want_ok is True:
                return {"text": "the structure that got me in is still my edge — "
                                "I'm holding for the move.", "polarity": "win"}
            if want_ok is False:
                return {"text": "the picture's gone murky — I don't love this; "
                                "be ready to act.", "polarity": "risk"}
            return {"text": "watching every factor — nothing decisive yet.",
                    "polarity": None}

    key = (chosen["name"], bool(chosen["ok"]))
    b = st.setdefault("b", {})
    cur_key = b.get("key")

    if cur_key is None:
        promote = True
    elif key == cur_key:
        promote = False
    else:
        if b.get("candidate") != key:
            b["candidate"] = key
            b["cand_since"] = now
        dwell_ok = (now - b.get("shown_ts", 0.0)) >= config.HEART_B_MIN_DWELL
        confirm_ok = (now - b.get("cand_since", now)) >= config.HEART_B_CONFIRM
        # a WIN↔RISK polarity flip is never noise — promote at once so Slot B
        # can never contradict Slot A's mood (the dwell only damps same-polarity
        # reshuffles, where holding the line IS the anti-noise win).
        polarity_flip = (cur_key[1] != key[1])
        promote = force_now or polarity_flip or (dwell_ok and confirm_ok)

    if promote and key != cur_key:
        b["key"] = key
        b["shown_ts"] = now
        b["idx"] = 0
        b["candidate"] = None
        cur_key = key

    # numbers stay live from the factor that matches the CURRENTLY-shown key
    held = by_name.get(cur_key[0])
    detail = (held or chosen).get("detail", "")
    zfac = by_name.get("Entry zone intact")
    zstr = zfac["detail"].replace("zone ", "") if zfac else ""

    variants = _WHY.get(cur_key) or [
        "this factor is " + ("with us." if cur_key[1] else "against us.")]
    # MAX HOLD: rotate phrasing on a held key so a steady trade doesn't read frozen
    if (now - b.get("shown_ts", 0.0)) >= config.HEART_B_MAX_HOLD:
        b["idx"] = (b.get("idx", 0) + 1) % len(variants)
        b["shown_ts"] = now
    text = _fill(variants[b.get("idx", 0) % len(variants)], z=zstr, detail=detail)
    return {"text": text, "polarity": "win" if cur_key[1] else "risk"}


def _classify_event(ev: Dict, bull: bool):
    """Map a radar event to (category, magnitude, lead-in text) for the held
    side, or None if it isn't material/significant enough."""
    tone = ev.get("tone")
    inst = (ev.get("instrument") or "").upper()
    mag = float(ev.get("magnitude", 0.0) or 0.0)
    # FAST-PATH: a gamma IGNITING event is pre-gated (rare, near-certain) — it
    # always flashes, framed for our side. Mood-coherence (don't red-flash a
    # trail-locked winner) is applied by the caller in _heart_slot_c.
    if ev.get("kind") == "blaster_igniting":
        helps = (tone == "bullish") if bull else (tone == "bearish")
        return {"cat": "confirm" if helps else "threat", "mag": mag,
                "tone": "bullish" if helps else "bearish",
                "lead_key": "blaster_confirm" if helps else "blaster_threat",
                "inst": "NIFTY", "text": ev.get("text", ""),
                "sig": "blaster|" + ("confirm" if helps else "threat")}
    is_basket = inst not in _NIFTY_INSTRUMENTS
    bar = config.HEART_C_MAG_BASKET if is_basket else 1.0
    if tone == "bullish":
        helps = bull
    elif tone == "bearish":
        helps = not bull
    else:
        helps = None
    if helps is True:                       # CONFIRM — higher bar
        if mag < config.HEART_C_MAG_CONFIRM * bar:
            return None
        cat, flash_tone = "confirm", "bullish"
    elif helps is False:                    # THREAT — lower bar, always material
        if mag < config.HEART_C_MAG_BASE * bar:
            return None
        cat, flash_tone = "threat", "bearish"
    else:                                   # warn / liquidity
        if mag < config.HEART_C_MAG_CONFIRM * bar:
            return None
        cat, flash_tone = "warn", "warn"
    lead_key = ("warn_generic" if cat == "warn"
                else f"{cat}_{'basket' if is_basket else 'nifty'}")
    return {"cat": cat, "mag": mag, "tone": flash_tone, "lead_key": lead_key,
            "inst": ev.get("instrument") or "", "text": ev.get("text", ""),
            "sig": f"{ev.get('kind')}|{inst}|{tone}"}


def _heart_slot_c(app, t, st: Dict, now: float):
    """Slot C — at most one fresh, directionally-material event flash per
    HEART_C_COOLDOWN, shown for HEART_C_TTL. None most of the time (by design)."""
    c = st.setdefault("c", {})
    # look for a NEW flash only once the global cooldown has elapsed
    if (now - c.get("ts", -1e9)) >= config.HEART_C_COOLDOWN:
        bull = (t.direction == "CE")
        wall = clk.now()
        cands = []
        try:
            radar_events = app.oi_radar.feed() + app.book_radar.feed()
        except Exception:
            radar_events = []
        for ev in radar_events:
            mts = ev.get("mts")
            if mts is None or (now - mts) > config.HEART_C_FRESH_SEC:
                continue
            r = _classify_event(ev, bull)
            if r:
                cands.append(r)
        try:
            cfeed = app.commentary.feed()
        except Exception:
            cfeed = []
        if cfeed:
            ev = cfeed[0]
            mts = ev.get("mts")
            up = (ev.get("text") or "").upper()
            ctone = ("bearish" if up.startswith("BEARISH")
                     else "bullish" if up.startswith("BULLISH") else None)
            if ctone and mts is not None and (wall - mts) <= config.HEART_C_FRESH_SEC:
                r = _classify_event(
                    {"tone": ctone, "instrument": "NIFTY", "kind": "comment",
                     "magnitude": config.HEART_C_MAG_CONFIRM,
                     "text": ev.get("text", "")}, bull)
                if r:
                    cands.append(r)
        if cands:
            cands.sort(key=lambda r: r["mag"], reverse=True)
            best = cands[0]
            # MOOD COHERENCE: never red-flash a winner whose +12 is already
            # trail-locked. A threat that fires against a secured trade becomes a
            # calm "let the trail decide" note, not a panic line.
            if best["lead_key"] == "blaster_threat" and getattr(t, "trail_active", False):
                c["sig"] = best["sig"]
                c["ts"] = now
                c["text"] = "Big move against us, but your trail is locked — let it decide, don't panic-book."
                c["tone"] = "warn"
            else:
                leads = _FLASH_LEAD.get(best["lead_key"], _FLASH_LEAD["warn_generic"])
                lead = _fill(leads[int(now) % len(leads)], inst=best["inst"])
                c["sig"] = best["sig"]
                c["ts"] = now
                c["text"] = lead + best["text"]
                c["tone"] = best["tone"]
    if c.get("text") and (now - c.get("ts", -1e9)) <= config.HEART_C_TTL:
        return {"text": c["text"], "tone": c["tone"]}
    return None


def _position_heart(app, t, cur: float, pnl: float) -> Dict:
    """Three slots: A (mood, owns colour/stance) + B (live 'why') + C (event
    flash). Shared clock & hysteresis live in app._heart so nothing strobes
    across the ~5 Hz price / 2 Hz full-state push vs 1 Hz analytics cadence."""
    now = clk.mono()
    with _HEART_LOCK:
        st = getattr(app, "_heart", None)
        if st is None or "a" not in st:
            st = {"a": None, "b": {}, "c": {}}
            app._heart = st
        a = _heart_slot_a(app, t, cur, pnl, st, now)
        conv = app.signals._pos_conv or {}
        b = _heart_slot_b(app, t, conv, pnl, a["mood"], st, now)
        c = _heart_slot_c(app, t, st, now)
        return {**a, "why": b, "flash": c}


def build_state(app) -> Dict:
    prices = app.prices
    spot, fut, atm, ce_ltp, pe_ltp = prices.freeze_core()
    dec = app.signals.last
    now = clk.now()

    # ── sentiment gauge: zone-hunt evidence balance (60%) + basket (40%) ─────
    engine_tilt = 50.0 + 50.0 * (dec.ce.ok_count - dec.pe.ok_count) / 8.0
    bull_score = 0.6 * engine_tilt + 0.4 * app.basket.sentiment

    # ── S/R ladder ────────────────────────────────────────────────────────────
    ladder = app.oi.ladder(atm) if atm > 0 else []
    supports = [{"level": z.level, "strength": z.strength, "oi": z.oi,
                 "building": z.building} for z in app.oi.support_zones]
    resists = [{"level": z.level, "strength": z.strength, "oi": z.oi,
                "building": z.building} for z in app.oi.resistance_zones]

    # ── open position (locked snapshots — exit thread mutates these lists) ───
    # A trade being WORKED for a cheaper fill is NOT a position — it goes to
    # `entering` (a distinct cockpit cue), never the position panel, and rings
    # no chime. Only a real FILL becomes a position. This is the fix for the
    # phantom-trade + premature-chime confusion.
    open_pos = []
    entering = None
    for t in app.trader.snapshot_open():
        cur = prices.option_price(t.strike, t.right)
        if getattr(t, "pending", False):
            wait_left = max(0, int(config.ENTRY_LIMIT_WAIT_SEC
                                   - (clk.mono() - t.limit_epoch)))
            entering = {
                "direction": t.direction, "strike": t.strike,
                "limit_price": _f(t.limit_price), "ltp": _f(cur),
                "lots": t.lots, "qty": t.qty, "wait_left": wait_left,
            }
            continue
        pnl_pts = cur - t.entry_price if cur > 0 else 0.0
        open_pos.append({
            "id": t.id, "direction": t.direction, "strike": t.strike,
            "lots": t.lots, "qty": t.qty, "pending": False,
            "limit_price": _f(t.limit_price),
            "entry_price": _f(t.entry_price), "entry_time": t.entry_time,
            "current": _f(cur), "pnl_pts": _f(pnl_pts),
            "pnl_cash": _f(pnl_pts * t.qty, 0),
            "stop_loss": _f(t.stop_loss),
            "target": _f(t.target),
            "trail_sl": _f(t.trail_sl) if t.trail_active else None,
            "peak": _f(t.peak_price - t.entry_price),
            "weakened": t.weakened,
            "age_sec": int(clk.mono() - t.entry_epoch),
            "live_score": _f(app.signals.live_score(t.direction)),
            "components": t.entry_components,
            "conviction": app.signals._pos_conv or None,
            "heart": _position_heart(app, t, cur, pnl_pts),
            "spot": _f(spot), "fut": _f(fut),
        })
    if not open_pos:
        with _HEART_LOCK:
            app._heart = None      # flat → no commentary, fresh start next trade

    # ── recent closed trades ──────────────────────────────────────────────────
    recent = []
    for t in app.trader.snapshot_closed(8)[::-1]:
        jv = app.journal.by_trade_id.get(t.id)
        recent.append({
            "id": t.id, "direction": t.direction, "strike": t.strike,
            "entry_time": t.entry_time, "exit_time": t.exit_time,
            "entry_price": _f(t.entry_price), "exit_price": _f(t.exit_price),
            "pnl_pts": _f(t.pnl_pts), "pnl_cash": _f(t.pnl_cash, 0),
            "reason": t.exit_reason, "lots": t.lots,
            "verdict": jv["verdict_line"] if jv else "",
            "mistake_class": (jv.get("mistake_class") if jv else
                              ("watching" if app.learning.watching(t.id) else "")),
            "entry_score": _f(getattr(t, "entry_score", 0.0)),
            "strike_delta_used": _f(getattr(t, "strike_delta_used", 0.0)),
        })

    # ── greeks for ATM ± position strike ─────────────────────────────────────
    greeks_panel = _greeks_panel(app, spot, atm)

    # ── feed health ───────────────────────────────────────────────────────────
    spot_age = (clk.mono() - prices.spot_ts) if prices.spot_ts else 9999
    health = {
        "status": app.status,
        "spot_age": _f(spot_age, 1),
        "opt_age": _f(prices.atm_option_age(), 1),
        "tick_count": prices.tick_count,
        "option_subs": len(prices.opt_ltp),
        "rest_calls": app.feed.rest_calls if app.feed else 0,
        "hw_live": sum(1 for ts in _copy(prices.hw_ts.values())
                       if clk.mono() - ts < 60),
        "uptime_min": int((now - app.started_at) / 60),
        "fut_ticks": prices.futures_ticks,
        "ticks_dropped": prices.fut_ticks_dropped,
        "fut_buffer": len(prices.fut_ticks),
        "chain_age": _f(clk.mono() - prices.chain_ts, 0) if prices.chain_ts else -1,
    }

    return {
        "ts": datetime.now(IST).strftime("%H:%M:%S"),
        "day": app.trader.day,
        "expiry": config.expiry_date(),
        "is_expiry_day": config.is_expiry_day(),
        "health": health,

        "market": {
            "spot": _f(spot), "futures": _f(fut), "atm": _f(atm, 0),
            "day_high": _f(getattr(app, "spot_day_high", 0.0)),
            "day_low": _f(getattr(app, "spot_day_low", 0.0)),
            "basis": _f(fut - spot, 1),
            "vix": _f(prices.vix, 2),
            "ce_ltp": _f(ce_ltp), "pe_ltp": _f(pe_ltp),
            "vwap": _f(app.flow.vwap.value),
            "pcr": _f(app.oi.pcr),
            "max_pain": _f(app.oi.max_pain, 0),
            "bull_score": _f(bull_score, 1),
            "basket_sentiment": _f(app.basket.sentiment, 1),
            "rsi": _f(app.flow.rsi.value, 1),
            "adx": _f(app.flow.adx.value, 1),
            "supertrend": app.flow.supertrend.direction,
            "cvd": _f(app.flow.cvd.value, 0),
            "cvd_slope": _f(app.flow.cvd.slope(60), 2),
            "fut_quadrant": app.flow.fut_oi.quadrant,
            "fut_oi_pct": _f(app.flow.fut_oi.oi_pct, 2),
            "avwap_high": _f(app.flow.avwap.from_high),
            "avwap_low": _f(app.flow.avwap.from_low),
            "spot_v": _f(app.signals.kin["spot"].v, 2),
            "spot_a": _f(app.signals.kin["spot"].a, 3),
            "sisters": [
                {"name": n, "ltp": _f(l),
                 "chg_pct": _f((l - prices.idx_prev.get(n, 0))
                               / prices.idx_prev[n] * 100, 2)
                 if prices.idx_prev.get(n, 0) > 0 else 0.0}
                for n, l in _copy(prices.idx_ltp.items()) if l > 0],
            "spot_hist": [[_f(t, 0), _f(s)] for t, s in _copy(app.spot_hist)[-300:]],
            "candles": _candles(app),
        },

        "signal": {
            "direction": dec.direction,
            "allowed": dec.allowed,
            "blocked": dec.blocked,
            "kind": dec.kind,
            "ce": _zone_view(dec.ce),
            "pe": _zone_view(dec.pe),
            "consec_sl": app.trader.consec_sl,
            "cooldown": _f(app.trader.cooldown_remaining(), 0),
        },

        "sr": {
            "ladder": ladder,
            "supports": supports,
            "resistances": resists,
            "hw_support": _f(app.basket.implied_support, 1),
            "hw_resistance": _f(app.basket.implied_resistance, 1),
            "oi_delta_hist": [[_f(t, 0), v] for t, v in
                              _copy(app.oi_delta_hist)[-300:]],
        },

        "vol": {
            "atm_iv": _f(app.vol.atm_iv * 100, 2),
            "iv_rank": _f(app.vol.iv_rank, 1),
            "iv_percentile": _f(app.vol.iv_percentile, 1),
            "skew": _f(app.vol.skew_25d, 2),
            "expected_move": _f(app.vol.expected_move, 1),
            "straddle": _f(app.vol.straddle, 1),
            "realized_vol": _f(app.vol.realized_vol_1m * 100, 2),
            "variance_premium": _f(app.vol.variance_premium, 2),
            "gex": _f(app.gex, 2),
            "gamma_heat": _f(app.gamma_heat, 3),
            "gamma_flip": _f(getattr(app, "gamma_flip", 0.0), 0),
            "gamma_stage": getattr(app, "gamma_stage", "idle"),
            "smile": _smile_safe(app, atm),
        },

        "chain": _premium_ladder(app, atm),
        "oi_flow": app.oi.multiframe(atm, spot) if atm > 0 else {},
        "greeks": greeks_panel,
        "position": open_pos,
        "entering": entering,
        "recent_trades": recent,
        "stats": app.trader.stats(),
        "equity_curve": app.trader.equity_curve(),
        "heavyweights": app.basket.rows(),
        "tug": _tug_of_war(app),
        "oi_radar": app.oi_radar.feed()[:25],
        "book_radar": app.book_radar.feed()[:25],
        "regime": dict(zip(("state", "note"), app.signals.market_state())),
        "market_memory": _memory_safe(app),
        "risk": _risk_safe(app),
        "battle_lines": _battle_safe(app),
        "commentary": app.commentary.feed(),
        "learning": {"recall": getattr(app, "_pending_recall", "") or "",
                     "eod": getattr(app.learning, "last_summary", ""),
                     "adaptive": app.learning.trust.snapshot(
                         getattr(app.learning, "gated_now", None)),
                     "safety_exits": getattr(app.trader, "_safety_exits", 0),
                     "doctrine_breaches": getattr(app.trader, "_doctrine_breaches", 0),
                     "gap_throughs": getattr(app.trader, "_gap_throughs", 0)},
        "events": _copy(app.events),
    }


def _candles(app, n: int = 45) -> list:
    """Last n 1-min futures candles [+ the live forming one] for the chart."""
    rows = [[_f(c.ts, 0), _f(c.open), _f(c.high), _f(c.low), _f(c.close)]
            for c in _copy(app.flow.candles_1m._candles)[-n:]]
    cur = app.flow.candles_1m.current
    if cur:
        rows.append([_f(cur.ts, 0), _f(cur.open), _f(cur.high),
                     _f(cur.low), _f(cur.close)])
    return rows


def _tug_of_war(app) -> dict:
    """Weighted bull-vs-bear force across the basket (official Nifty weights).
    bull_force = Σ weight×bias over stocks pulling up; bear mirror."""
    bull = bear = 0.0
    bulls, bears = [], []
    for s in _copy(app.basket.stocks.values()):   # analytics on_tick adds keys
        if s.ltp <= 0:
            continue
        f = s.weight * s.bias
        if f > 0:
            bull += f
            bulls.append((s.symbol, f))
        elif f < 0:
            bear += -f
            bears.append((s.symbol, -f))
    bulls.sort(key=lambda x: -x[1])
    bears.sort(key=lambda x: -x[1])
    total = bull + bear
    return {
        "bull_force": _f(bull, 2),
        "bear_force": _f(bear, 2),
        "bull_pct": _f(100 * bull / total, 1) if total > 0 else 50.0,
        "top_bulls": [{"symbol": s, "force": _f(f, 2)} for s, f in bulls[:3]],
        "top_bears": [{"symbol": s, "force": _f(f, 2)} for s, f in bears[:3]],
    }


def _zone_view(v) -> dict:
    return {
        "state": v.state,
        "kind": v.kind,
        "zone_level": _f(v.zone_level, 0),
        "zone_strength": _f(v.zone_strength, 2),
        "distance": _f(v.distance, 0),
        "evidence": [{"name": e.name, "ok": e.ok, "detail": e.detail}
                     for e in v.evidence],
        "ok_count": v.ok_count,
        "needed": v.needed,
        "sustain": v.sustain,
        "sustain_need": v.sustain_need,
        "premium_low": _f(v.premium_low),
        "premium_now": _f(v.premium_now),
    }


def _premium_ladder(app, atm: float, n: int = 4) -> list:
    """Live CE/PE premiums (with bid/ask + IV) for strikes around ATM —
    the user watches these numbers to mirror trades manually."""
    if atm <= 0:
        return []
    p = app.prices
    merged_oi = p.merged_oi()
    rows = []
    for off in range(-n, n + 1):
        k = atm + off * config.STRIKE_STEP
        ce_iv = app.vol.chain_iv.get((k, "call"))
        pe_iv = app.vol.chain_iv.get((k, "put"))
        ce_oi = merged_oi.get((k, "call"), 0.0)
        pe_oi = merged_oi.get((k, "put"), 0.0)
        rows.append({
            "strike": _f(k, 0),
            "is_atm": off == 0,
            "ce_ltp": _f(p.opt_ltp.get((k, "call"), 0)),
            "ce_bid": _f(p.opt_bid.get((k, "call"), 0)),
            "ce_ask": _f(p.opt_ask.get((k, "call"), 0)),
            "ce_iv": _f(ce_iv * 100, 1) if ce_iv else None,
            "ce_oi": _f(ce_oi, 0),
            "pe_ltp": _f(p.opt_ltp.get((k, "put"), 0)),
            "pe_bid": _f(p.opt_bid.get((k, "put"), 0)),
            "pe_ask": _f(p.opt_ask.get((k, "put"), 0)),
            "pe_iv": _f(pe_iv * 100, 1) if pe_iv else None,
            "pe_oi": _f(pe_oi, 0),
            "pcr": _f(pe_oi / ce_oi, 2) if ce_oi > 0 else None,
        })
    return rows


def _greeks_panel(app, spot: float, atm: float) -> dict:
    """ATM CE/PE greeks + net position greeks."""
    out = {"atm_ce": None, "atm_pe": None, "position": None}
    if spot <= 0 or atm <= 0:
        return out
    from . import greeks as gk
    T = gk.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))

    for right, key in (("call", "atm_ce"), ("put", "atm_pe")):
        ltp = app.prices.option_price(atm, right)
        g = gk.single_greeks(ltp, spot, atm, T, right) if ltp > 0 else None
        if g:
            out[key] = {"iv": _f(g.iv * 100, 2), "delta": _f(g.delta, 3),
                        "gamma": _f(g.gamma, 5), "theta": _f(g.theta, 2),
                        "vega": _f(g.vega, 2)}

    for t in app.trader.snapshot_open():   # 4Hz exit loop pops trader.open
        ltp = app.prices.option_price(t.strike, t.right)
        g = gk.single_greeks(ltp, spot, t.strike, T, t.right) if ltp > 0 else None
        if g:
            out["position"] = {
                "label": f"{t.direction} {t.strike:.0f} × {t.qty}",
                "delta": _f(g.delta * t.qty, 1),
                "gamma": _f(g.gamma * t.qty, 3),
                "theta": _f(g.theta * t.qty, 0),
                "vega": _f(g.vega * t.qty, 0),
            }
    return out
