"""
MYTHOS — persistent MARKET MEMORY (v1: DISPLAY-ONLY).

The long-horizon, cross-session memory of WHERE price has been fought over and HOW
the complex (Nifty + BankNifty + FinNifty + the heavyweight basket) is poised — so
the user can feel the market's NERVE and recall which levels are battle-tested
support/resistance across days. It accrues into human-readable external files that
survive restarts and improve the read over time.

SAFETY CONTRACT — load-bearing, do not break (designed + adversarially reviewed
2026-06-15; see memory `mythos-realtime-display-lessons`):
  • DISPLAY-ONLY. This module is imported ONLY by state.build_state. signals.py and
    trader.py MUST NOT import it — absence of the import (grep-provable) is the
    guarantee that memory can NEVER change a trade. Decision wiring is DEFERRED
    behind a record-and-measure-predictiveness gate (the entry-evidence lever is
    anti-predictive — proven three ways — so memory must EARN any decision role on
    live tape first).
  • OFF THE HOT PATH. The analytics thread only COPIES ~10 frozen scalars and
    put_nowait()s them (drops, never blocks). All folding + disk I/O + fsync run on
    a dedicated daemon writer (the store.py discipline). This module holds ZERO
    reference to any live engine deque/dict, so it cannot re-trigger the 2026-06-15
    cross-thread price-freeze.
  • BOUNDED + TEAR-PROOF. Files are capped + day-decayed; atomic tmp+fsync+replace
    (the learning.py idiom). A corrupt/missing file starts empty, never raises.
  • SIM-ISOLATED. Under --sim the dir is memory_sim/ (asserted in app.__init__) so a
    synthetic session can never poison the live ledger; sim-built levels are tagged.
"""
import json
import logging
import math
import os
import queue
import threading
from collections import deque

from . import clk, config

log = logging.getLogger("mythos.memory")

# instruments whose directional poise we track minutely (the "complex")
_POISE_WEIGHTS = {"NIFTY": 0.40, "BANKNIFTY": 0.25, "FINNIFTY": 0.10, "BASKET": 0.25}


def _atomic_write(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())          # durable before the atomic swap
        os.replace(tmp, path)
    except Exception as e:
        log.warning("memory write failed (%s): %s", path, e)


