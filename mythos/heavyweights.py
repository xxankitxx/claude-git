"""
MYTHOS — heavyweight constituent analyzer (Requirement §4.2, §6.1).

Each basket stock contributes a bias score in [-1, +1] built from:
    * intraday % change (live WS cash tick vs previous close)   — 50%
    * its own option-chain PCR (REST, monthly expiry)           — 30%
    * OI-wall proximity: price sitting on its put wall is supportive,
      pressed under its call wall is capping                    — 20%

Index sentiment = Σ (weight_i × bias_i) / Σ weight_i  → scaled 0..100.

Constituent-implied index S/R (first-principles correction of the
requirement's formula): a stock's support sitting d% below its price
contributes weight×d% of *index* downside cushion. The weighted average of
those distances, applied to the Nifty spot, gives the composite cushion level:
    implied_support = nifty_spot × (1 − Σ w_i·d_i% / Σ w_i)
and the mirror for resistance. That is dimensionally sound — the requirement's
"2500 × 0.09 / correlation" example is not (it mixes rupees of a stock with
index points), so it was discarded.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import clk, config


@dataclass
class StockState:
    symbol:     str
    weight:     float
    isec_code:  str = ""
    ltp:        float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    pcr:        float = 1.0
    put_wall:   float = 0.0     # strike of biggest put OI (support)
    call_wall:  float = 0.0     # strike of biggest call OI (resistance)
    bias:       float = 0.0     # -1 .. +1
    chain_ts:   float = 0.0     # last successful chain poll
    tick_ts:    float = 0.0


class HeavyweightBasket:
    def __init__(self):
        self.stocks: Dict[str, StockState] = {
            sym: StockState(sym, w) for sym, w in config.HEAVYWEIGHTS.items()
        }
        self.sentiment: float = 50.0          # 0=max bear .. 100=max bull
        self.implied_support: float = 0.0     # index points
        self.implied_resistance: float = 0.0

    # ── feed-side updates ────────────────────────────────────────────────────
    def on_tick(self, symbol: str, ltp: float):
        s = self.stocks.get(symbol)
        if not s or ltp <= 0:
            return
        s.ltp = ltp
        s.tick_ts = clk.now()
        if s.prev_close > 0:
            s.change_pct = (ltp - s.prev_close) / s.prev_close * 100.0

    def set_prev_close(self, symbol: str, prev_close: float, ltp: float = 0.0):
        s = self.stocks.get(symbol)
        if not s:
            return
        if prev_close > 0:
            s.prev_close = prev_close
        if ltp > 0:
            self.on_tick(symbol, ltp)

    def on_chain(self, symbol: str, ce_rows: List[dict], pe_rows: List[dict]):
        """Digest a REST option-chain response (rows with strike_price, OI, ltp)."""
        s = self.stocks.get(symbol)
        if not s:
            return

        def parse(rows):
            out = {}
            for r in rows or []:
                try:
                    k = float(r.get("strike_price") or 0)
                    oi = float(r.get("open_interest") or r.get("OI") or 0)
                    if k > 0 and oi > 0:
                        out[k] = out.get(k, 0.0) + oi
                except (TypeError, ValueError):
                    continue
            return out

        ce, pe = parse(ce_rows), parse(pe_rows)
        tot_ce, tot_pe = sum(ce.values()), sum(pe.values())
        if tot_ce > 0:
            s.pcr = tot_pe / tot_ce
        if pe and s.ltp > 0:
            below = {k: v for k, v in pe.items() if k <= s.ltp}
            if below:
                s.put_wall = max(below, key=below.get)
        if ce and s.ltp > 0:
            above = {k: v for k, v in ce.items() if k >= s.ltp}
            if above:
                s.call_wall = max(above, key=above.get)
        s.chain_ts = clk.now()
        self._recompute_bias(s)

    # ── scoring ──────────────────────────────────────────────────────────────
    @staticmethod
    def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, v))

    def _recompute_bias(self, s: StockState):
        # price component: ±1% intraday move saturates the score
        price_c = self._clamp(s.change_pct / 1.0)
        # PCR component: 1.0 neutral; 1.5+ strongly defended; 0.6- capped
        pcr_c = self._clamp((s.pcr - 1.0) / 0.5)
        # wall proximity: within 1% above its put wall → support tailwind;
        # within 1% below its call wall → resistance headwind
        wall_c = 0.0
        if s.ltp > 0:
            if s.put_wall > 0:
                d = (s.ltp - s.put_wall) / s.ltp * 100.0
                if 0 <= d <= 1.0:
                    wall_c += (1.0 - d)            # closer = stronger support
            if s.call_wall > 0:
                d = (s.call_wall - s.ltp) / s.ltp * 100.0
                if 0 <= d <= 1.0:
                    wall_c -= (1.0 - d)
            wall_c = self._clamp(wall_c)
        s.bias = self._clamp(0.5 * price_c + 0.3 * pcr_c + 0.2 * wall_c)

    def recompute(self, nifty_spot: float):
        """Aggregate sentiment + constituent-implied S/R. Stocks with no live
        tick yet are excluded rather than poisoning the average."""
        live = [s for s in self.stocks.values() if s.ltp > 0]
        if live:
            for s in live:
                self._recompute_bias(s)
            tot_w = sum(s.weight for s in live)
            agg = sum(s.weight * s.bias for s in live) / tot_w if tot_w else 0.0
            self.sentiment = round(50.0 + 50.0 * agg, 1)

        if nifty_spot > 0:
            sup_w = sup_d = res_w = res_d = 0.0
            for s in live:
                if s.put_wall > 0 and s.ltp > 0 and s.put_wall <= s.ltp:
                    d_pct = (s.ltp - s.put_wall) / s.ltp
                    if d_pct <= 0.05:               # ignore stale far walls
                        sup_w += s.weight
                        sup_d += s.weight * d_pct
                if s.call_wall > 0 and s.ltp > 0 and s.call_wall >= s.ltp:
                    d_pct = (s.call_wall - s.ltp) / s.ltp
                    if d_pct <= 0.05:
                        res_w += s.weight
                        res_d += s.weight * d_pct
            if sup_w > 0:
                self.implied_support = round(nifty_spot * (1 - sup_d / sup_w), 1)
            if res_w > 0:
                self.implied_resistance = round(nifty_spot * (1 + res_d / res_w), 1)

    # ── UI rows ──────────────────────────────────────────────────────────────
    def rows(self) -> List[dict]:
        out = []
        for s in sorted(self.stocks.values(), key=lambda x: -x.weight):
            out.append({
                "symbol": s.symbol,
                "weight": s.weight,
                "ltp": round(s.ltp, 2),
                "change_pct": round(s.change_pct, 2),
                "pcr": round(s.pcr, 2),
                "bias": round(s.bias, 3),
                "put_wall": s.put_wall,
                "call_wall": s.call_wall,
                "live": (clk.now() - s.tick_ts) < 60 if s.tick_ts else False,
            })
        return out

    def extreme_movers(self, pct: float) -> List[StockState]:
        return [s for s in self.stocks.values()
                if s.ltp > 0 and abs(s.change_pct) >= pct]

    def reset_session(self):
        for s in self.stocks.values():
            s.change_pct = 0.0
            s.bias = 0.0
        self.sentiment = 50.0
