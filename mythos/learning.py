"""
MYTHOS — the learning loop: MYTHOS grades its OWN trades and ACTS on it.

The user is psychologically prone to booking winners too early and holding
losers too long, and wants the system to "streamline gradually into a better
system." This module is the closed feedback loop that does it:

  POST-EXIT PATH WATCH — after every exit, sample the option for the full
      POSTEXIT_WATCH_SEC and capture the TRUE running max favourable excursion
      AFTER our exit (fresh quotes only). So "did it run further after we booked"
      is measured on the real path, not one snapshot (a +30s spike that fades by
      +60s is now caught).
  VERDICT — a factual, sharp grade (BOOKED_EARLY / HELD_LOSER / CLEAN_WIN /
      CLEAN_LOSS / GREY / SAFETY_EXIT). GREY when it genuinely can't tell.
  PERSISTENT JOURNAL — survives the day-roll. FIFO-capped.
  TrustBook — the CLOSED LOOP. A bounded, per-(zone,direction) EMA of
      DOCTRINE-CLEAN outcomes (+12 vs a −10 WITH a broken thesis). It raises the
      entry evidence bar on contexts that keep failing relative to the book
      average — gradually, reversibly, never touching an exit, never able to
      starve all trading. An HONEST non-runner −10 (thesis intact) teaches
      NOTHING — that is the signal the user wants visible, not a penalty.
  RECALL / EOD — the spoken-narration channels (unchanged).

No background timers: everything piggybacks the 1 Hz analytics tick (clk-virtual,
so it all compresses correctly under SIM2 10x).
"""

import json
import logging
import os
import threading
from collections import Counter, deque
from datetime import datetime

from . import clk, config

log = logging.getLogger("mythos.learning")

RECALL_MAXLEN = 90      # module literals — never tuned
VERDICT_MAXLEN = 160


def _pool(pool, now):
    return pool[int(now) % len(pool)]


def _fill(tpl, **kw):
    out = tpl
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _today_str() -> str:
    return datetime.now(config.IST).strftime("%Y-%m-%d")


_VERDICT = {
    "BOOKED_EARLY": [
        "Booked +{pts}, it ran to +{ran}. You grabbed the slice and left the table — "
        "that's the exact leak we're plugging.",
        "+{pts} banked, +{ran} on offer. You were right; you just got scared early. "
        "The trail existed to hold this."],
    "HELD_LOSER": [
        "{detail} broke and you sat hoping for cost — {pts} pts gone. A small loss became "
        "a real one. Cut at the rail next time.",
        "Thesis was already broken ({detail}) and you held into the stop. {pts} pts. "
        "That was the exit, not the −10."],
    "CLEAN_WIN": [
        "Trailed out at +{pts} on {reason} — nothing left on the table. Exactly the read, "
        "exactly the exit.",
        "+{pts} clean on {reason}. The trail carried it and you let it. That's the "
        "discipline compounding."],
    "GOOD_EXIT": [
        "Clean +{pts} on {reason} — it reversed within minutes of the exit. You got out at "
        "the top; that timing was the read.",
        "+{pts} and the move gave it all back right after — the exit was well-judged, "
        "nothing left on the table."],
}
_RECALL = [   # appended to the fill WHY note — fill-only, silent
    "RECALL: last {n} {dir} entries at {z} you {klass} — let this one reach the trail.",
    "RECALL: {z} {dir} has burned you {n}× the same way ({klass}). Hold your nerve this time.",
]
_KLASS_HUMAN = {"BOOKED_EARLY": "booked early", "HELD_LOSER": "held the loser",
                "GREY": "unclear", "CLEAN_WIN": "clean win", "CLEAN_LOSS": "clean loss",
                "SAFETY_EXIT": "safety exit", "GOOD_EXIT": "good exit (it reversed)"}


def _bucket(level: float) -> float:
    return round(level / config.STRIKE_STEP) * config.STRIKE_STEP


