"""
MYTHOS — FallRiskMonitor: a forward-looking FALL / RIP early-warning.

The gap it fills (2026-06-15): every leading signal the engine computes — sister
breadth, CVD divergence, futures-OI quadrant, structure loss, VIX/PCR drift — was
siloed into a coincident, 180s-throttled, single-axis commentary fire or an entry-
only vote, so a −51.6pt roll-over built over 30-40 min with ZERO advance warning.
This FUSES them into a 0-100 FALL RISK and RIP RISK that builds WHILE the move
forms, with a RISING flag, a confidence, and a plain-language tell that labels each
driver LEADING / COINCIDENT / LAGGING so a manual buyer trusts it correctly.

THREAD / SAFETY CONTRACT (like flow.py): touched only by the analytics thread via
update() once per pass. snapshot() (called from the WS-push thread) reads only the
RESULT SCALARS — it never iterates the internal histories — so it is tear-proof and
cannot re-trigger the 2026-06-15 cross-thread freeze. READ-ONLY: this module is
imported by app.py (to drive update) + state.build_state (to render); it is NOT
imported by signals.py/trader.py and changes NO trade decision (v1). A behaviour-
changing FALL_RISK_VETO is a separate, flag-gated, replay-A/B'd experiment.
"""
import logging
from collections import deque

from . import clk, config

log = logging.getLogger("mythos.risk")


def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