def _load(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("memory load failed (%s): %s — starting empty", path, e)
    return default


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class MarketMemory:
    """Persistent S/R level ledger (NIFTY) + multi-instrument poise/nerve state.
    DISPLAY-ONLY. Thread model: observe() is called from the analytics thread and
    only copy+enqueues; a daemon thread folds + writes; snapshot() is called from
    the WS-push thread and reads a locked copy. No shared mutable structure is ever
    iterated across threads."""

    def __init__(self, mem_dir: str):
        self.dir = mem_dir
        self.is_sim = "sim" in mem_dir
        self._levels_path = os.path.join(mem_dir, "levels_NIFTY.json")
        self._poise_path = os.path.join(mem_dir, "poise_state.json")
        self._daily_path = os.path.join(mem_dir, "poise_daily.jsonl")
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self.dropped = 0
        self._dirty = False
        self._last_flush = 0.0
        self._last_day = ""
        self._stop = threading.Event()

        # ── state ──
        lj = _load(self._levels_path, {})
        self.levels = lj.get("levels", {}) if isinstance(lj, dict) else {}
        self._last_day = (lj.get("updated_day", "") if isinstance(lj, dict) else "")
        pj = _load(self._poise_path, {})
        self.poise = pj.get("instruments", {}) if isinstance(pj, dict) else {}
        self.complex_read = (pj.get("complex", {}).get("read_text", "warming up")
                             if isinstance(pj, dict) else "warming up")
        self._daily = deque(maxlen=config.MEM_POISE_DAILY_MAX)
        for line in (_load_jsonl(self._daily_path)):
            self._daily.append(line)

        self._thread = threading.Thread(target=self._writer, name="MarketMemory",
                                        daemon=True)
        self._thread.start()
        log.info("MarketMemory ready (%s) — %d levels, %d poise instruments%s",
                 mem_dir, len(self.levels), len(self.poise),
                 " [SIM]" if self.is_sim else "")

    # ── hot-path API (analytics thread): copy + enqueue, never block ───────────
    def observe(self, obs: dict):
        """obs is a SMALL dict of already-frozen primitives (copied by the caller).
        Drops rather than blocks if the daemon is behind — a stale memory panel is
        always acceptable; a slowed analytics pass is not."""
        try:
            self._q.put_nowait(obs)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 200 == 1:
                log.warning("memory queue full — %d observations dropped", self.dropped)

    # ── day-roll (analytics thread, piggybacks roll_day_if_needed) ─────────────
    def maybe_decay(self, day: str):
        if day == self._last_day:
            return
        with self._lock:
            prev_day = self._last_day
            self._last_day = day
            # append yesterday's poise close to the rolling daily log
            if prev_day:
                cx = self._complex_score_locked()
                self._daily.append({"day": prev_day, "complex": round(cx, 3),
                                    "read": self.complex_read})
            # decay + prune levels
            dead = []
            for k, lv in self.levels.items():
                touched_today = lv.get("last_touch") == day
                if not touched_today:
                    lv["strength"] = lv.get("strength", 0.0) * (1 - config.MEM_DECAY_PER_DAY)
                # stale eviction
                staleness = _day_gap(lv.get("last_touch", ""), day)
                if lv["strength"] < 0.05 or staleness > config.MEM_STALE_DAYS:
                    dead.append(k)
            for k in dead:
                self.levels.pop(k, None)
            self._dirty = True
        self._flush(force=True)

    # ── render API (push thread): locked copy, no live iteration ───────────────
    def snapshot(self) -> dict:
        with self._lock:
            levels = []
            for lv in self.levels.values():
                tally = lv.get("tally", {})
                total = sum(tally.values())
                if total < 1:
                    continue
                conf = 1 - math.exp(-total / 3.0)        # one touch ≠ battle-tested
                surf = round(lv.get("strength", 0.0) * conf, 3)
                holds = tally.get("hold_s", 0) + tally.get("hold_r", 0)
                levels.append({
                    "level": lv["bucket"], "role": lv.get("role", "SUPPORT"),
                    "strength": surf, "holds": holds,
                    "sessions": lv.get("sessions_active", 1),
                    "tally": tally,
                    "launch": ("LAUNCHED" if lv.get("launch_q", 0.5) >= 0.5
                               else "STALLED"),
                    "sim": self.is_sim,
                })
            levels.sort(key=lambda x: -x["strength"])
            cx = self._complex_score_locked()
            poise = {k: {"score": round(v.get("slow", 0.0), 3),
                         "nerve": round(abs(v.get("fast", 0.0) - v.get("slow", 0.0)), 3)}
                     for k, v in self.poise.items()}
            daily = list(self._daily)[-12:]
            read = self.complex_read
        return {
            "levels": levels[:8],
            "complex_score": round(cx, 3),
            "complex_read": read,
            "poise": poise,
            "daily": [{"day": d.get("day", ""), "complex": d.get("complex", 0.0)}
                      for d in daily],
            "sim": self.is_sim,
        }

    def stop(self):
        self._stop.set()
        try:
            self._q.put_nowait(None)        # wake the writer
        except queue.Full:
            pass
        self._thread.join(timeout=3.0)
        self._flush(force=True)

    # ── daemon: fold observations + periodic flush (all heavy work off-thread) ──
    def _writer(self):
        while not self._stop.is_set():
            try:
                obs = self._q.get(timeout=1.0)
            except queue.Empty:
                self._flush()
                continue
            if obs is None:
                break
            try:
                self._fold(obs)
            except Exception as e:
                log.debug("memory fold failed (ancillary): %s", e)
            self._flush()

    def _flush(self, force: bool = False):
        now = clk.mono()
        if not force and (not self._dirty or now - self._last_flush < config.MEM_FLUSH_SEC):
            return
        with self._lock:
            if not self._dirty and not force:
                return
            levels = {"v": 1, "instrument": "NIFTY", "updated_day": self._last_day,
                      "levels": self.levels}
            poise = {"v": 1, "last_day": self._last_day,
                     "complex": {"score": round(self._complex_score_locked(), 3),
                                 "read_text": self.complex_read},
                     "instruments": self.poise}
            daily = list(self._daily)
            self._dirty = False
            self._last_flush = now
        _atomic_write(self._levels_path, levels)
        _atomic_write(self._poise_path, poise)
        _write_jsonl(self._daily_path, daily)

    # ── the fold: detect level events + update strength + poise (daemon only) ───
    def _fold(self, obs: dict):
        spot = obs.get("spot", 0.0)
        day = obs.get("day", "")
        if spot <= 0:
            return
        band = spot * config.MEM_LEVEL_BAND_PCT
        with self._lock:
            self._last_day = day or self._last_day
            for z in obs.get("zones", []):
                self._fold_level(z, spot, band, day)
            # bound the ledger
            if len(self.levels) > config.MEM_MAX_LEVELS:
                weak = sorted(self.levels.items(),
                              key=lambda kv: kv[1].get("strength", 0.0))
                for k, _ in weak[:len(self.levels) - config.MEM_MAX_LEVELS]:
                    self.levels.pop(k, None)
            self._fold_poise(obs)
            self._dirty = True

    def _fold_level(self, z, spot, band, day):
        # z = (kind, level, strength, oi, building)
        kind, level, zstr = z[0], float(z[1]), float(z[2])
        oi = float(z[3]) if len(z) > 3 else 0.0
        key = str(int(round(level)))
        lv = self.levels.get(key)
        if lv is None:
            lv = {"bucket": int(round(level)), "origins": ["OI_WALL"],
                  "role": kind, "strength": _clamp(0.3 + 0.4 * zstr, 0.0, 1.0),
                  "tally": {"touch": 0, "hold_s": 0, "hold_r": 0, "reject": 0,
                            "break_up": 0, "break_dn": 0},
                  "launch_q": 0.5, "first_seen": day, "last_touch": "",
                  "sessions_active": 1, "max_oi": oi, "events": [],
                  "_side": "", "_at_spot": 0.0, "_last_day_seen": day}
            self.levels[key] = lv
        # side of spot relative to the level
        side = "at" if abs(spot - level) <= band else ("above" if spot > level else "below")
        prev = lv.get("_side", "")
        lv["max_oi"] = max(lv.get("max_oi", 0.0), oi)
        # session counter: first event of a NEW day
        if lv.get("_last_day_seen") != day:
            lv["_last_day_seen"] = day
            lv["sessions_active"] = lv.get("sessions_active", 0) + 1
        ev = None
        if side == "at" and prev != "at":
            lv["tally"]["touch"] += 1
            lv["last_touch"] = day
            lv["_at_spot"] = spot
            ev = "TOUCH"
        elif prev in ("at", "below") and side == "above":
            # price tested then rose away — a SUPPORT HOLD (a bounce)
            if lv["role"] == "SUPPORT":
                self._reward(lv, zstr, spot, band)
                lv["tally"]["hold_s"] += 1
                lv["last_touch"] = day
                ev = "HOLD_SUP"
            else:                                   # rose THROUGH resistance = break
                self._break(lv, "SUPPORT")
                lv["tally"]["break_up"] += 1
                ev = "BREAK_UP"
        elif prev in ("at", "above") and side == "below":
            if lv["role"] == "RESISTANCE":
                self._reward(lv, zstr, spot, band)
                lv["tally"]["hold_r"] += 1
                lv["last_touch"] = day
                ev = "HOLD_RES"
            else:                                   # fell THROUGH support = break
                self._break(lv, "RESISTANCE")
                lv["tally"]["break_dn"] += 1
                ev = "BREAK_DN"
        lv["_side"] = side
        if ev and ev != "TOUCH":
            lv["events"].append({"d": day, "k": ev, "spot": round(spot, 1)})
            lv["events"] = lv["events"][-config.MEM_EVENTS_RING:]

    def _reward(self, lv, zstr, spot, band):
        reward = _clamp(0.5 + 0.5 * zstr, 0.0, 1.0)
        a = config.MEM_STRENGTH_ALPHA
        lv["strength"] = (1 - a) * lv.get("strength", 0.3) + a * reward
        # launch quality: how far price sprang from the level on the hold
        move = abs(spot - lv.get("_at_spot", spot))
        launch = _clamp(move / (3.0 * band) if band > 0 else 0.5, 0.0, 1.0)
        lv["launch_q"] = (1 - a) * lv.get("launch_q", 0.5) + a * launch

    def _break(self, lv, new_role):
        lv["role"] = new_role                       # role reversal
        lv["strength"] = lv.get("strength", 0.3) * 0.35

    def _fold_poise(self, obs):
        now = obs.get("ts", clk.mono())
        scores = {
            "NIFTY": _clamp(obs.get("nifty_bias", 0.0), -1.0, 1.0),
            "BANKNIFTY": _clamp(obs.get("sisters", {}).get("BANKNIFTY", 0.0) / 0.5, -1, 1),
            "FINNIFTY": _clamp(obs.get("sisters", {}).get("FINNIFTY", 0.0) / 0.5, -1, 1),
            "BASKET": _clamp((obs.get("basket", 50.0) - 50.0) / 50.0, -1, 1),
        }
        for inst, sc in scores.items():
            st = self.poise.setdefault(inst, {"fast": 0.0, "slow": 0.0, "ts": now})
            dt = max(0.0, now - st.get("ts", now))
            af = 1 - 0.5 ** (dt / config.MEM_POISE_HALF_FAST) if dt > 0 else 0.1
            asl = 1 - 0.5 ** (dt / config.MEM_POISE_HALF_SLOW) if dt > 0 else 0.02
            st["fast"] = (1 - af) * st["fast"] + af * sc
            st["slow"] = (1 - asl) * st["slow"] + asl * sc
            st["ts"] = now
        self.complex_read = self._poise_read_locked()

    def _complex_score_locked(self) -> float:
        s = w = 0.0
        for inst, wt in _POISE_WEIGHTS.items():
            if inst in self.poise:
                s += wt * self.poise[inst].get("slow", 0.0)
                w += wt
        return s / w if w else 0.0

    def _poise_read_locked(self) -> str:
        cx = self._complex_score_locked()
        lean = ("BEARISH" if cx <= -0.5 else "bearish-leaning" if cx <= -0.15
                else "BULLISH" if cx >= 0.5 else "bullish-leaning" if cx >= 0.15
                else "BALANCED")
        # which instruments lead the lean (strongest agreeing slow score)
        agree = sorted(((abs(v.get("slow", 0.0)), k) for k, v in self.poise.items()
                        if (v.get("slow", 0.0) < 0) == (cx < 0) and abs(v.get("slow", 0)) > 0.15),
                       reverse=True)
        leaders = ", ".join(k for _, k in agree[:2]) or "no clear leader"
        nerve = max((abs(v.get("fast", 0.0) - v.get("slow", 0.0))
                     for v in self.poise.values()), default=0.0)
        steadiness = "jumpy" if nerve > 0.4 else "steady"
        return (f"Complex poised {lean} (score {cx:+.2f}) — led by {leaders}; "
                f"{steadiness} tape.")


# ── tiny jsonl helpers (bounded daily poise log) ───────────────────────────────
def _load_jsonl(path):
    out = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
    except Exception as e:
        log.warning("memory daily load failed: %s", e)
    return out[-config.MEM_POISE_DAILY_MAX:]


def _write_jsonl(path, rows):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows[-config.MEM_POISE_DAILY_MAX:]:
                f.write(json.dumps(r) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log.warning("memory daily write failed: %s", e)


def _day_gap(last_touch: str, day: str) -> int:
    """Whole-day gap between two YYYY-MM-DD strings; 0 if unknown/same."""
    if not last_touch or not day or last_touch == day:
        return 0
    try:
        from datetime import date
        a = date.fromisoformat(last_touch)
        b = date.fromisoformat(day)
        return abs((b - a).days)
    except Exception:
        return 0
