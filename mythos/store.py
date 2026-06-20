"""
MYTHOS — SQLite persistence.

What is stored (and why — nothing is stored that nothing reads):
    candles_1m   : futures 1-min OHLCV     → warm indicator restart mid-session
    iv_samples   : ATM IV ~1/min           → IV rank/percentile across days
    oi_snapshots : per-strike OI ~1/min    → OI-delta-flow history, research
    trades       : every closed paper trade → performance stats, archive

All writes funnel through a single writer thread via a queue — SQLite is
happy with one writer, and the analytics thread never blocks on disk.
"""

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import config
from .flow import Candle

log = logging.getLogger("mythos.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_1m (
    day TEXT, ts REAL, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (day, ts)
);
CREATE TABLE IF NOT EXISTS iv_samples (
    day TEXT, ts REAL, atm_iv REAL,
    PRIMARY KEY (day, ts)
);
CREATE TABLE IF NOT EXISTS oi_snapshots (
    day TEXT, ts REAL, strike REAL, right TEXT, oi REAL,
    PRIMARY KEY (day, ts, strike, right)
);
CREATE TABLE IF NOT EXISTS trades (
    day TEXT, trade_id INTEGER, payload TEXT,
    PRIMARY KEY (day, trade_id)
);
CREATE TABLE IF NOT EXISTS frames (
    day TEXT, ts REAL, frame TEXT,
    PRIMARY KEY (day, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_day ON oi_snapshots(day);
CREATE INDEX IF NOT EXISTS idx_frames_day ON frames(day);
"""


class Store:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self._q: queue.Queue = queue.Queue(maxsize=10000)
        self.dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer, daemon=True,
                                        name="StoreWriter")
        # init schema on the calling thread (connection then closed)
        con = sqlite3.connect(self.path)
        con.executescript(_SCHEMA)
        con.commit()
        con.close()
        self._thread.start()

    # ── async write API ──────────────────────────────────────────────────────
    def _put(self, sql: str, params: tuple):
        try:
            self._q.put_nowait((sql, params))
        except queue.Full:
            # drop rather than block the analytics thread — but NEVER silently
            # (a slow disk once ate a minute of candles with no trace)
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("store queue full — %d writes dropped so far",
                            self.dropped)

    def save_candle(self, day: str, c: Candle):
        self._put("INSERT OR REPLACE INTO candles_1m VALUES (?,?,?,?,?,?,?)",
                  (day, c.ts, c.open, c.high, c.low, c.close, c.volume))

    def save_iv(self, day: str, ts: float, atm_iv: float):
        self._put("INSERT OR REPLACE INTO iv_samples VALUES (?,?,?)",
                  (day, ts, atm_iv))

    def save_frame(self, day: str, ts: float, frame: dict):
        """Flight recorder: a full market snapshot, replayable later."""
        self._put("INSERT OR REPLACE INTO frames VALUES (?,?,?)",
                  (day, ts, json.dumps(frame)))

    def load_frames(self, day: str):
        """Ordered (ts, frame_dict) for a day — for the replay harness."""
        con = self._read_con()
        try:
            rows = con.execute(
                "SELECT ts, frame FROM frames WHERE day=? ORDER BY ts",
                (day,)).fetchall()
            return [(r[0], json.loads(r[1])) for r in rows]
        finally:
            con.close()

    def frame_days(self):
        con = self._read_con()
        try:
            return [r[0] for r in con.execute(
                "SELECT DISTINCT day FROM frames ORDER BY day").fetchall()]
        finally:
            con.close()

    def save_oi_snapshot(self, day: str, ts: float,
                         oi: Dict[Tuple[float, str], float]):
        for (k, r), v in oi.items():
            self._put("INSERT OR REPLACE INTO oi_snapshots VALUES (?,?,?,?,?)",
                      (day, ts, k, r, v))

    def save_trade(self, day: str, trade_id: int, payload: dict):
        self._put("INSERT OR REPLACE INTO trades VALUES (?,?,?)",
                  (day, trade_id, json.dumps(payload)))

    def _writer(self):
        con = sqlite3.connect(self.path)
        last_commit = time.monotonic()
        while not self._stop.is_set():
            try:
                sql, params = self._q.get(timeout=1.0)
                con.execute(sql, params)
                if time.monotonic() - last_commit > 2.0:
                    con.commit()
                    last_commit = time.monotonic()
            except queue.Empty:
                if time.monotonic() - last_commit > 2.0:
                    con.commit()
                    last_commit = time.monotonic()
            except Exception as e:
                log.warning("store write failed: %s", e)
        con.commit()
        con.close()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5.0)

    # ── sync read API (startup only) ─────────────────────────────────────────
    def _read_con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load_today_candles(self, day: str) -> List[Candle]:
        con = self._read_con()
        try:
            rows = con.execute(
                "SELECT ts, open, high, low, close, volume FROM candles_1m "
                "WHERE day=? ORDER BY ts", (day,)).fetchall()
            return [Candle(*r) for r in rows]
        finally:
            con.close()

    def load_daily_iv_closes(self, days: int = config.IV_HISTORY_DAYS) -> List[float]:
        """Last ATM IV sample of each prior day."""
        con = self._read_con()
        try:
            rows = con.execute(
                "SELECT day, atm_iv FROM iv_samples WHERE ts IN "
                "(SELECT MAX(ts) FROM iv_samples GROUP BY day) "
                "ORDER BY day DESC LIMIT ?", (days,)).fetchall()
            return [r[1] for r in rows if r[1] and r[1] > 0]
        finally:
            con.close()

    def load_today_iv(self, day: str) -> List[Tuple[float, float]]:
        con = self._read_con()
        try:
            rows = con.execute(
                "SELECT ts, atm_iv FROM iv_samples WHERE day=? ORDER BY ts",
                (day,)).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            con.close()

    def load_trades(self, day: str) -> List[dict]:
        con = self._read_con()
        try:
            rows = con.execute(
                "SELECT payload FROM trades WHERE day=? ORDER BY trade_id",
                (day,)).fetchall()
            return [json.loads(r[0]) for r in rows]
        finally:
            con.close()

    def load_all_days_pnl(self) -> List[dict]:
        """Per-day aggregate for the performance panel's history strip."""
        con = self._read_con()
        try:
            rows = con.execute("SELECT day, payload FROM trades").fetchall()
        finally:
            con.close()
        days: Dict[str, dict] = {}
        for day, payload in rows:
            t = json.loads(payload)
            d = days.setdefault(day, {"day": day, "trades": 0, "pnl_cash": 0.0,
                                      "wins": 0})
            d["trades"] += 1
            d["pnl_cash"] += t.get("pnl_cash", 0.0)
            if (t.get("pnl_pts") or 0) >= 0:
                d["wins"] += 1
        return sorted(days.values(), key=lambda x: x["day"])
