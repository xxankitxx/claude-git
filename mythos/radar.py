"""
MYTHOS — Activity Radars: the system's eyes on EVERYTHING, all the time.

Two dedicated feeds (user mandate: "highlight any significant change in OI …
in any strike of any instrument" + "significant change in buyers or sellers
in any contract"):

  OIRadar   — significant open-interest moves anywhere: every Nifty strike
              (WS + full REST chain), Nifty futures OI, and every strike of
              all 14 heavyweight option chains. Also flags volume spikes.
  BookRadar — significant buyer/seller pressure shifts: bid/ask QUANTITIES
              on every Nifty option contract, Nifty futures, and all 14
              heavyweight stocks.

Both maintain per-contract baselines (EMA) and emit ranked, deduplicated
events into rolling feeds the dashboard renders. Detection is RELATIVE —
a change is significant versus the contract's own normal, not an absolute
constant, so a quiet midcap option and the ATM strike both get fair hearing.
"""

import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from . import clk, config


def _ts() -> str:
    from datetime import datetime
    return datetime.now(config.IST).strftime("%H:%M:%S")


class OIRadar:
    """Significant OI builds/unwinds + volume spikes, any instrument."""

    def __init__(self):
        # key -> (last_oi, baseline_ema, last_event_ts)
        self._oi: Dict[str, list] = {}
        self._vol: Dict[str, list] = {}      # key -> (last_vol, ema_rate, last_event)
        self.events: Deque[dict] = deque(maxlen=60)

    def _emit(self, kind: str, instrument: str, contract: str, text: str,
              tone: str, magnitude: float):
        self.events.appendleft({
            "ts": _ts(), "mts": clk.mono(), "kind": kind,
            "instrument": instrument, "contract": contract, "text": text,
            "tone": tone, "magnitude": round(magnitude, 1),
        })

    def ingest_oi(self, instrument: str, contract: str, side: str,
                  oi: float, bullish_when_build: bool):
        """side: 'CE'/'PE'/'FUT'. bullish_when_build: interpretation hint
        (put build = support = bullish; call build = cap = bearish)."""
        if oi <= 0:
            return
        key = f"{instrument}|{contract}|{side}"
        now = clk.mono()
        rec = self._oi.get(key)
        if rec is None:
            self._oi[key] = [oi, oi, 0.0]
            return
        last, base, last_ev = rec
        rec[0] = oi
        rec[1] = base + 0.02 * (oi - base)          # slow baseline
        if base <= 0 or now - last_ev < config.RADAR_EVENT_COOLDOWN:
            return
        chg_pct = (oi - base) / base * 100.0
        if abs(chg_pct) >= config.RADAR_OI_PCT and \
                abs(oi - base) >= config.RADAR_OI_MIN_ABS:
            build = chg_pct > 0
            bullish = (build == bullish_when_build)
            tone = "bullish" if bullish else "bearish"
            verb = "BUILD" if build else "UNWIND"
            self._emit("oi", instrument, f"{contract} {side}",
                       f"{verb} {chg_pct:+.0f}% vs its norm "
                       f"(OI {oi / 1e5:.1f}L) — "
                       f"{'support forming' if (build and side == 'PE') else 'resistance forming' if (build and side == 'CE') else 'writers leaving' if not build else 'positioning'}",
                       tone, abs(chg_pct))
            rec[2] = now

    def ingest_volume(self, instrument: str, contract: str, vol: float):
        """Cumulative session volume — detect rate spikes vs own norm."""
        if vol <= 0:
            return
        key = f"{instrument}|{contract}"
        now = clk.mono()
        rec = self._vol.get(key)
        if rec is None:
            self._vol[key] = [vol, 0.0, 0.0, now]
            return
        last, rate_ema, last_ev, last_t = rec
        dt = max(now - rec[3], 1e-3)
        rate = max(vol - last, 0.0) / dt
        rec[0] = vol
        rec[3] = now
        if rate_ema > 0 and rate >= rate_ema * config.RADAR_VOL_MULT \
                and now - last_ev > config.RADAR_EVENT_COOLDOWN \
                and rate > 100:
            self._emit("vol", instrument, contract,
                       f"VOLUME SPIKE {rate / max(rate_ema, 1):.1f}× its norm "
                       f"— heavy participation",
                       "warn", rate / max(rate_ema, 1))
            rec[2] = now
        rec[1] = rate_ema + 0.05 * (rate - rate_ema)

    def feed(self) -> list:
        for _ in range(3):
            try:
                return list(self.events)
            except RuntimeError:
                continue
        return []


class BookRadar:
    """Significant buyer/seller pressure shifts in any contract's book."""

    def __init__(self):
        # key -> (ratio_ema, last_event_ts)
        self._book: Dict[str, list] = {}
        self.events: Deque[dict] = deque(maxlen=60)

    def ingest(self, instrument: str, contract: str,
               bid_qty: float, ask_qty: float, bullish_when_buyers: bool = True):
        if bid_qty <= 0 or ask_qty <= 0:
            return
        key = f"{instrument}|{contract}"
        now = clk.mono()
        ratio = bid_qty / ask_qty
        rec = self._book.get(key)
        if rec is None:
            self._book[key] = [ratio, 0.0]
            return
        ema, last_ev = rec
        rec[0] = ema + 0.05 * (ratio - ema)
        if now - last_ev < config.RADAR_EVENT_COOLDOWN or ema <= 0:
            return
        shift = ratio / ema
        if shift >= config.RADAR_BOOK_SHIFT or shift <= 1.0 / config.RADAR_BOOK_SHIFT:
            buyers = shift > 1.0
            bullish = (buyers == bullish_when_buyers)
            who = "BUYERS flooding in" if buyers else "SELLERS flooding in"
            self.events.appendleft({
                "ts": _ts(), "mts": now, "kind": "book", "instrument": instrument,
                "contract": contract,
                "text": f"{who} — bid/ask size {ratio:.1f}× "
                        f"({shift:.1f}× vs its norm)",
                "tone": "bullish" if bullish else "bearish",
                "magnitude": round(max(shift, 1 / shift), 1),
            })
            rec[1] = now

    def feed(self) -> list:
        for _ in range(3):
            try:
                return list(self.events)
            except RuntimeError:
                continue
        return []
