"""
MYTHOS — daily archiving (Requirement §11).

At 15:30 the day's trades auto-export to archive/trades_YYYY-MM-DD.csv + .json;
the dashboard's "Archive Day" button calls the same function on demand.
"""

import csv
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from typing import List

from . import config
from .config import IST

log = logging.getLogger("mythos.archive")

_FIELDS = ["id", "direction", "strike", "lots", "qty", "entry_time",
           "entry_price", "exit_time", "exit_price", "exit_reason",
           "pnl_pts", "pnl_cash", "entry_score", "peak_price"]


def archive_day(trader, day: str = "") -> str:
    """Export the given (default: current) day's closed trades + stats.
    Returns the path of the JSON archive, '' if nothing to archive."""
    day = day or trader.day
    trades = [asdict(t) for t in trader.closed]
    if not trades:
        return ""
    stats = trader.stats()
    base = os.path.join(config.ARCHIVE_DIR,
                        f"{config.ARCHIVE_PREFIX}trades_{day}")

    with open(base + ".json", "w") as f:
        json.dump({"day": day, "stats": stats, "trades": trades}, f, indent=2)

    with open(base + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)

    log.info("Archived %d trades for %s → %s.{json,csv}", len(trades), day, base)
    return base + ".json"


class AutoArchiver:
    """Fires archive_day once after market close."""

    def __init__(self, trader):
        self.trader = trader
        self._done_for = ""

    def tick(self):
        now = datetime.now(IST)
        c_h, c_m = config.MARKET_CLOSE
        if (now.hour * 60 + now.minute) >= (c_h * 60 + c_m):
            day = self.trader.day
            if self._done_for != day and self.trader.closed:
                archive_day(self.trader, day)
                self._done_for = day
