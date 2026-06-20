"""
MYTHOS — commentary engine (Requirement §8): pre-programmed, fired only on
extreme events, each event type rate-limited so the ticker stays signal, not
noise.
"""

import logging
import time
from collections import deque
from typing import List, Optional

from . import clk, config

log = logging.getLogger("mythos.commentary")


class Commentary:
    def __init__(self, oi_engine, vol_engine, flow, basket, prices,
                 on_alert=None):
        self.oi = oi_engine
        self.vol = vol_engine
        self.flow = flow
        self.basket = basket
        self.prices = prices
        self.on_alert = on_alert or (lambda text: None)
        self.items: deque = deque(maxlen=60)       # (epoch, time_str, text, kind)
        self._last_fired: dict = {}                # kind -> epoch
        self._last_hw_alert: dict = {}             # symbol -> epoch
        self._spread_ema: dict = {}                # (strike, right) -> avg spread
        self._last_quadrant: str = "NEUTRAL"
        self._last_gamma: float = 0.0
        self._routine_fires: deque = deque()       # clk epochs of recent ROUTINE chimes
        self._cross_hist: deque = deque(maxlen=120)  # (ts, BankNifty ltp) for intraday momentum
        self._last_cross: str = ""                 # last cross-tape verdict (dedup: fire on flip)

    def note(self, text: str, kind: str = "trade"):
        """Direct entry into the ticker — used for trade rationale. No
        cooldown, no chime (the entry sound already plays)."""
        from datetime import datetime
        ts = datetime.now(config.IST).strftime("%H:%M:%S")
        self.items.appendleft({"ts": ts, "mts": clk.now(), "text": text, "kind": kind})
        log.info("COMMENTARY [%s] %s", kind, text)

    # per-kind cooldown overrides — the blaster tiers are rarer than ordinary
    # commentary BY DESIGN; everything else uses COMMENT_COOLDOWN_SEC (180s).
    _KIND_COOLDOWN = {"blaster_igniting": "BLASTER_IGNITE_COOLDOWN",
                      "blaster_loading": "BLASTER_LOADING_COOLDOWN",
                      # targeted noise cut — the repeat offenders, long cooldowns
                      "book_call": "COMMENT_BOOK_COOLDOWN",
                      "book_put": "COMMENT_BOOK_COOLDOWN",
                      "book_call_sell": "COMMENT_BOOK_COOLDOWN",
                      "book_put_sell": "COMMENT_BOOK_COOLDOWN",
                      "vol_surge_call": "COMMENT_VOLSURGE_COOLDOWN",
                      "vol_surge_put": "COMMENT_VOLSURGE_COOLDOWN",
                      "fut_quad": "COMMENT_QUAD_COOLDOWN",
                      "expiry_warn": "EXPIRY_WARN_COOLDOWN",
                      # low-value regime/vol chatter → 6 min
                      "gex": "COMMENT_LOWPRI_COOLDOWN",
                      "gamma": "COMMENT_LOWPRI_COOLDOWN",
                      "vol_expansion": "COMMENT_LOWPRI_COOLDOWN",
                      "iv_spike": "COMMENT_LOWPRI_COOLDOWN",
                      "liquidity": "COMMENT_LOWPRI_COOLDOWN"}

    # priority tiers (task #37 declutter). CRITICAL kinds bypass the global
    # routine rate-cap entirely and are NEVER suppressed away — these are the
    # exact tells the user says get missed: seller-exhaustion (reversal_fuel),
    # the fall-warning (distribution/accumulation), gamma ignition, cross-index.
    # Everything else is ROUTINE and shares a rolling-window budget.
    _TIER_CRITICAL = {"reversal_fuel", "distribution", "accumulation",
                      "blaster_igniting", "cross_index_oi"}
    # MEDIUM = the user's valued tells — exempt from the low-value routine budget
    # (kept at their own cooldown) so PCR/CVD/max-pain are never crowded out by
    # gamma/book/vol chatter.
    _TIER_MEDIUM = {"pcr_spike", "pcr_drop", "cvd_diverge", "max_pain"}

    def _fire(self, kind: str, text: str):
        # SIGNAL-ONLY (user mandate): show ONLY the most significant directional
        # alerts (the CRITICAL tier — fall/rip early-warning, seller-exhaustion,
        # broad-tape consensus + gate veto, gamma ignition). Everything else is
        # silenced entirely. The WHY-THIS-TRADE rationale is unaffected (it goes
        # via note(), not here).
        if config.COMMENT_SIGNAL_ONLY and kind not in self._TIER_CRITICAL:
            return
        now = clk.now()
        cd = getattr(config, self._KIND_COOLDOWN.get(kind, ""),
                     config.COMMENT_COOLDOWN_SEC)
        if now - self._last_fired.get(kind, 0.0) < cd:
            return
        # GLOBAL ROUTINE BUDGET (task #37): CRITICAL tells bypass; routine chatter
        # is rate-capped per rolling window so book/vol-surge/regime spam can't
        # crowd out the chime that matters. Default off = byte-identical.
        if (config.COMMENT_PRIORITY_ON and kind not in self._TIER_CRITICAL
                and kind not in self._TIER_MEDIUM):
            win = config.COMMENT_ROUTINE_WINDOW_SEC
            while self._routine_fires and now - self._routine_fires[0] > win:
                self._routine_fires.popleft()
            if len(self._routine_fires) >= config.COMMENT_ROUTINE_MAX_PER_WIN:
                return
            self._routine_fires.append(now)
        self._last_fired[kind] = now
        from datetime import datetime
        ts = datetime.now(config.IST).strftime("%H:%M:%S")
        self.items.appendleft({"ts": ts, "mts": now, "text": text, "kind": kind})
        log.info("COMMENTARY [%s] %s", kind, text)
        self.on_alert(text)

    # called once per analytics pass
    def scan(self, spot: float, atm: float, gamma_heat: float = 0.0,
             gex: float = 0.0, gamma_stage: str = "idle", cross_blocked: str = ""):
        if spot <= 0:
            return
        # CROSS-TAPE VETO (task #39): the consensus gate stood a trade down because
        # it fought BankNifty/FinNifty/stocks. CRITICAL tier — the user always sees
        # the cross-instrument bloc acting on a trade.
        if cross_blocked:
            self._fire("cross_index_oi",
                       f"GATE — entry stood down: {cross_blocked}.")

        # EXPIRY-DAY THETA WARNING — on expiry, premium melts on any stall, so a
        # slow / range-bound trade bleeds out fast. Advisory only (the user is the
        # executor): a recurring, prominent reminder to be far more selective and
        # never hold a non-mover into the decay. Long cooldown so it stays present
        # without spamming. Not directional (no BULLISH/BEARISH prefix → Slot C
        # ignores it). The deeper expiry ENTRY discipline is being A/B-designed.
        if config.is_expiry_day():
            self._fire("expiry_warn",
                       "⏳ EXPIRY DAY — theta is brutal: premium melts the moment "
                       "price stalls. Demand a STRONG, FAST move; skip slow / "
                       "range-bound setups and don't hold a non-mover — it bleeds out.")

        # While the two-tier BLASTER owns the gamma narrative (LOADING/IGNITING),
        # suppress the three SLOW regime fires below so the tape doesn't double-
        # speak the same coil/expansion in two voices. They resume once idle.
        blaster_active = gamma_stage in ("loading", "igniting")

        # 0c. dealer gamma regime flip — sets how the whole tape behaves
        prev_gex = getattr(self, "_last_gex", 0.0)
        if not blaster_active:
            if gex < -0.5 and prev_gex >= -0.5:
                self._fire("gex",
                           "TAPE AMPLIFIED — dealer gamma turned NEGATIVE: market "
                           "makers must BUY rises and SELL falls to stay hedged. "
                           "Moves will extend and accelerate — the option buyer's "
                           "regime. Trends deserve extra trust.")
            elif gex > 0.5 and prev_gex <= 0.5:
                self._fire("gex",
                           "TAPE DAMPENED — dealer gamma turned POSITIVE: market "
                           "makers fade every move to stay hedged. Expect chop and "
                           "mean reversion; breakouts will struggle. Demand more "
                           "evidence before trusting any move.")
        self._last_gex = gex      # ALWAYS update — IGNITING's gex-flip vote reads prev_gex

        # 0a. GAMMA regime — the buyer's convexity weapon (and danger)
        if not blaster_active and gamma_heat >= 0.18 and self._last_gamma < 0.18:
            self._fire("gamma",
                       f"GAMMA ZONE — ATM gamma is extreme: a 50-pt Nifty move "
                       f"now adds ~{gamma_heat:.2f} delta. Premiums will move "
                       f"violently BOTH ways: winners explode, losers bleed "
                       f"fast. Respect the stop.")
        self._last_gamma = gamma_heat

        # 0b. VOLATILITY EXPANSION — realized movement outrunning option
        # pricing: the single best regime for an option buyer
        if not blaster_active and self.vol.iv_expanding() \
                and self.vol.variance_premium < -1.0:
            self._fire("vol_expansion",
                       f"VOLATILITY EXPANSION — the market is moving MORE than "
                       f"options are charging for (variance premium "
                       f"{self.vol.variance_premium:+.1f}). This is the option "
                       f"buyer's best regime: bought moves get paid.")

        # 1. PCR spike — massive protective put buying
        spike = self.oi.pcr_spike_5min()
        if spike >= config.COMMENT_PCR_SPIKE:
            sup = self.oi.nearest_support(spot)
            at = f" near {sup.level:.0f}" if sup else ""
            self._fire("pcr_spike",
                       f"BULLISH — Massive put writing underway (PCR jumped "
                       f"+{spike:.2f} in 5 min). Sellers are confident Nifty "
                       f"won't fall; a support floor is forming{at}.")
        elif spike <= -config.COMMENT_PCR_SPIKE:
            res = self.oi.nearest_resistance(spot)
            at = f" near {res.level:.0f}" if res else ""
            self._fire("pcr_drop",
                       f"BEARISH — Heavy call writing underway (PCR fell "
                       f"{spike:.2f} in 5 min). Sellers are confident Nifty "
                       f"won't rise; a ceiling is forming{at}.")

        # 2. CVD / price divergence
        sigma = self.flow.cvd.divergence_sigma()
        if abs(sigma) >= config.COMMENT_CVD_SIGMA:
            if sigma > 0:
                self._fire("cvd_diverge",
                           f"BULLISH — Big players are buying Nifty futures far more "
                           f"aggressively than the price shows ({sigma:.1f}σ anomaly). "
                           f"An upside breakout may be brewing.")
            else:
                self._fire("cvd_diverge",
                           f"BEARISH — Big players are selling Nifty futures far more "
                           f"aggressively than the price shows ({abs(sigma):.1f}σ anomaly). "
                           f"A downside break may be brewing.")

        # 3. IV rank jump
        jump = self.vol.iv_jump_5min()
        if jump >= config.COMMENT_IVR_JUMP:
            self._fire("iv_spike",
                       f"Volatility explosion: IV rank +{jump:.0f} in minutes — "
                       f"premiums rich, expect wider swings.")

        # 4. Max pain displacement
        shift = self.oi.max_pain_shift()
        if shift >= config.MAX_PAIN_SHIFT_ALERT:
            self._fire("max_pain",
                       f"Max Pain magnet jumped {shift:.0f} pts to "
                       f"{self.oi.max_pain:.0f} — expect gravitation.")

        # 5. Single-strike volume surge
        for off in range(-3, 4):
            k = atm + off * config.STRIKE_STEP
            for right, label in (("call", "call"), ("put", "put")):
                vol = self.prices.opt_vol.get((k, right), 0.0)
                surge = self.oi.volume_surge(k, right, vol)
                if surge >= config.COMMENT_VOL_SURGE_MULT:
                    tag = "BULLISH" if right == "call" else "BEARISH"
                    side = "a rise" if right == "call" else "a fall"
                    self._fire(f"vol_surge_{right}",
                               f"{tag} — Unusual {label} buying at {k:.0f} "
                               f"({surge:.1f}× normal volume). Smart money appears "
                               f"to be positioning for {side} in Nifty.")
                    break

        # 6. Order-book imbalance — real bid/ask size pressure on near strikes.
        # Demand for CALLS is bullish; demand for PUTS is bearish; dumping of
        # either is the reverse. Every message states the verdict explicitly.
        for off in (-1, 0, 1):
            k = atm + off * config.STRIKE_STEP
            for right in ("call", "put"):
                b = self.prices.opt_bqty.get((k, right), 0.0)
                a = self.prices.opt_aqty.get((k, right), 0.0)
                if b <= 0 or a <= 0:
                    continue
                name = f"{k:.0f} {'CE' if right == 'call' else 'PE'}"
                if b / a >= config.COMMENT_BOOK_IMBAL:
                    if right == "call":
                        self._fire("book_call",
                                   f"BULLISH — Heavy buying interest in {name}: "
                                   f"buyers are waiting with {b / a:.1f} times more "
                                   f"quantity than sellers are offering. Traders "
                                   f"are positioning for Nifty to RISE.")
                    else:
                        self._fire("book_put",
                                   f"BEARISH — Heavy buying interest in {name}: "
                                   f"buyers are waiting with {b / a:.1f} times more "
                                   f"quantity than sellers are offering. Traders "
                                   f"are positioning for Nifty to FALL.")
                elif a / b >= config.COMMENT_BOOK_IMBAL:
                    if right == "call":
                        self._fire("book_call_sell",
                                   f"BEARISH — {name} is being dumped: sellers are "
                                   f"offering {a / b:.1f} times more quantity than "
                                   f"buyers want. Traders are abandoning their "
                                   f"bets on a rise.")
                    else:
                        self._fire("book_put_sell",
                                   f"BULLISH — {name} is being dumped: sellers are "
                                   f"offering {a / b:.1f} times more quantity than "
                                   f"buyers want. Traders are abandoning their "
                                   f"bets on a fall.")

        # 7. Liquidity blowout — ATM spread exploding vs its own average
        for right in ("call", "put"):
            bid = self.prices.opt_bid.get((atm, right), 0.0)
            ask = self.prices.opt_ask.get((atm, right), 0.0)
            if bid <= 0 or ask <= bid:
                continue
            spread = ask - bid
            key = (atm, right)
            ema = self._spread_ema.get(key, spread)
            self._spread_ema[key] = ema + 0.05 * (spread - ema)
            if ema > 0.15 and spread >= ema * config.COMMENT_SPREAD_BLOWOUT:
                self._fire("liquidity",
                           f"Liquidity pulled on {atm:.0f} "
                           f"{right.upper()[:4]}: spread {spread:.2f} vs normal "
                           f"{ema:.2f} — market makers stepping away, expect "
                           f"violent ticks.")

        # 8. REVERSAL FUEL — the user's "most important" tell: when sellers/shorts
        # RUN AWAY the price rises (bullish), and the mirror (buyers flee → price
        # falls). We CORROBORATE the futures-OI quadrant with the 30 s order-flow
        # (CVD) so it's a CONFIRMED reversal, not a raw label, state it in plain
        # language, and let it REFRESH while it holds (its own cooldown) so it
        # stays on the marquee — not a one-shot jargon flip.
        quad = self.flow.fut_oi.quadrant
        oi_pct = self.flow.fut_oi.oi_pct
        s30 = self.flow.cvd.slope(30)            # tear-proof; >0 = net buying
        if quad == "SHORT_COVERING" and s30 >= 0:
            self._fire("reversal_fuel",
                       f"BULLISH — SELLERS ARE RUNNING AWAY: shorts covering "
                       f"(futures OI {oi_pct:+.2f}%) AND buy-flow confirming "
                       f"(CVD {s30:+.0f}/s). Rises from here can accelerate sharply.")
        elif quad == "LONG_UNWINDING" and s30 <= 0:
            self._fire("reversal_fuel",
                       f"BEARISH — BUYERS ARE FLEEING: trapped longs bailing out "
                       f"(futures OI {oi_pct:+.2f}%) AND sell-flow confirming "
                       f"(CVD {s30:+.0f}/s). Falls from here can accelerate sharply.")
        # raw quadrant FLIP (uncorroborated, or fresh buildup) — lower-key, on change
        if quad != self._last_quadrant and quad != "NEUTRAL":
            self._last_quadrant = quad
            texts = {
                "LONG_UNWINDING": f"BEARISH — long unwinding: futures OI "
                                  f"{oi_pct:+.2f}% as price falls.",
                "SHORT_COVERING": f"BULLISH — short covering: futures OI "
                                  f"{oi_pct:+.2f}% as price rises.",
                "LONG_BUILDUP": f"BULLISH — fresh long buildup: futures OI "
                                f"{oi_pct:+.2f}% with rising price (new upside bets).",
                "SHORT_BUILDUP": f"BEARISH — fresh short buildup: futures OI "
                                 f"{oi_pct:+.2f}% with falling price (new downside bets).",
            }
            self._fire("fut_quad", texts[quad])
        elif quad == "NEUTRAL":
            self._last_quadrant = quad

        # 9. Heavyweight extremes
        now = clk.now()
        for s in self.basket.extreme_movers(config.COMMENT_HW_MOVE_PCT):
            if now - self._last_hw_alert.get(s.symbol, 0.0) < 900:
                continue
            self._last_hw_alert[s.symbol] = now
            direction = "surging" if s.change_pct > 0 else "sliding"
            self._fire(f"hw_{s.symbol}",
                       f"{s.symbol} ({s.weight:.0f}% of Nifty) {direction} "
                       f"{s.change_pct:+.1f}% — dragging the index "
                       f"{'up' if s.change_pct > 0 else 'down'}.")

        # 10. CROSS-INSTRUMENT TELL (task #37) — BankNifty/FinNifty + the stock
        # basket in plain language, so a trade AGAINST the broad tape (a PE while
        # banks rip up) is obvious to skip. Display/audio only — touches no
        # decision; CRITICAL tier so it is never throttled away. Reads live
        # sister day-% (staleness-gated) + basket sentiment.
        idx = {}
        bn_ltp = 0.0
        for nm in ("BANKNIFTY", "FINNIFTY"):
            ltp = self.prices.idx_ltp.get(nm, 0.0)
            prev = self.prices.idx_prev.get(nm, 0.0)
            ts = self.prices.idx_ts.get(nm, 0.0)
            if ltp > 0 and prev > 0 and (ts <= 0 or clk.mono() - ts <= 60.0):
                idx[nm] = (ltp - prev) / prev * 100.0   # day-change (context)
                if nm == "BANKNIFTY":
                    bn_ltp = ltp
        if idx and bn_ltp <= 0:                          # FinNifty-only fallback
            bn_ltp = self.prices.idx_ltp.get("FINNIFTY", 0.0)
        if idx and bn_ltp > 0:
            now = clk.now()
            h = self._cross_hist
            if not h or now - h[-1][0] >= 5.0:
                h.append((now, bn_ltp))
            past = next((v for t, v in h
                         if now - t <= config.COMMENT_CROSS_MOM_SEC), None)
            mom = ((bn_ltp - past) / past * 100.0) if (past and past > 0) else 0.0
            sent = self.basket.sentiment
            thr = config.COMMENT_CROSS_MOM_PCT
            verdict = ("BULLISH" if mom >= thr else
                       "BEARISH" if mom <= -thr else "NEUTRAL")
            win = int(config.COMMENT_CROSS_MOM_SEC / 60)
            # fire ONLY on a genuine flip (no 3-min repetition); steer by the
            # INTRADAY turn, with the day-change shown as context.
            if verdict == "NEUTRAL":
                self._last_cross = ""                    # reset so the next leg re-fires
            elif verdict != self._last_cross:
                self._last_cross = verdict
                parts = [f"{nm[:4]} {chg:+.2f}%" for nm, chg in idx.items()]
                parts.append(f"stocks {sent:.0f}/100")
                tape = " · ".join(parts)
                if verdict == "BULLISH":
                    self._fire("cross_index_oi",
                               f"BROAD TAPE TURNING UP — BankNifty {mom:+.2f}% last "
                               f"{win}m · {tape} → favour CE.")
                else:
                    self._fire("cross_index_oi",
                               f"BROAD TAPE TURNING DOWN — BankNifty {mom:+.2f}% last "
                               f"{win}m · {tape} → favour PE.")

    def feed(self) -> List[dict]:
        # called from the server thread while the analytics thread appends
        for _ in range(3):
            try:
                return list(self.items)
            except RuntimeError:
                continue
        return []
