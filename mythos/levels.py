"""
MYTHOS — BATTLE LINES (user request, 2026-06-19): strong buying / selling zones
across the WHOLE complex.

For every tracked series it learns, intraday, WHERE price is being FOUGHT:

  • DEFENDED FLOOR  — a price buyers keep stepping in to hold: each time it dips
    there it bounces (and, where book data exists, SERIOUS BUYING appears — best-
    bid quantity swamps the offers). A strong BUYING zone.
  • RESISTED CEILING — a price sellers keep slamming back: each test rejects (with
    SERIOUS SELLING where book data exists). A strong SELLING zone.

TRACKED:
  • Nifty ATM/ITM CE & PE PREMIUMS  (full book: ltp + bid/ask qty)
  • NIFTY spot                       (futures book for flow)
  • BANKNIFTY, FINNIFTY              (price only — level by repeated holds)
  • every heavyweight STOCK          (price + best-bid/ask qty)

HOW: a zigzag pivot detector per series. A down-leg that reverses up by a rebound
threshold prints a PIVOT LOW (a tested floor); an up-leg reversing down prints a
PIVOT HIGH (a tested ceiling). Pivots within a band cluster into one level whose
STRENGTH = number of tests. A level held >= MIN_TESTS is battle-tested. Where book
data exists, each test is tagged whether buyers/sellers were SERIOUS, and a LIVE
flag fires when price is AT a level now with the flow defending it.

This is PRICE MEMORY, not a vote. DISPLAY-ONLY: single-writer on the analytics
thread, snapshot() returns frozen scalars (tear-proof, like memory.py / risk.py).
"""
import logging

from . import clk, config

log = logging.getLogger("mythos.levels")


class _Level:
    __slots__ = ("price", "strength", "serious", "last_ts", "first_ts")

    def __init__(self, price, ts, serious):
        self.price = price
        self.strength = 1
        self.serious = 1 if serious else 0
        self.last_ts = ts
        self.first_ts = ts

    def touch(self, price, ts, serious):
        self.price = (self.price * self.strength + price) / (self.strength + 1)
        self.strength += 1
        if serious:
            self.serious += 1
        self.last_ts = ts


class _Track:
    """Zigzag pivot detector + clustered levels for ONE series. Rebound/band
    thresholds are passed in so an option premium (volatile) and an index price
    (tight) each use scale-appropriate values."""
    __slots__ = ("rf", "bf", "rp", "bp", "has_flow",
                 "leg", "ext", "ext_serious", "floors", "ceils", "last_ltp")

    def __init__(self, rebound_frac, band_frac, rebound_pts, band_pts, has_flow):
        self.rf, self.bf, self.rp, self.bp = rebound_frac, band_frac, rebound_pts, band_pts
        self.has_flow = has_flow
        self.leg = 0
        self.ext = 0.0
        self.ext_serious = False
        self.floors = []
        self.ceils = []
        self.last_ltp = 0.0

    def _band(self, px):
        return max(self.bp, px * self.bf)

    def _rebound(self, px):
        return max(self.rp, px * self.rf)

    def _cluster(self, levels, price, ts, serious):
        band = self._band(price)
        for lv in levels:
            if abs(lv.price - price) <= band:
                lv.touch(price, ts, serious)
                return
        levels.append(_Level(price, ts, serious))
        if len(levels) > 8:
            levels.sort(key=lambda l: (l.strength, l.last_ts))
            del levels[0]

    def update(self, ltp, bqty, aqty, now):
        if ltp <= 0:
            return
        self.last_ltp = ltp
        buy_serious = aqty > 0 and bqty / aqty >= config.BATTLE_FLOW_RATIO
        sell_serious = bqty > 0 and aqty / bqty >= config.BATTLE_FLOW_RATIO
        if self.leg == 0:
            self.leg, self.ext, self.ext_serious = -1, ltp, buy_serious
            return
        if self.leg < 0:
            if ltp <= self.ext:
                self.ext = ltp
                self.ext_serious = buy_serious or self.ext_serious
            elif ltp - self.ext >= self._rebound(self.ext):
                self._cluster(self.floors, self.ext, now, self.ext_serious)
                self.leg, self.ext, self.ext_serious = +1, ltp, sell_serious
        else:
            if ltp >= self.ext:
                self.ext = ltp
                self.ext_serious = sell_serious or self.ext_serious
            elif self.ext - ltp >= self._rebound(self.ext):
                self._cluster(self.ceils, self.ext, now, self.ext_serious)
                self.leg, self.ext, self.ext_serious = -1, ltp, buy_serious

    def _best(self, levels, want_below, px):
        """Strongest battle-tested level on the wanted side of px. Qualifies on
        STRENGTH alone (a repeated hold IS the evidence); flow only adds weight."""
        cand = [l for l in levels
                if l.strength >= config.BATTLE_MIN_TESTS
                and (l.price <= px if want_below else l.price >= px)]
        if not cand:
            return None
        return max(cand, key=lambda l: (l.strength, l.serious, -abs(l.price - px)))