class MistakeJournal:
    """Persistent, day-stamped, FIFO-capped store of graded exits."""

    def __init__(self, path: str):
        self.path = path
        self.entries: list = []
        self.by_trade_id: dict = {}

    def load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = (data.get("entries", []))[-config.JOURNAL_MAX_ENTRIES:]
            self.by_trade_id = {e["trade_id"]: e for e in self.entries
                                if e.get("trade_id") is not None}
            log.info("Journal restored: %d graded trades", len(self.entries))
        except Exception as e:
            log.warning("journal load failed: %s", e)

    def append(self, entry: dict):
        self.entries.append(entry)
        if len(self.entries) > config.JOURNAL_MAX_ENTRIES:
            self.entries = self.entries[-config.JOURNAL_MAX_ENTRIES:]
            self.by_trade_id = {e["trade_id"]: e for e in self.entries
                                if e.get("trade_id") is not None}
        else:
            if entry.get("trade_id") is not None:
                self.by_trade_id[entry["trade_id"]] = entry
        self._save()

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": 2, "entries": self.entries}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())          # durable before the atomic swap
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("journal save failed: %s", e)

    def recall(self, direction: str, zone_bucket: float):
        hits = [e for e in self.entries
                if e.get("direction") == direction
                and e.get("zone_bucket") == zone_bucket
                and e.get("mistake_class") in ("BOOKED_EARLY", "HELD_LOSER")]
        if len(hits) < config.RECALL_REPEAT_MIN:
            return None
        last = hits[-1]
        return _fill(_pool(_RECALL, clk.now()), n=len(hits), dir=direction,
                     z=f"{zone_bucket:.0f}",
                     klass=_KLASS_HUMAN.get(last["mistake_class"], "the same way"))[:RECALL_MAXLEN]

    def today(self, day: str):
        return [e for e in self.entries if e.get("day") == day]