class FallRiskMonitor:
    def __init__(self, oi, flow, vol, basket, prices, signals):
        self.oi, self.flow, self.vol = oi, flow, vol
        self.basket, self.prices, self.signals = basket, prices, signals
        self._hist = deque(maxlen=400)     # (ts, spot, bank, fin, basket_sent)
        self._fall_hist = deque(maxlen=120)  # (ts, fall) for the rising-slope read
        # result scalars (read by snapshot() cross-thread — atomic reads only)
        self.fall = 0.0
        self.rip = 0.0
        self.confidence = 0.0
        self.rising = False
        self.regime_tag = ""
        self.drivers = []                  # [(name, lead_class, value)]
        self.tell = "warming up — building the market read"

    # ── analytics thread, once per pass ────────────────────────────────────────
    def update(self, spot: float, now: float):
        try:
            self._update(spot, now)
        except Exception as e:                 # never abort the pass over the HUD
            log.debug("fall-risk update failed (ancillary): %s", e)

    def _update(self, spot, now):
        if spot <= 0:
            return
        bank = self.prices.idx_ltp.get("BANKNIFTY", 0.0)
        fin = self.prices.idx_ltp.get("FINNIFTY", 0.0)
        sent = float(getattr(self.basket, "sentiment", 50.0))
        self._hist.append((now, spot, bank, fin, sent))
        win = config.FALLRISK_WIN_SEC
        past = self._at_or_before(now - win)

        # short-horizon % moves over the window (None until enough history)
        def chg(cur, idx):
            if past is None:
                return None
            p = past[idx]
            return ((cur - p) / p * 100.0) if p and p > 0 else None

        nifty_chg = chg(spot, 1)
        bank_chg = chg(bank, 2)
        fin_chg = chg(fin, 3)
        sent_chg = (sent - past[4]) if past is not None else None
        nifty_holding = (nifty_chg is None) or (nifty_chg >= -0.05)   # not yet down
        nifty_weak = (nifty_chg is None) or (nifty_chg <= 0.05)

        # ── components, each returns (fall_c, rip_c) in 0..1 ──
        cb_f, cb_r = self._breadth(bank_chg, fin_chg, sent_chg, nifty_holding, nifty_weak)
        cc_f, cc_r = self._cvd()
        cq_f, cq_r = self._quadrant()
        cs_f, cs_r = self._structure(spot)
        cv_f, cv_r = self._volpcr()

        W = config.FALLRISK_W
        comps_f = {"breadth": cb_f, "cvd": cc_f, "quadrant": cq_f,
                   "structure": cs_f, "volpcr": cv_f}
        comps_r = {"breadth": cb_r, "cvd": cc_r, "quadrant": cq_r,
                   "structure": cs_r, "volpcr": cv_r}
        fall_raw = 100.0 * sum(W[k] * comps_f[k] for k in W)
        rip_raw = 100.0 * sum(W[k] * comps_r[k] for k in W)

        a = config.FALLRISK_EMA_ALPHA
        self.fall = (1 - a) * self.fall + a * fall_raw
        self.rip = (1 - a) * self.rip + a * rip_raw

        # rising = the dominant side's EMA climbed over the last ~60s
        self._fall_hist.append((now, self.fall - self.rip))
        net_then = self._fall_at(now - 60.0)
        net_now = self.fall - self.rip
        self.rising = (net_then is not None) and (net_now - net_then > 2.0)

        side = comps_f if self.fall >= self.rip else comps_r
        lit = [k for k, v in side.items() if v >= 0.15]
        self.confidence = round(len(lit) / len(W), 2)
        self.drivers = [(k, _LEAD_CLASS[k], round(side[k], 2)) for k in lit]
        self._compose(spot, bank_chg, fin_chg)

    # ── components (fall_c, rip_c) ──────────────────────────────────────────────
    def _breadth(self, bank_chg, fin_chg, sent_chg, nifty_holding, nifty_weak):
        # MOST DEFENSIBLY LEADING — banks ~35% of Nifty; sisters/heavies roll first.
        if bank_chg is None:
            return 0.0, 0.0
        downs = sum(1 for c in (bank_chg, fin_chg) if c is not None and c < -0.03)
        ups = sum(1 for c in (bank_chg, fin_chg) if c is not None and c > 0.03)
        if sent_chg is not None:
            downs += 1 if sent_chg < -1.0 else 0
            ups += 1 if sent_chg > 1.0 else 0
        frac_d = downs / 3.0
        frac_u = ups / 3.0
        # divergence boost: sisters rolling while Nifty still holds is the tell
        f = _clamp(frac_d * (1.20 if nifty_holding and frac_d > 0 else 1.0))
        r = _clamp(frac_u * (1.20 if nifty_weak and frac_u > 0 else 1.0))
        return f, r

    def _cvd(self):
        try:
            s30 = self.flow.cvd.slope(30)
            s120 = self.flow.cvd.slope(120)
            sig = self.flow.cvd.divergence_sigma()
        except Exception:
            return 0.0, 0.0
        # negative sigma = CVD weak while price holds = distribution (bearish);
        # trip LOW (~0.8σ) to catch the build, scale by magnitude. Accelerating
        # (|s30|>|s120|, same sign) strengthens it.
        accel_dn = s30 < 0 and s30 <= s120
        accel_up = s30 > 0 and s30 >= s120
        f = _clamp(max(-sig - 0.8, 0.0) / 1.6) if sig < 0 else 0.0
        r = _clamp(max(sig - 0.8, 0.0) / 1.6) if sig > 0 else 0.0
        if accel_dn:
            f = _clamp(f + 0.25)
        if accel_up:
            r = _clamp(r + 0.25)
        return f, r

    def _quadrant(self):
        try:
            quad = self.flow.fut_oi.quadrant
            oi_pct = abs(self.flow.fut_oi.oi_pct)
        except Exception:
            return 0.0, 0.0
        mag = _clamp(oi_pct / 0.5)
        f = mag if quad in ("LONG_UNWINDING", "SHORT_BUILDUP") else 0.0
        r = mag if quad in ("SHORT_COVERING", "LONG_BUILDUP") else 0.0
        return f, r

    def _structure(self, spot):
        f = r = 0.0
        try:
            st = (self.flow.supertrend.direction or "").lower()  # emits UP/DOWN
            if st == "down":
                f += 0.5
            elif st == "up":
                r += 0.5
            fut = self.prices.futures or spot
            av = self.flow.avwap
            if getattr(av, "from_low", 0) > 0 and fut < av.from_low:
                f += 0.5
            if getattr(av, "from_high", 0) > 0 and fut > av.from_high:
                r += 0.5
        except Exception:
            pass
        return _clamp(f), _clamp(r)

    def _volpcr(self):
        f = r = 0.0
        try:
            d_pcr = self.oi.near_pcr_change(180)
            if d_pcr <= -0.05:           # puts being written-down / calls richening = bearish drift
                f += 0.5
            elif d_pcr >= 0.05:
                r += 0.5
            if getattr(self.vol, "iv_expanding", lambda: False)():
                f += 0.3                 # rising IV usually accompanies falls (fear bid)
        except Exception:
            pass
        return _clamp(f), _clamp(r)

    # ── the plain-language tell ─────────────────────────────────────────────────
    def _compose(self, spot, bank_chg, fin_chg):
        loud = config.FALLRISK_LOUD
        fall, rip = self.fall, self.rip
        if fall >= loud and self.confidence >= 0.4 and fall >= rip:
            self.regime_tag = "ROLLING_OVER"
            kind, score = "DISTRIBUTION FORMING", fall
        elif rip >= loud and self.confidence >= 0.4 and rip > fall:
            self.regime_tag = "RIPPING"
            kind, score = "ACCUMULATION", rip
        else:
            self.regime_tag = ""
            self.tell = (f"tape balanced (fall {fall:.0f} / rip {rip:.0f})"
                         if max(fall, rip) > config.FALLRISK_QUIET else
                         f"calm (fall {fall:.0f} / rip {rip:.0f})")
            return
        lead = [n for n, c, _ in self.drivers if c == "LEADING"]
        coin = [n for n, c, _ in self.drivers if c == "COINCIDENT"]
        mix = ("mostly leading signals" if lead and not coin else
               "leading + coincident" if lead else "mostly coincident")
        bits = []
        if bank_chg is not None:
            bits.append(f"BankNifty {bank_chg:+.2f}%")
        if fin_chg is not None:
            bits.append(f"FinNifty {fin_chg:+.2f}%")
        drv = ", ".join(self.drivers and [d[0] for d in self.drivers] or [])
        arrow = ", RISING" if self.rising else ""
        expiry = " · expiry: theta brutal in a flat tape" if config.is_expiry_day() else ""
        self.tell = (f"{kind} ({'fall' if kind[0]=='D' else 'rip'} risk "
                     f"{score:.0f}{arrow}) — {', '.join(bits)}; drivers: {drv}; "
                     f"{mix}{expiry}.")

    # ── render (push thread): result scalars only, never iterates a live deque ──
    def snapshot(self) -> dict:
        return {
            "fall": round(self.fall, 0),
            "rip": round(self.rip, 0),
            "confidence": self.confidence,
            "rising": bool(self.rising),
            "regime_tag": self.regime_tag,
            "drivers": [{"name": n, "class": c, "v": v} for n, c, v in self.drivers],
            "tell": self.tell,
        }

    def loud_kind(self):
        """Returns 'distribution'/'accumulation' if the tell should fire on the
        marquee, else None. Used by app to drive a persistent commentary line."""
        if self.regime_tag == "ROLLING_OVER":
            return "distribution"
        if self.regime_tag == "RIPPING":
            return "accumulation"
        return None

    # ── helpers ────────────────────────────────────────────────────────────────
    def _at_or_before(self, ts):
        best = None
        for row in self._hist:
            if row[0] <= ts:
                best = row
            else:
                break
        return best

    def _fall_at(self, ts):
        best = None
        for t, v in self._fall_hist:
            if t <= ts:
                best = v
            else:
                break
        return best


_LEAD_CLASS = {"breadth": "LEADING", "cvd": "LEADING", "quadrant": "COINCIDENT",
               "structure": "COINCIDENT", "volpcr": "LAGGING"}