class BattleLines:
    def __init__(self, prices):
        self.prices = prices
        self.opt_tracks = {}        # (strike,right) -> _Track   (option premiums)
        self.inst_tracks = {}       # name -> _Track             (instrument prices)
        self._opt_rows = []
        self._inst_rows = []

    # ── option premium tracks (volatile → larger thresholds) ──────────────────
    def _opt_track(self, key):
        return self.opt_tracks.setdefault(key, _Track(
            config.BATTLE_REBOUND_FRAC, config.BATTLE_BAND_FRAC,
            config.BATTLE_REBOUND_PTS, config.BATTLE_BAND_PTS, True))

    # ── instrument price tracks (tight → smaller % thresholds) ────────────────
    def _inst_track(self, name, has_flow):
        return self.inst_tracks.setdefault(name, _Track(
            config.BATTLE_INST_REBOUND_FRAC, config.BATTLE_INST_BAND_FRAC,
            config.BATTLE_INST_REBOUND_PTS, config.BATTLE_INST_BAND_PTS, has_flow))

    def _significant(self, atm):
        step = config.STRIKE_STEP
        n = config.BATTLE_STRIKES_EACH
        ce = [(atm - i * step, "call") for i in range(0, n + 1)]
        pe = [(atm + i * step, "put") for i in range(0, n + 1)]
        return ce + pe

    def _row(self, tr, ltp, label, sub):
        floor = tr._best(tr.floors, True, ltp)
        ceil = tr._best(tr.ceils, False, ltp)
        band = tr._band(ltp)
        bq = aq = 0.0
        return floor, ceil, band, {
            "label": label, "sub": sub,
            "ltp": round(ltp, 1),
            "floor": round(floor.price, 1) if floor else 0.0,
            "floor_str": floor.strength if floor else 0,
            "floor_serious": (floor.serious if floor else 0) if tr.has_flow else -1,
            "ceil": round(ceil.price, 1) if ceil else 0.0,
            "ceil_str": ceil.strength if ceil else 0,
            "ceil_serious": (ceil.serious if ceil else 0) if tr.has_flow else -1,
            "at_floor": bool(floor and (ltp - floor.price) <= band),
            "at_ceil": bool(ceil and (ceil.price - ltp) <= band),
        }

    def update(self, spot, atm, fut_bqty=0.0, fut_aqty=0.0):
        if atm <= 0:
            return
        now = clk.now()

        # 1. Nifty option premiums (ATM/ITM CE & PE)
        orows = []
        for strike, right in self._significant(atm):
            ltp = self.prices.opt_ltp.get((strike, right), 0.0)
            if ltp <= 0:
                continue
            tr = self._opt_track((strike, right))
            bq = self.prices.opt_bqty.get((strike, right), 0.0)
            aq = self.prices.opt_aqty.get((strike, right), 0.0)
            tr.update(ltp, bq, aq, now)
            money = "ATM" if strike == atm else (
                "ITM" if (right == "call" and strike < spot)
                or (right == "put" and strike > spot) else "OTM")
            floor, ceil, band, row = self._row(
                tr, ltp, f"{strike:.0f} {'CE' if right=='call' else 'PE'}", money)
            row["defending"] = bool(floor and (ltp - floor.price) <= band
                                    and aq > 0 and bq / aq >= config.BATTLE_FLOW_RATIO)
            row["rejecting"] = bool(ceil and (ceil.price - ltp) <= band
                                    and bq > 0 and aq / bq >= config.BATTLE_FLOW_RATIO)
            orows.append(row)
        self._opt_rows = orows

        # 2. instrument PRICES — Nifty / BankNifty / FinNifty / every stock
        irows = []
        # Nifty (futures book = flow)
        tr = self._inst_track("NIFTY", True)
        tr.update(spot, fut_bqty, fut_aqty, now)
        f, c, band, row = self._row(tr, spot, "NIFTY", "index")
        row["defending"] = bool(f and (spot - f.price) <= band
                                and fut_aqty > 0 and fut_bqty / fut_aqty >= config.BATTLE_FLOW_RATIO)
        row["rejecting"] = bool(c and (c.price - spot) <= band
                                and fut_bqty > 0 and fut_aqty / fut_bqty >= config.BATTLE_FLOW_RATIO)
        irows.append(row)
        # sister indices (price only)
        for nm in ("BANKNIFTY", "FINNIFTY"):
            v = self.prices.idx_ltp.get(nm, 0.0)
            if v <= 0:
                continue
            tr = self._inst_track(nm, False)
            tr.update(v, 0.0, 0.0, now)
            f, c, band, row = self._row(tr, v, nm, "index")
            row["defending"] = bool(f and (v - f.price) <= band)
            row["rejecting"] = bool(c and (c.price - v) <= band)
            irows.append(row)
        # heavyweight stocks (price + book)
        for sym, ltp in list(self.prices.hw_ltp.items()):
            if ltp <= 0:
                continue
            tr = self._inst_track(sym, True)
            bq = self.prices.hw_bqty.get(sym, 0.0)
            aq = self.prices.hw_aqty.get(sym, 0.0)
            tr.update(ltp, bq, aq, now)
            f, c, band, row = self._row(tr, ltp, sym, "stock")
            row["defending"] = bool(f and (ltp - f.price) <= band
                                    and aq > 0 and bq / aq >= config.BATTLE_FLOW_RATIO)
            row["rejecting"] = bool(c and (c.price - ltp) <= band
                                    and bq > 0 and aq / bq >= config.BATTLE_FLOW_RATIO)
            irows.append(row)
        self._inst_rows = irows

    def snapshot(self):
        return {"options": list(self._opt_rows), "instruments": list(self._inst_rows)}