class TrustBook:
    """The CLOSED ADAPTIVE LOOP. Per-(direction, zone_bucket) EMA of
    DOCTRINE-CLEAN outcomes, used ONLY to raise (never lower) the entry evidence
    bar on contexts failing relative to the book. Bounded, gradual, decaying,
    persistent, and provably unable to starve all trading or touch an exit."""

    def __init__(self, path: str):
        self.path = path
        self.ctx: dict = {}                       # "DIR|ZB" -> {"ema","n","last_seen_mono"}
        self.global_ema: float = config.ADAPT_PRIOR
        self.global_n: int = 0                    # total graded outcomes (for the book brake)
        self.last_day: str = ""
        self._lock = threading.Lock()

    @staticmethod
    def _key(direction: str, zb: float) -> str:
        return f"{direction}|{zb:.0f}"

    def load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.ctx = d.get("ctx", {})
            self.global_ema = d.get("global_ema", config.ADAPT_PRIOR)
            self.global_n = d.get("global_n", 0)
            self.last_day = d.get("last_day", "")
            log.info("TrustBook restored: %d contexts, global_ema %.2f",
                     len(self.ctx), self.global_ema)
        except Exception as e:
            log.warning("trustbook load failed: %s", e)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": 1, "global_ema": round(self.global_ema, 4),
                           "global_n": self.global_n,
                           "last_day": self.last_day, "ctx": self.ctx}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())          # durable before the atomic swap
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("trustbook save failed: %s", e)

    def _reward(self, t, cls):
        """ONLY a clean doctrine outcome teaches trust. +12 → 1.0; a −10 WITH a
        broken thesis (graded HELD_LOSER) → 0.0; an HONEST non-runner −10 (thesis
        intact) and everything ambiguous → None (no update — that is the visible
        bad-entry signal the user wants, not a zone penalty)."""
        if t.exit_reason in config.SAFETY_EXIT_REASONS:
            return None
        if t.pnl_pts >= config.TARGET_POINTS - 1.5:            # reached +12
            return 1.0
        if t.pnl_pts <= -(config.SL_POINTS - 1.5):             # took the −10
            return 0.0 if cls == "HELD_LOSER" else None
        return None                                            # GREY / ambiguous

    def update(self, direction, zb, reward, now_mono):
        if reward is None:
            return
        with self._lock:
            a = config.ADAPT_EMA_ALPHA
            c = self.ctx.setdefault(self._key(direction, zb),
                                    {"ema": config.ADAPT_PRIOR, "n": 0,
                                     "last_seen_mono": now_mono})
            c["ema"] = (1 - a) * c["ema"] + a * reward        # convex ⇒ ema ∈ [0,1]
            c["n"] += 1
            c["last_seen_mono"] = now_mono
            self.global_ema = (1 - a) * self.global_ema + a * reward
            self.global_n += 1
        self.save()

    def book_brake(self) -> int:
        """ABSOLUTE brake (the fix for the 74-trade wipeout): when the whole book
        is a proven net loser, demand MUCH more evidence on EVERY entry so only
        the very best setups fire — the engine stops digging. Stacks with the
        per-context bump; releases the moment wins lift global_ema back up. 0
        until ADAPT_BOOK_MIN_N graded outcomes (anti-noise). Evidence-based, not
        a capital stop."""
        if not config.ADAPT_ENTRY_GATE_ON or self.global_n < config.ADAPT_BOOK_MIN_N:
            return 0
        if self.global_ema < config.ADAPT_BOOK_FLOOR2:
            return config.ADAPT_BOOK_BRAKE2
        if self.global_ema < config.ADAPT_BOOK_FLOOR:
            return config.ADAPT_BOOK_BRAKE
        return 0

    def trust_gate(self, direction, zb) -> int:
        """Bump ∈ {0,1,2}. NEVER lowers the bar, NEVER skips. RELATIVE to the book
        average (a uniform sim-bias depresses all contexts equally → no bumps)."""
        if not config.ADAPT_ENTRY_GATE_ON:
            return 0
        c = self.ctx.get(self._key(direction, zb))
        if c is None or c["n"] < config.ADAPT_MIN_SAMPLES:    # anti-fluke
            return 0
        if self._throttled():                                 # base rate moved book-wide
            return 0
        gap = self.global_ema - c["ema"]                      # RELATIVE, not absolute
        if gap >= config.ADAPT_REL_BUMP2:
            return min(2, config.ADAPT_BUMP_MAX)
        if gap >= config.ADAPT_REL_BUMP1:
            return min(1, config.ADAPT_BUMP_MAX)
        return 0

    def _throttled(self) -> bool:
        elig = [c for c in self.ctx.values() if c["n"] >= config.ADAPT_MIN_SAMPLES]
        if not elig:
            return False
        bumped = sum(1 for c in elig
                     if self.global_ema - c["ema"] >= config.ADAPT_REL_BUMP1)
        return bumped / len(elig) > config.ADAPT_GLOBAL_THROTTLE

    def maybe_decay(self, day: str):
        """Once per virtual-day boundary: revert each ema toward the neutral
        prior and shrink n, so a tightened context self-heals and an unvisited
        one falls back below the min-sample gate. Idempotent within a day."""
        if day == self.last_day:
            return
        with self._lock:
            self.last_day = day
            for c in self.ctx.values():
                c["ema"] += (config.ADAPT_PRIOR - c["ema"]) * config.ADAPT_DECAY
                c["n"] = int(c["n"] * config.ADAPT_SAMPLE_DECAY)
            self.global_ema += (config.ADAPT_PRIOR - self.global_ema) * config.ADAPT_DECAY
            self.ctx = {k: c for k, c in self.ctx.items() if c["n"] > 0}   # bound the store
        self.save()

    def snapshot(self, gated_now=None):
        rows = []
        for k, c in list(self.ctx.items()):
            d, zb = k.split("|")
            rows.append({"dir": d, "zone_bucket": float(zb),
                         "ema": round(c["ema"], 2), "n": c["n"],
                         "bump": self.trust_gate(d, float(zb))})
        rows.sort(key=lambda r: r["ema"])
        return {"global_ema": round(self.global_ema, 2), "global_n": self.global_n,
                "book_brake": self.book_brake(), "contexts": rows[:8],
                "gated_now": gated_now}


