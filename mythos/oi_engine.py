"""
MYTHOS — Open Interest engine (Requirement §4 — "the core differentiator").

Per-strike OI tracking with change-rate EMAs, OI walls, PCR-based
support/resistance zones, max pain, and OI-vs-price divergence.

Interpretation model (as specified by the user's requirement):
    Put OI is written into demand from bulls hedging → a strike with heavy and
    *building* put OI is being defended → SUPPORT.
    Call OI is written by bears / covered-call sellers → heavy building call
    OI caps price → RESISTANCE.
    PCR per strike > 1 + accelerating put OI  → support zone.
    PCR per strike < 0.7 + accelerating call OI → resistance zone.

Everything updates incrementally; a full recompute over ±10 strikes costs
microseconds, so it runs on every analytics pass.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import clk, config


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SRZone:
    kind:     str      # 'SUPPORT' | 'RESISTANCE'
    level:    float    # strike (centre of cluster)
    strength: float    # 0..1 normalized OI weight
    oi:       float    # absolute OI behind the zone
    building: bool     # OI still increasing (positive d(OI)/dt)


class _OITrack:
    """Per-(strike, right) OI with multi-window rate-of-change EMAs."""
    __slots__ = ("oi", "ts", "emas", "_last")

    def __init__(self):
        self.oi: float = 0.0
        self.ts: float = 0.0
        # d(OI)/dt in contracts/sec smoothed over 1/3/5 minutes
        self.emas: Dict[float, float] = {w: 0.0 for w in config.OI_EMA_WINDOWS}
        self._last: Tuple[float, float] = (0.0, 0.0)   # (ts, oi)

    def update(self, oi: float, now: float):
        if oi <= 0:
            return
        lt, lo = self._last
        if lt > 0 and now > lt and lo > 0:
            rate = (oi - lo) / (now - lt)
            for w in self.emas:
                alpha = min(1.0, (now - lt) / w)
                self.emas[w] += alpha * (rate - self.emas[w])
        self._last = (now, oi)
        self.oi = oi
        self.ts = now


class OIEngine:
    def __init__(self):
        self._tracks: Dict[Tuple[float, str], _OITrack] = {}
        self.pcr: float = 1.0
        self.pcr_history: deque = deque(maxlen=600)       # (epoch, pcr) ~1/sec
        # near-ATM PCR (±6 strikes) — the user's "most important aspect":
        # writers battling at the money move THIS number first
        self.near_pcr: float = 1.0
        self.near_pcr_hist: deque = deque(maxlen=900)     # (epoch, near_pcr)
        self.max_pain: float = 0.0
        self._max_pain_history: deque = deque(maxlen=120)  # (epoch, mp)
        self.support_zones: List[SRZone] = []
        self.resistance_zones: List[SRZone] = []
        self._vol_baseline: Dict[Tuple[float, str], float] = {}  # avg volume
        self._price_hist: deque = deque(maxlen=300)        # (epoch, spot)
        # per-strike OI history for the multi-timeframe FLOW panel: absolute
        # strike -> deque of (ts, ce_oi, pe_oi). ~16 min at 1/s. Powers the
        # 3/5/10/15-min OI & PCR change-per-strike view (walls firming/cracking).
        self._strike_hist: Dict[float, deque] = {}

    # ── ingestion ────────────────────────────────────────────────────────────
    def update_strike(self, strike: float, right: str, oi: float,
                      now: Optional[float] = None):
        now = now or clk.now()
        key = (strike, right)
        t = self._tracks.get(key)
        if t is None:
            t = _OITrack()
            self._tracks[key] = t
        t.update(oi, now)

    def update_volume_baseline(self, strike: float, right: str, vol: float):
        """EMA of cumulative volume per strike, for surge detection."""
        if vol <= 0:
            return
        key = (strike, right)
        prev = self._vol_baseline.get(key, vol)
        self._vol_baseline[key] = prev + 0.05 * (vol - prev)

    def note_spot(self, spot: float, now: Optional[float] = None):
        if spot > 0:
            self._price_hist.append((now or clk.now(), spot))

    # ── recompute (analytics pass) ──────────────────────────────────────────
    def recompute(self, atm: float, spot: float):
        if atm <= 0:
            return
        step = config.STRIKE_STEP
        now = clk.now()

        # aggregate PCR — snapshot first: the Nifty-chain POLLER thread can
        # add strikes via update_strike while this iterates (teardown finding)
        for _ in range(3):
            try:
                tracks = dict(self._tracks)
                break
            except RuntimeError:
                continue
        else:
            return
        tot_ce = sum(t.oi for (k, r), t in tracks.items() if r == "call")
        tot_pe = sum(t.oi for (k, r), t in tracks.items() if r == "put")
        if tot_ce > 0:
            self.pcr = tot_pe / tot_ce
            self.pcr_history.append((now, self.pcr))

        # near-ATM PCR (±6 strikes) — read from the SNAPSHOT, never the live
        # dict (the chain poller inserts keys concurrently → RuntimeError that
        # the broad analytics except swallows, silently killing the pass).
        # Stash the snapshot so _strike_pcr/_find_zones use it too.
        self._snap = tracks
        n_ce = n_pe = 0.0
        for off in range(-6, 7):
            k = atm + off * step
            tc = tracks.get((k, "call"))
            tp = tracks.get((k, "put"))
            if tc:
                n_ce += tc.oi
            if tp:
                n_pe += tp.oi
        if n_ce > 0:
            self.near_pcr = n_pe / n_ce
            self.near_pcr_hist.append((now, self.near_pcr))

        self._find_zones(atm, spot, step, now, tracks)
        self._compute_max_pain(atm, step, now, tracks)

        # snapshot per-strike OI (ATM ± 12) for the multi-timeframe flow panel
        for off in range(-12, 13):
            k = atm + off * step
            tc = tracks.get((k, "call"))
            tp = tracks.get((k, "put"))
            if tc or tp:
                h = self._strike_hist.get(k)
                if h is None:
                    h = deque(maxlen=1000)      # ~16 min @ 1/s
                    self._strike_hist[k] = h
                h.append((now, tc.oi if tc else 0.0, tp.oi if tp else 0.0))

    def multiframe(self, atm: float, spot: float,
                   windows=(180.0, 300.0, 600.0, 900.0), n: int = 8) -> dict:
        """Per-strike OI & PCR change over 3/5/10/15-min windows + a verdict on
        whether each strike is FIRMING or CRACKING as support/resistance — the
        user's panel: see how every wall strengthens or weakens over time."""
        now = clk.now()
        step = config.STRIKE_STEP
        labels = [int(w // 60) for w in windows]
        rows = []
        for off in range(n, -n - 1, -1):          # high strike -> low (display order)
            k = atm + off * step
            hq = self._strike_hist.get(k)
            if not hq:
                continue
            # snapshot the deque: recompute() (analytics thread) appends to it
            # while this push-thread read iterates → "deque mutated during
            # iteration" RuntimeError. Copy once, retry on the size race, then
            # work off the copy (a skipped row for one frame is harmless).
            for _ in range(3):
                try:
                    h = list(hq)
                    break
                except RuntimeError:
                    continue
            else:
                continue
            if not h:
                continue
            _, ce_now, pe_now = h[-1]
            if ce_now <= 0 and pe_now <= 0:
                continue
            frames = {}
            for w, lab in zip(windows, labels):
                base = None
                for ts, ce0, pe0 in h:            # oldest sample inside the window
                    if now - ts <= w:
                        base = (ce0, pe0)
                        break
                if base is None:
                    frames[lab] = None
                    continue
                ce0, pe0 = base
                pcr0 = (pe0 / ce0) if ce0 > 0 else 0.0
                pcr_now = (pe_now / ce_now) if ce_now > 0 else 0.0
                frames[lab] = {"ce_d": round(ce_now - ce0),
                               "pe_d": round(pe_now - pe0),
                               "pcr_d": round(pcr_now - pcr0, 2)}
            pcr = round(pe_now / ce_now, 2) if ce_now > 0 else 0.0
            is_support = k <= spot
            ref = frames.get(5) or frames.get(3) or frames.get(10) or frames.get(15)
            verdict, score = "—", 0.0
            if ref:
                ce_d, pe_d = ref["ce_d"], ref["pe_d"]
                thr_p = max(20000.0, 0.02 * pe_now)
                thr_c = max(20000.0, 0.02 * ce_now)
                if is_support:
                    if pe_d >= thr_p:
                        verdict, score = "SUPPORT FIRMING", min(1.0, pe_d / (4 * thr_p))
                    elif pe_d <= -thr_p:
                        verdict, score = "SUPPORT CRACKING", -min(1.0, -pe_d / (4 * thr_p))
                    else:
                        verdict = "support steady"
                else:
                    if ce_d >= thr_c:
                        verdict, score = "RESISTANCE FIRMING", -min(1.0, ce_d / (4 * thr_c))
                    elif ce_d <= -thr_c:
                        verdict, score = "RESISTANCE WEAKENING", min(1.0, -ce_d / (4 * thr_c))
                    else:
                        verdict = "resistance steady"
            rows.append({"strike": k, "atm": k == atm,
                         "side": "S" if is_support else "R",
                         "ce_oi": round(ce_now), "pe_oi": round(pe_now), "pcr": pcr,
                         "frames": frames, "verdict": verdict, "score": round(score, 2)})
        return {"labels": labels, "atm": atm, "rows": rows,
                "near_pcr": round(self.near_pcr, 2),
                "near_pcr_d5": round(self.near_pcr_change(300), 3),
                "max_pain": self.max_pain}

    def _strike_pcr(self, k: float, tracks: dict = None) -> float:
        t = tracks if tracks is not None else self._tracks
        ce = t.get((k, "call"))
        pe = t.get((k, "put"))
        c = ce.oi if ce else 0.0
        p = pe.oi if pe else 0.0
        return (p / c) if c > 0 else (2.0 if p > 0 else 1.0)

    def _find_zones(self, atm: float, spot: float, step: int, now: float,
                    tracks: dict = None):
        """OI walls + PCR clusters → support/resistance zones.
        Reads ONLY the passed snapshot — never self._tracks (poller race)."""
        if tracks is None:
            tracks = self._tracks
        n = 10
        strikes = [atm + i * step for i in range(-n, n + 1)]

        put_oi = {k: tracks[(k, "put")].oi
                  for k in strikes if (k, "put") in tracks}
        call_oi = {k: tracks[(k, "call")].oi
                   for k in strikes if (k, "call") in tracks}

        max_put = max(put_oi.values()) if put_oi else 0.0
        max_call = max(call_oi.values()) if call_oi else 0.0

        def neighbour_avg(d: dict, k: float) -> float:
            vals = [d[x] for x in (k - step, k + step, k - 2 * step, k + 2 * step)
                    if x in d and d[x] > 0]
            return sum(vals) / len(vals) if vals else 0.0

        supports, resists = [], []
        for k in strikes:
            # SUPPORT candidates: at/below spot, put-heavy
            poi = put_oi.get(k, 0.0)
            if poi > 0 and k <= spot:
                navg = neighbour_avg(put_oi, k)
                is_wall = navg > 0 and poi >= config.OI_WALL_MULT * navg
                pcr_k = self._strike_pcr(k, tracks)
                tr = tracks.get((k, "put"))
                building = bool(tr and tr.emas[config.OI_EMA_WINDOWS[1]] > 0)
                if is_wall or (pcr_k >= config.SUPPORT_PCR_MIN and building):
                    strength = poi / max_put if max_put > 0 else 0.0
                    supports.append(SRZone("SUPPORT", k, round(strength, 3),
                                           poi, building))
            # RESISTANCE candidates: at/above spot, call-heavy
            coi = call_oi.get(k, 0.0)
            if coi > 0 and k >= spot:
                navg = neighbour_avg(call_oi, k)
                is_wall = navg > 0 and coi >= config.OI_WALL_MULT * navg
                pcr_k = self._strike_pcr(k, tracks)
                tr = tracks.get((k, "call"))
                building = bool(tr and tr.emas[config.OI_EMA_WINDOWS[1]] > 0)
                if is_wall or (pcr_k <= config.RESIST_PCR_MAX and building):
                    strength = coi / max_call if max_call > 0 else 0.0
                    resists.append(SRZone("RESISTANCE", k, round(strength, 3),
                                          coi, building))

        # strongest first, cluster-merge adjacent strikes into one zone
        self.support_zones = self._merge(sorted(
            supports, key=lambda z: -z.strength))
        self.resistance_zones = self._merge(sorted(
            resists, key=lambda z: -z.strength))

    @staticmethod
    def _merge(zones: List[SRZone]) -> List[SRZone]:
        merged: List[SRZone] = []
        width = config.SR_ZONE_STRIKES * config.STRIKE_STEP
        for z in zones:
            hit = next((m for m in merged if abs(m.level - z.level) <= width), None)
            if hit:
                # weighted-centre merge
                tot = hit.oi + z.oi
                hit.level = (hit.level * hit.oi + z.level * z.oi) / tot if tot else hit.level
                hit.level = round(hit.level / config.STRIKE_STEP) * config.STRIKE_STEP
                hit.oi = tot
                hit.strength = max(hit.strength, z.strength)
                hit.building = hit.building or z.building
            else:
                merged.append(z)
        return merged[:5]

    def _compute_max_pain(self, atm: float, step: int, now: float,
                          tracks: dict = None):
        """Strike where total option-buyer payout is minimized."""
        if tracks is None:
            tracks = self._tracks
        n = 10
        strikes = [atm + i * step for i in range(-n, n + 1)]
        best_k, best_pain = 0.0, float("inf")
        for expiry_at in strikes:
            pain = 0.0
            for k in strikes:
                ce = tracks.get((k, "call"))
                pe = tracks.get((k, "put"))
                if ce and ce.oi > 0:
                    pain += max(expiry_at - k, 0.0) * ce.oi
                if pe and pe.oi > 0:
                    pain += max(k - expiry_at, 0.0) * pe.oi
            if pain < best_pain:
                best_pain, best_k = pain, expiry_at
        if best_k > 0:
            if self.max_pain > 0 and best_k != self.max_pain:
                self._max_pain_history.append((now, self.max_pain))
            self.max_pain = best_k

    # ── signal queries ───────────────────────────────────────────────────────
    def nearest_support(self, spot: float) -> Optional[SRZone]:
        below = [z for z in self.support_zones if z.level <= spot]
        return max(below, key=lambda z: z.level) if below else None

    def nearest_resistance(self, spot: float) -> Optional[SRZone]:
        above = [z for z in self.resistance_zones if z.level >= spot]
        return min(above, key=lambda z: z.level) if above else None

    def pcr_flip(self, up: bool = True) -> bool:
        """PCR crossed 1.2 upward (bullish) / 0.8 downward (bearish) within
        the last 5 minutes after being on the other side."""
        if len(self.pcr_history) < 30:
            return False
        now = self.pcr_history[-1][0]
        window = [(t, v) for t, v in self.pcr_history if now - t <= 300]
        if len(window) < 10:
            return False
        cur = window[-1][1]
        if up:
            return cur >= 1.2 and any(v < 1.1 for _, v in window[:-5])
        return cur <= 0.8 and any(v > 0.9 for _, v in window[:-5])

    def near_pcr_change(self, seconds: float = 300.0) -> float:
        """Change in near-ATM PCR over the window. Positive = puts being
        written near the money (bulls defending) — bullish; negative mirror."""
        if len(self.near_pcr_hist) < 2:
            return 0.0
        now = self.near_pcr_hist[-1][0]
        past = next((v for t, v in self.near_pcr_hist if now - t <= seconds),
                    None)
        return self.near_pcr_hist[-1][1] - past if past is not None else 0.0

    def pcr_spike_5min(self) -> float:
        """Absolute PCR change over 5 minutes (commentary trigger)."""
        if len(self.pcr_history) < 2:
            return 0.0
        now = self.pcr_history[-1][0]
        past = next((v for t, v in self.pcr_history if now - t <= 300), None)
        return self.pcr_history[-1][1] - past if past is not None else 0.0

    def oi_divergence(self, direction: str, spot: float) -> bool:
        """Bullish divergence: price rising while near-money CALL OI falls
        (bears covering). Bearish: price falling while PUT OI falls.

        Called from the ENTRY path (signals.evaluate). The Nifty-chain POLLER
        thread inserts keys into self._tracks concurrently, so — exactly like
        recompute() — read a one-shot SNAPSHOT (retry on the size-changed
        RuntimeError) and tolerate a transiently-missing EMA window, instead of
        reading the live dict directly. The five strikes are then summed from a
        single consistent view, and a future iterating change can never crash a
        trade evaluation mid-pass."""
        if len(self._price_hist) < 60:
            return False
        now = self._price_hist[-1][0]
        past_spot = next((p for t, p in self._price_hist if now - t <= 180), None)
        if past_spot is None:
            return False
        price_up = spot > past_spot * 1.0005
        price_dn = spot < past_spot * 0.9995
        atm = round(spot / config.STRIKE_STEP) * config.STRIKE_STEP
        mid_w = config.OI_EMA_WINDOWS[1]

        # Snapshot the EMA VALUES (not just the dict keys): dict(self._tracks)
        # copies the mapping but the _OITrack objects are SHARED, so the poller
        # thread can mutate tr.emas[mid_w] mid-sum and the five strikes would be
        # read from different instants. Freezing the scalar rate per key under the
        # same retry guard gives a genuinely consistent view.
        for _ in range(3):
            try:
                rates = {k: t.emas.get(mid_w, 0.0)
                         for k, t in self._tracks.items()}
                break
            except RuntimeError:
                continue
        else:
            return False

        def side_rate(right: str) -> float:
            return sum(rates.get((atm + off * config.STRIKE_STEP, right), 0.0)
                       for off in range(-2, 3))

        if direction == "CE":
            return price_up and side_rate("call") < 0
        return price_dn and side_rate("put") < 0

    def max_pain_shift(self) -> float:
        """Largest max-pain displacement in the last 10 minutes."""
        if not self._max_pain_history or self.max_pain <= 0:
            return 0.0
        now = clk.now()
        old = [mp for t, mp in self._max_pain_history if now - t <= 600]
        return max((abs(self.max_pain - mp) for mp in old), default=0.0)

    def volume_surge(self, strike: float, right: str, vol: float) -> float:
        """Current cumulative volume vs EMA baseline (× multiple)."""
        base = self._vol_baseline.get((strike, right), 0.0)
        return vol / base if base > 0 else 0.0

    def ladder(self, atm: float, n: int = 8) -> List[dict]:
        """Per-strike rows for the S/R thermometer + PCR heat strip UI.
        Reads a snapshot (push-thread call; poller inserts keys concurrently)."""
        for _ in range(3):
            try:
                tracks = dict(self._tracks)
                break
            except RuntimeError:
                continue
        else:
            return []
        out = []
        for off in range(-n, n + 1):
            k = atm + off * config.STRIKE_STEP
            ce = tracks.get((k, "call"))
            pe = tracks.get((k, "put"))
            mid_w = config.OI_EMA_WINDOWS[1]
            out.append({
                "strike": k,
                "ce_oi": ce.oi if ce else 0.0,
                "pe_oi": pe.oi if pe else 0.0,
                "ce_oi_rate": round(ce.emas.get(mid_w, 0.0), 2) if ce else 0.0,
                "pe_oi_rate": round(pe.emas.get(mid_w, 0.0), 2) if pe else 0.0,
                "pcr": round(self._strike_pcr(k, tracks), 2),
            })
        return out

    def oi_snapshot(self) -> Dict[Tuple[float, str], float]:
        """{(strike, right): oi} for persistence and delta-flow computation.
        Retry-snapshots like recompute() — this .items() iteration would also
        raise 'dict changed size' when the chain poller inserts a key mid-loop
        (same race class as _find_zones, also fixed)."""
        for _ in range(3):
            try:
                return {k: t.oi for k, t in self._tracks.items() if t.oi > 0}
            except RuntimeError:
                continue
        return {}

    def reset_session(self):
        self._tracks.clear()
        self.pcr_history.clear()
        self.near_pcr_hist.clear()
        self.near_pcr = 1.0
        self._max_pain_history.clear()
        self.support_zones = []
        self.resistance_zones = []
        self._vol_baseline.clear()
        self._price_hist.clear()
        self.pcr = 1.0
        self.max_pain = 0.0
