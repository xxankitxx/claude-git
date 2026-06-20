#!/usr/bin/env python3
"""
MYTHOS — daily archive command.

Fire this once a day (e.g. after the close) to PRESERVE the day's record without
ever losing the LEARNING:

    python archive_day.py                 # archive today's logs + trades; back up learning
    python archive_day.py --date 2026-06-14
    python archive_day.py --rotate        # ...and start a fresh mythos.log for tomorrow
    python archive_day.py --with-db       # ...also snapshot the SQLite DB

What it does (all COPIES — nothing live is destroyed):
  • snapshots  logs/mythos.log(+rotated backups)  → archive/<day>/logs/
  • snapshots  data/trades_today.json             → archive/<day>/trades.json (+ trades.csv)
  • BACKS UP   data/mistake_journal.json           → archive/<day>/learning_backup/
               data/adaptive_state.json            → archive/<day>/learning_backup/

CRITICAL GUARANTEE: the LEARNING files (mistake_journal.json, adaptive_state.json)
are only ever COPIED — they are NEVER moved, cleared, or deleted, so tomorrow's
session keeps every lesson and the per-zone trust it has built. The real-time
logs are archived, not deleted. (Capital + the day's trade file reset themselves
on tomorrow's first run via the normal day-roll; the learning is deliberately
separate from that reset.)
"""

import argparse
import csv
import glob
import io
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from mythos import config
from mythos.config import IST

# the files that ARE the learning — these must survive forever. Copied, never moved.
LEARNING_FILES = [config.MISTAKE_JOURNAL_JSON, config.ADAPT_STATE_JSON]

_TRADE_FIELDS = ["id", "direction", "strike", "lots", "qty", "entry_time",
                 "entry_price", "exit_time", "exit_price", "exit_reason",
                 "pnl_pts", "pnl_cash", "entry_score", "strike_delta_used",
                 "peak_price"]


def _copy(src: str, dst_dir: str) -> bool:
    """Copy src into dst_dir (preserving name). Returns True if copied."""
    if not src or not os.path.exists(src):
        return False
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(dst_dir, os.path.basename(src)))
    return True


def _learning_summary() -> str:
    """Read (never write) the learning files for a confirmation line."""
    bits = []
    try:
        with io.open(config.MISTAKE_JOURNAL_JSON, encoding="utf-8") as f:
            n = len(json.load(f).get("entries", []))
        bits.append(f"journal {n} graded trades")
    except Exception:
        bits.append("journal (none yet)")
    try:
        with io.open(config.ADAPT_STATE_JSON, encoding="utf-8") as f:
            d = json.load(f)
        bits.append(f"trust {len(d.get('ctx', {}))} zones @ global {d.get('global_ema', 0.5):.2f}")
    except Exception:
        bits.append("trust (none yet)")
    return " · ".join(bits)


def archive(day: str, rotate: bool, with_db: bool) -> int:
    out = os.path.join(config.ARCHIVE_DIR, day)
    os.makedirs(out, exist_ok=True)
    actions = []

    # 1. logs — copy mythos.log + every rotated backup (mythos.log.1, .2, ...)
    log_glob = os.path.join(config.LOG_DIR, "mythos.log*")
    logs = sorted(glob.glob(log_glob))
    log_dst = os.path.join(out, "logs")
    n_logs = sum(1 for p in logs if _copy(p, log_dst))
    if n_logs:
        actions.append(f"{n_logs} log file(s) → {os.path.relpath(log_dst)}")

    # 2. trades — copy the raw day file + emit a CSV of closed trades
    if _copy(config.TRADES_JSON, out):
        actions.append(f"trades_today.json → {os.path.relpath(os.path.join(out, 'trades_today.json'))}")
        try:
            with io.open(config.TRADES_JSON, encoding="utf-8") as f:
                closed = (json.load(f) or {}).get("closed", [])
            if closed:
                with io.open(os.path.join(out, "trades.csv"), "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=_TRADE_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    for t in closed:
                        w.writerow(t)
                actions.append(f"{len(closed)} closed trades → trades.csv")
        except Exception as e:
            print(f"  (trade CSV skipped: {e})")

    # 3. LEARNING — BACK UP only. NEVER moved/removed from data/.
    lb = os.path.join(out, "learning_backup")
    n_learn = sum(1 for p in LEARNING_FILES if _copy(p, lb))
    if n_learn:
        actions.append(f"{n_learn} learning file(s) BACKED UP → {os.path.relpath(lb)} (originals untouched)")

    # 4. optional DB snapshot
    if with_db and _copy(config.DB_PATH, out):
        actions.append(f"DB snapshot → {os.path.relpath(os.path.join(out, os.path.basename(config.DB_PATH)))}")

    # 5. optional fresh log (AFTER the copy above) — truncate, do not delete the handle
    if rotate:
        live_log = os.path.join(config.LOG_DIR, "mythos.log")
        try:
            if os.path.exists(live_log):
                open(live_log, "w", encoding="utf-8").close()   # truncate in place
                actions.append("mythos.log truncated for a fresh day (archived copy kept)")
        except Exception as e:
            print(f"  (log rotate skipped — is a session running and holding it? {e})")

    print(f"\n  MYTHOS daily archive — {day}")
    print(f"  archive folder: {os.path.relpath(out)}")
    for a in actions:
        print(f"    ✓ {a}")
    if not actions:
        print("    (nothing to archive — no logs/trades found for this day)")
    print(f"\n  LEARNING PRESERVED (still live in data/, NOT archived away):")
    print(f"    {_learning_summary()}")
    print("    → tomorrow's session keeps every lesson + per-zone trust.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description="MYTHOS daily archive (preserves learning).")
    ap.add_argument("--date", default="", help="day to label the archive (default: today IST)")
    ap.add_argument("--rotate", action="store_true",
                    help="truncate the live mythos.log after archiving (fresh log for tomorrow)")
    ap.add_argument("--with-db", action="store_true", help="also snapshot the SQLite DB")
    args = ap.parse_args()
    day = args.date or datetime.now(IST).strftime("%Y-%m-%d")
    return archive(day, args.rotate, args.with_db)


if __name__ == "__main__":
    sys.exit(main())