class LearningLoop:
    def __init__(self, journal: MistakeJournal):
        self.journal = journal
        self.trust = TrustBook(config.ADAPT_STATE_JSON)
        self.trust.load()
        self.pending: deque = deque(maxlen=config.ADAPT_WATCH_MAXLEN)
        self._lock = threading.Lock()
        self._eod_done = ""
        self.last_summary = ""
        self.gated_now = None              # set by app each pass for the dashboard

    # ── post-exit watch ──────────────────────────────────────────────────────
    def on_exit(self, trade, pos_conv_frozen: dict, entry_zone: float, now_mono: float):
        zb = _bucket(entry_zone)
        with self._lock:
            # flood guard: one active ticket per (dir, zone) — finalize any open
            # duplicate on its partial path so its lesson is not silently evicted.
            for w in list(self.pending):
                if w["trade"].direction == trade.direction and _bucket(w["zone"]) == zb:
                    self._finalize(w, now_mono)
                    self.pending.remove(w)
            self.pending.append({
                "trade": trade, "conv": dict(pos_conv_frozen or {}),
                "zone": entry_zone, "opened_mono": now_mono,
                "watch_until": now_mono + config.POSTEXIT_WATCH_SEC_LONG,
                "exit_price": trade.exit_price, "mfe": trade.exit_price,
                "mae": trade.exit_price,            # running max-ADVERSE (for GOOD_EXIT)
                "mfe_epoch": now_mono, "fresh_samples": 0, "stale_samples": 0,
                "provisional_done": False})         # the 60s spoken early read
        log.info("learning: watching %s %g for %.0fs post-exit (multi-horizon)",
                 trade.direction, trade.strike, config.POSTEXIT_WATCH_SEC_LONG)

    def watching(self, trade_id: int) -> bool:
        with self._lock:
            return any(w["trade"].id == trade_id for w in self.pending)

    def tick(self, now_mono: float, prices):
        """Once per 1 Hz pass. Samples the FULL post-exit path of every open
        ticket (running max-favourable), finalizes at +POSTEXIT_WATCH_SEC.
        Returns a list of (entry, speak) finalized this pass (usually empty)."""
        self.trust.maybe_decay(_today_str())          # idempotent virtual-day decay
        out = []
        with self._lock:
            for w in list(self.pending):
                t = w["trade"]
                age = prices.option_age(t.strike, t.right)
                px = prices.option_price(t.strike, t.right)
                if age <= config.POSTEXIT_FRESH_SEC and px > 0:
                    w["fresh_samples"] += 1
                    if px > w["mfe"]:                  # TRUE running max after exit
                        w["mfe"] = px
                        w["mfe_epoch"] = now_mono
                    if px < w["mae"]:                  # TRUE running min after exit
                        w["mae"] = px
                else:
                    w["stale_samples"] += 1            # a drifted strike can't prove a run
                watch_age = now_mono - w["opened_mono"]
                # PROVISIONAL early read at 60s — spoken, not journaled; the FINAL
                # grade comes ~5 min later when the tape has fully shown its hand.
                if not w["provisional_done"] and watch_age >= config.POSTEXIT_WATCH_SEC:
                    w["provisional_done"] = True
                    out.append(({"verdict_line": self._provisional_line(w)}, True))
                if now_mono >= w["watch_until"]:
                    out.append(self._finalize(w, now_mono))
                    self.pending.remove(w)
        return out

    def _provisional_line(self, w) -> str:
        t = w["trade"]
        run = round(w["mfe"] - w["exit_price"], 1)
        tail = (f" — already +{run:.0f} beyond exit; watching the next few min for the "
                f"real grade" if run >= config.GREY_MFE_PTS
                else " — flat so far; watching the next few min before grading")
        return (f"EARLY READ {t.direction} {t.strike:.0f} {t.pnl_pts:+.0f}{tail}")[:VERDICT_MAXLEN]

    # ── the honest grader + the closed loop ──────────────────────────────────
    def _finalize(self, w, now_mono):
        t = w["trade"]
        conv = w["conv"]
        factors = conv.get("factors", []) if isinstance(conv, dict) else []
        broken = [f["name"] for f in factors if not f.get("ok")]
        broken_frac = (len(broken) / len(factors)) if factors else 0.0
        run_after = round(w["mfe"] - w["exit_price"], 1)   # TRUE path max beyond exit (full ~5m)
        give_back = round(w["exit_price"] - w.get("mae", w["exit_price"]), 1)  # max ADVERSE after exit
        win = t.pnl_pts > 0
        stale = w["fresh_samples"] < config.POSTEXIT_MIN_SAMPLES

        if t.exit_reason in config.SAFETY_EXIT_REASONS:
            cls, notable = "SAFETY_EXIT", False
            line = (f"{t.direction} {t.strike:.0f} {t.pnl_pts:+.0f} — safety exit "
                    f"({t.exit_reason}), not a chosen trade.")
        elif stale:
            cls, notable = "GREY", False
            line = (f"Couldn't grade {t.direction} {t.strike:.0f} — too few fresh "
                    f"post-exit quotes.")
        elif win and run_after >= config.BOOK_EARLY_PTS:
            cls, notable = "BOOKED_EARLY", True
            line = _fill(_pool(_VERDICT["BOOKED_EARLY"], clk.now()),
                         pts=f"{t.pnl_pts:.0f}", ran=f"{t.pnl_pts + run_after:.0f}")
        elif win and give_back >= config.GREY_MFE_PTS and run_after < config.GREY_MFE_PTS:
            # multi-horizon upgrade: a win that peaked at/near our exit then REVERSED
            # over the next few minutes — the exit was well-timed, not premature.
            cls, notable = "GOOD_EXIT", True
            line = _fill(_pool(_VERDICT["GOOD_EXIT"], clk.now()),
                         pts=f"{t.pnl_pts:.0f}", reason=t.exit_reason)
        elif (not win) and broken_frac >= config.BROKEN_FRAC:
            cls, notable = "HELD_LOSER", True
            line = _fill(_pool(_VERDICT["HELD_LOSER"], clk.now()),
                         pts=f"{t.pnl_pts:.0f}", detail=", ".join(broken[:2]) or "the thesis")
        elif abs(run_after) < config.GREY_MFE_PTS:
            cls, notable = "GREY", False
            line = (f"{t.direction} {t.strike:.0f} {t.pnl_pts:+.0f}: outcome unclear "
                    f"post-exit.")
        elif win:
            cls, notable = "CLEAN_WIN", True
            line = _fill(_pool(_VERDICT["CLEAN_WIN"], clk.now()),
                         pts=f"{t.pnl_pts:.0f}", reason=t.exit_reason)
        else:
            cls, notable = "CLEAN_LOSS", False
            line = f"Clean {t.pnl_pts:+.0f} — thesis broke, stop did its job."

        entry = {
            "v": 3, "day": _today_str(), "ts": clk.now(),
            "trade_id": t.id, "direction": t.direction, "strike": t.strike,
            "zone": w["zone"], "zone_bucket": _bucket(w["zone"]),
            "exit_reason": t.exit_reason, "pnl_pts": round(t.pnl_pts, 1),
            "mfe_after": (None if stale else run_after),
            "give_back_after": (None if stale else give_back),
            "horizon_sec": round(now_mono - w["opened_mono"], 0),   # ~300s multi-horizon
            "time_to_mfe": (None if stale else round(w["mfe_epoch"] - w["opened_mono"], 1)),
            "path_samples": w["fresh_samples"],
            "exit_px": round(w["exit_price"], 2),
            "entry_score": getattr(t, "entry_score", None),
            "strike_delta_used": getattr(t, "strike_delta_used", None),
            "mistake_class": cls, "verdict_line": line[:VERDICT_MAXLEN],
            "factors_broken": broken, "notable": notable,
        }
        self.journal.append(entry)
        # CLOSE THE LOOP — doctrine-clean reward only (None = no learning).
        reward = self.trust._reward(t, cls)
        if reward is not None:
            self.trust.update(t.direction, _bucket(w["zone"]), reward, now_mono)
        speak = notable and cls not in ("GREY", "SAFETY_EXIT")
        log.info("learning verdict #%d %s [%s] reward=%s: %s", t.id, cls,
                 "speak" if speak else "silent", reward, line[:70])
        return entry, speak

    # ── EOD summary (15:25 window, reads closed[] before the day-roll wipe) ──
    def eod_tick(self, now_ist, closed_trades, note):
        h, m = config.EOD_SUMMARY_AT
        if (now_ist.hour * 60 + now_ist.minute) < (h * 60 + m):
            return
        day = now_ist.strftime("%Y-%m-%d")
        if self._eod_done == day or not closed_trades:
            return
        self._eod_done = day
        today = self.journal.today(day)
        tally = Counter(e["mistake_class"] for e in today)
        wins = sum(1 for t in closed_trades if t.pnl_pts > 0)
        net = sum(t.pnl_pts for t in closed_trades)
        dom, dom_n = (tally.most_common(1)[0] if tally else ("", 0))
        pattern = (f" The pattern that cost you: {_KLASS_HUMAN.get(dom, dom)} ({dom_n}×)."
                   if dom_n >= 2 and dom in ("BOOKED_EARLY", "HELD_LOSER") else "")
        grey = tally.get("GREY", 0)
        grey_note = (f" {grey} grey — entry/sim edge unclear, run forensic_entry.py."
                     if grey >= 3 else "")
        self.last_summary = (f"EOD — {len(closed_trades)} trades, {wins} green, "
                             f"{net:+.0f} pts net.{pattern}{grey_note}")
        note(self.last_summary, kind="eod")
