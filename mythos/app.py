"""
MYTHOS — application orchestrator.

Thread model (no asyncio in the engine; the web layer alone is async):
    breeze WS thread   → writes PriceStore (lock-free, owned by breeze_connect)
    analytics thread   → 1 Hz: indicators, OI engine, vol engine, basket,
                         signal evaluation, entry attempts, commentary,
                         persistence sampling, daily reset, auto-archive
    exit thread        → 4 Hz: open-position management (near tick speed)
    maintenance thread → 10 s: reconnects + ATM-drift strike subscriptions
    poller threads     → Nifty chain / heavyweight chains / VIX on REST budgets
    uvicorn (main)     → serves dashboard, pushes state JSON over websocket

Every engine object is single-writer: only the analytics thread mutates them,
so no cross-engine locks exist anywhere (run7's core lesson scaled up).
"""

import logging
import logging.handlers
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from . import audio, clk, config
from .archive import AutoArchiver, archive_day
from .commentary import Commentary
from .config import IST
from .feed import BreezeFeed, PriceStore
from .flow import FlowStack
from .gamma import GAMMA_COIL_WINDOW, GammaWatch
from .learning import LearningLoop, MistakeJournal
from .heavyweights import HeavyweightBasket
from .memory import MarketMemory
from .risk import FallRiskMonitor
from .oi_engine import OIEngine
from .radar import BookRadar, OIRadar
from .signals import SignalEngine
from .store import Store
from .trader import PaperTrader
from .vol import VolEngine

log = logging.getLogger("mythos")


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                            "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(config.LOG_DIR, "mythos.log"),
        maxBytes=10_000_000, backupCount=6, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    root = logging.getLogger("mythos")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    for noisy in ("WebsocketLogger", "APILogger", "engineio", "socketio"):
        logging.getLogger(noisy).propagate = False

    # one-line RUNNING IDENTITY — so a stale process/tab is never invisible again
    # (2026-06-15: 5 'recall' crashes were OLD code in a running process while the
    # source was already fixed). git is optional (this repo is non-git) — fall back
    # to the app.py mtime and NEVER raise.
    try:
        from . import __version__ as _ver
    except Exception:
        _ver = "?"
    try:
        import datetime as _dt
        _mtime = _dt.datetime.fromtimestamp(
            os.path.getmtime(__file__), config.IST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        _mtime = "?"
    root.info("MYTHOS v%s · app.py last-modified %s · paper-only=%s",
              _ver, _mtime, not getattr(config, "LIVE_ORDERS", False))


class MythosApp:
    def __init__(self, sim: bool = False, speed: float = 1.0):
        # SIM2 time-warp: set the virtual-clock speed BEFORE anything reads it.
        # speed 1.0 = identical to wall-clock (live / ordinary sim / tests).
        self.speed = float(speed)
        if self.speed != 1.0:
            clk.set_speed(self.speed)
            # at N× the analytics produces fresh state N× faster — push the UI
            # proportionally faster so the fast tape renders smoothly (the socket
            # itself stays on real wall-clock; only the value shrinks).
            config.UI_PUSH_MS = max(60, int(config.UI_PUSH_MS / self.speed))
        self.sim = sim
        if sim:
            # sim must NEVER share state with live: separate DB, trade file,
            # archive prefix — a sim session on the same calendar day would
            # otherwise pollute live capital and IV history
            config.DB_PATH = config.DB_PATH.replace("mythos.db", "mythos_sim.db")
            config.TRADES_JSON = config.TRADES_JSON.replace(
                "trades_today.json", "trades_today_sim.json")
            config.ARCHIVE_PREFIX = "sim_"
            # the learning journal also splits live/sim so a synthetic session
            # can never poison the real mistake history
            config.MISTAKE_JOURNAL_JSON = config.MISTAKE_JOURNAL_JSON.replace(
                "mistake_journal.json", "mistake_journal_sim.json")
            config.ADAPT_STATE_JSON = config.ADAPT_STATE_JSON.replace(
                "adaptive_state.json", "adaptive_state_sim.json")
            assert "sim" in config.MISTAKE_JOURNAL_JSON   # loud guard vs live-journal poisoning
            assert "sim" in config.ADAPT_STATE_JSON
            config.MEMORY_DIR = config.MEMORY_DIR.replace("memory", "memory_sim")
            assert "sim" in config.MEMORY_DIR             # sim must never write the live ledger
            # every sim session is a fresh world at new synthetic price levels —
            # restoring yesterday's open trade once produced a phantom −72 pt
            # "loss" when the restored strike repriced in the new world
            for stale in (config.TRADES_JSON, config.TRADES_JSON + ".tmp"):
                try:
                    if os.path.exists(stale):
                        os.remove(stale)
                except OSError:
                    pass
        self.prices = PriceStore()
        self.store = Store(config.DB_PATH)
        self.oi = OIEngine()
        self.flow = FlowStack()
        self.vol = VolEngine()
        self.basket = HeavyweightBasket()
        self.signals = SignalEngine(self.oi, self.flow, self.vol,
                                    self.basket, self.prices)
        self.trader = PaperTrader(self.prices, self.store,
                                  on_event=self._on_trade_event)
        self.commentary = Commentary(self.oi, self.vol, self.flow,
                                     self.basket, self.prices,
                                     on_alert=lambda t: self._push_event("commentary", t))
        self.archiver = AutoArchiver(self.trader)
        # persistent market memory (DISPLAY-ONLY: only state.build_state reads it;
        # signals/trader never import it — it can't change a trade). Daemon-backed,
        # off the hot path.
        self.memory = MarketMemory(config.MEMORY_DIR)
        self._last_mem_ts = 0.0
        # forward-looking fall/rip early-warning (READ-ONLY HUD + commentary tell;
        # imported here + by state only, never by signals/trader → changes no trade)
        self.risk = FallRiskMonitor(self.oi, self.flow, self.vol,
                                    self.basket, self.prices, self.signals)
        # OPTION BATTLE LINES — defended floors / resisted ceilings of the ATM/ITM
        # Nifty CE & PE premiums (display-only; analytics-thread writer, snapshot read)
        from .levels import BattleLines
        self.battle = BattleLines(self.prices)
        self.feed: Optional[BreezeFeed] = None
        self.simfeed = None

        # UI event stream (audio cues in the browser) — (seq, kind, text)
        self.events: deque = deque(maxlen=50)
        self._event_seq = 0
        self._evt_lock = threading.Lock()

        # OI-delta-flow history for the dashboard (dealer-hedging proxy)
        self.oi_delta_hist: deque = deque(maxlen=900)   # (epoch, value)
        self.gamma_heat: float = 0.0    # delta gained per strike-step of spot
        self.gex: float = 0.0           # dealer gamma exposure (sign = regime)
        self.oi_radar = OIRadar()       # significant OI/volume anywhere
        self.book_radar = BookRadar()   # buyer/seller pressure anywhere
        # spot/futures sparkline history
        self.spot_hist: deque = deque(maxlen=900)
        self.score_hist: deque = deque(maxlen=900)      # (epoch, ce, pe)

        # two-tier gamma-explosion forewarning + the learning loop
        self.gamma_flip: float = 0.0
        self.gamma_stage: str = "idle"
        self.gamma = GammaWatch(self.flow)
        self.journal = MistakeJournal(config.MISTAKE_JOURNAL_JSON)  # path sim-redirected above
        self.journal.load()
        self.learning = LearningLoop(self.journal)
        self._rv_hist: deque = deque(maxlen=200)        # (now_mono, realized_vol_1m) for IGNITING rv-kick
        self._pending_recall = None

        self._stop = threading.Event()
        self._threads = []
        self._last_oi_persist = 0.0
        self._last_frame_ts = 0.0
        self._last_day = self.trader.day
        self.started_at = clk.now()
        self.status = "INITIALISING"

    # ── events ───────────────────────────────────────────────────────────────
    def _push_event(self, kind: str, text: str):
        with self._evt_lock:
            self._event_seq += 1
            self.events.append({"seq": self._event_seq, "kind": kind,
                                "text": text,
                                "ts": datetime.now(IST).strftime("%H:%M:%S")})
        # commentary gets a long 2.6 s chime (it only fires on extreme events,
        # which are by definition worth hearing); trades get their own sounds
        audio.play({"entry": "entry", "exit_win": "win", "exit_loss": "loss",
                    "armed": "armed"}.get(kind, "commentary"))

    def _on_trade_event(self, kind: str, trade):
        if kind == "entry_working":
            # MYTHOS is WORKING a cheaper fill — SILENT and NO position shown.
            # No entry chime here: the chime + the "IN TRADE" panel appear only
            # at the real fill ("entry"), so a manual trader is never tricked
            # into thinking a trade was taken when it hasn't filled yet.
            self.commentary.note(
                f"WORKING ENTRY → buying {trade.direction} {trade.strike:.0f}; "
                f"bidding ₹{trade.limit_price:.2f} for a cheaper price, will take "
                f"market if it runs — this trade WILL be taken.")
            return
        if kind == "entry_lapsed":
            # a working order whose strike never quoted — the slot is freed, no
            # trade was taken. Re-arm the hunts; silent (no entry chime).
            self.commentary.note(
                f"ORDER CANCELLED → {trade.direction} {trade.strike:.0f} never "
                f"quoted; freed the slot, hunting again. No trade taken.")
            self._pending_recall = None
            self.signals.note_exit()
            return
        if kind == "entry":
            self._push_event("entry",
                             f"BUY {trade.direction} {trade.strike:.0f} @ "
                             f"₹{trade.entry_price:.2f} × {trade.lots} lots")
            # tell the user WHY, in plain words, in the commentary ticker
            v = (self.signals.last.ce if trade.direction == "CE"
                 else self.signals.last.pe)
            fired = [c for c in trade.entry_components if c.get("fired")]
            reasons = "; ".join(f"{c['name']} ({c['detail']})" for c in fired) \
                or "zone defense confirmed"
            why = (f"WHY THIS TRADE → {trade.direction} {trade.strike:.0f} "
                   f"[{v.kind} at zone {v.zone_level:.0f}]: {reasons}. "
                   f"Risk: SL ₹{trade.stop_loss:.2f} (−10), target "
                   f"₹{trade.target:.2f} dynamic, trail beyond +8.")
            if self._pending_recall:        # learning recall — fill-only (never on a lapse)
                why += "  " + self._pending_recall
                self._pending_recall = None
            self.commentary.note(why)
        else:
            self._push_event(kind,
                             f"EXIT {trade.direction} {trade.strike:.0f} "
                             f"{trade.pnl_pts:+.1f} pts (₹{trade.pnl_cash:+,.0f}) "
                             f"[{trade.exit_reason}]")
            if kind == "exit_loss":
                # burn the zone that failed — same zone now needs overwhelming
                # evidence before re-entry (structural guard, not a timer)
                self.signals.note_stop(trade.direction)
            # learning loop: freeze the conviction + thesis zone and queue the
            # 60s "what happened next" watch BEFORE note_exit (snapshot guards
            # against a re-entry inside the window clearing _pos_conv).
            conv = dict(self.signals._pos_conv or {})
            ez = self.signals._entry_zone.get(trade.direction, 0.0)
            self.learning.on_exit(trade, conv, ez, clk.mono())
            # ANY exit: all hunts rebuild confirmation from zero — no
            # pre-armed instant flips into the opposite direction
            self.signals.note_exit()

    # ── startup ──────────────────────────────────────────────────────────────
    def start(self):
        audio.copy_run7_assets()
        audio.init()
        self._seed_from_store()

        if self.sim:
            from .sim_feed import SimFeed
            self.signals.bypass_time_gates = True
            self.trader.bypass_time = True
            self.simfeed = SimFeed(self.prices, self.basket)
            self.simfeed.start()
            # warm the momentum stack instantly — without this RSI/SuperTrend/
            # ADX are blind for 14+ min and the engine can't reach threshold
            self.flow.seed_candles(self.simfeed.warmup_candles())
            self.status = (f"SIM ×{self.speed:g}" if self.speed != 1.0
                           else "SIMULATION")
            log.info("=== MYTHOS started in SIMULATION mode (speed %g×, "
                     "indicators warm: RSI %.0f, ST %s) ===", self.speed,
                     self.flow.rsi.value, self.flow.supertrend.direction)
        else:
            self.feed = BreezeFeed(self.prices)
            self.status = "CONNECTING"
            self.feed.login()
            self.feed.resolve_heavyweights()
            self.feed.connect_ws()
            self.feed.subscribe_index()
            self.feed.subscribe_heavyweights()
            # spot is the lifeblood — retry the REST seed a few times before
            # giving up, then subscribe the option band off it
            spot = 0.0
            for attempt in range(4):
                spot = self.feed.fetch_spot_bootstrap()
                if spot > 0:
                    break
                log.warning("spot bootstrap empty (attempt %d/4) — retrying",
                            attempt + 1)
                clk.sleep(1.5)
            subs = 0
            if spot > 0:
                subs = self.feed.subscribe_strikes(self.prices.atm)
            else:
                log.critical("NO SPOT at startup — chain not subscribed; "
                             "maintenance loop will keep retrying REST spot")
            if not self.feed._spot_token:
                log.critical("NIFTY spot TOKEN unresolved — WS spot relies on "
                             "the stock_name fragment; REST refresh is the "
                             "safety net")
            self.feed.fetch_vix()
            self.feed.resolve_sentiment_indices()
            self._bootstrap_hw_quotes()
            chain = self.feed.fetch_nifty_chain()
            self._ingest_chain(chain)
            self.status = "LIVE" if subs > 0 else "DEGRADED"
            log.info("=== MYTHOS %s — spot %.1f, %d strike subscriptions, "
                     "chain %d strikes ===", self.status, spot, subs, len(chain))

        self._spawn(self._analytics_loop, "Analytics")
        self._spawn(self._exit_loop, "ExitWatch")
        if not self.sim:
            self._spawn(self._maintenance_loop, "Maintenance")
            self._spawn(self._nifty_chain_loop, "NiftyChain")
            self._spawn(self._hw_chain_loop, "HWChains")
            self._spawn(self._vix_loop, "VIX")

    def _spawn(self, fn, name):
        t = threading.Thread(target=fn, daemon=True, name=name)
        t.start()
        self._threads.append(t)

    def stop(self):
        self._stop.set()
        if self.simfeed:
            self.simfeed.stop()
        self.store.stop()
        try:
            self.memory.stop()          # flush the market memory to disk
        except Exception:
            pass

    def _seed_from_store(self):
        if self.sim:
            # never seed sim from persisted history: a previous sim session
            # ran at unrelated synthetic price/IV levels — replaying it
            # corrupts indicators and IV rank (seen live: rank went negative
            # and the chop filter blocked every entry)
            return
        day = datetime.now(IST).strftime("%Y-%m-%d")
        candles = self.store.load_today_candles(day)
        if candles:
            self.flow.seed_candles(candles)
            log.info("Warm restart: %d candles replayed", len(candles))
        self.vol.seed_history(self.store.load_daily_iv_closes(),
                              self.store.load_today_iv(day))

    def _bootstrap_hw_quotes(self):
        for sym in config.HEAVYWEIGHTS:
            ltp, prev = self.feed.fetch_stock_quote(sym)
            if prev > 0:
                self.basket.set_prev_close(sym, prev, ltp)
            clk.sleep(0.25)        # gentle on the REST budget

    def _ingest_chain(self, chain: dict):
        """REST chain → OI engine (full width) + vol engine inputs."""
        now = clk.now()
        for (k, right), d in chain.items():
            if d.get("oi", 0) > 0:
                self.oi.update_strike(k, right, d["oi"], now)

    # ── analytics loop (1 Hz) ────────────────────────────────────────────────
    def _analytics_loop(self):
        last_iv_save = 0.0
        last_health = 0.0
        last_dropped = 0
        while not self._stop.is_set():
            t0 = clk.mono()
            try:
                self._analytics_pass(t0)
            except Exception as e:
                log.exception("analytics pass failed: %s", e)
            # tick-loss watchdog — the user's explicit fear; never silent
            dropped = self.prices.fut_ticks_dropped
            if dropped > last_dropped:
                log.error("TICK LOSS: %d futures ticks dropped (buffer "
                          "overflow — analytics stalled). Total %d.",
                          dropped - last_dropped, dropped)
                last_dropped = dropped
            took = clk.mono() - t0
            if took > 0.9 and t0 - last_health > 30:
                log.warning("analytics pass slow: %.2fs (fut buffer %d)",
                            took, len(self.prices.fut_ticks))
                last_health = t0
            clk.sleep(max(0.05, config.ANALYTICS_SEC - took))

    def _analytics_pass(self, t0: float):
        # day roll
        if self.trader.roll_day_if_needed():
            self.flow.reset_session()
            self.oi.reset_session()
            self.vol.reset_session()
            self.signals.reset_session()
            self.basket.reset_session()

        spot, fut, atm, ce_ltp, pe_ltp = self.prices.freeze_core()
        if spot <= 0:
            return
        now = clk.now()
        day = self.trader.day
        self.memory.maybe_decay(day)        # idempotent; only acts on a day change

        # 1. drain futures ticks → flow stack (CVD, VWAP, candles, indicators)
        ticks = self.prices.fut_ticks
        n = len(ticks)
        fut_vol = 0.0                       # traded futures volume drained this frame
        for _ in range(n):
            try:
                price, qty, bid, ask, foi = ticks.popleft()
            except IndexError:
                break
            fut_vol += qty
            closed = None
            self.flow.vwap.update(price, qty)
            self.flow.avwap.update(price, qty)
            self.flow.swings.update(price)
            self.flow.cvd.on_tick(price, qty, bid, ask)
            if foi > 0:
                self.flow.fut_oi.update(price, foi)
            closed = self.flow.candles_1m.update(price, qty)
            if closed:
                self.flow.rsi.on_candle(closed)
                self.flow.atr.on_candle(closed)
                self.flow.supertrend.on_candle(closed)
                self.flow.adx.on_candle(closed)
                self.store.save_candle(day, closed)
        # recorder: per-frame traded futures volume, so replay can reconstruct a
        # real CVD (the flight recorder previously stored none → replay fed qty=0 →
        # the flow/consensus signals were dead in replay; this revives them).
        self._frame_fut_vol = fut_vol

        # 2. options OI / volume → OI engine
        strikes = self.prices.snapshot_strikes(atm_override=atm)
        for (k, right), d in strikes.items():
            if d["oi"] > 0:
                self.oi.update_strike(k, right, d["oi"], now)
            if d["vol"] > 0:
                self.oi.update_volume_baseline(k, right, d["vol"])
        self.oi.note_spot(spot, now)
        self.oi.recompute(atm, spot)

        # 3. volatility engine
        from . import greeks as gk
        T = gk.years_to_expiry(config.expiry_dt_ist(), datetime.now(IST))
        self.vol.update_chain(spot, strikes, T, atm)
        self.vol.update_spot(spot)
        if self.vol.atm_iv > 0 and now - self._last_oi_persist > 60.0:
            self.store.save_iv(day, now, self.vol.atm_iv)
            self.store.save_oi_snapshot(day, now, self.oi.oi_snapshot())
            self._last_oi_persist = now

        # 4. heavyweights
        for sym, ltp in list(self.prices.hw_ltp.items()):
            self.basket.on_tick(sym, ltp)
        self.basket.recompute(spot)

        # snapshot the merged OI ONCE per pass — GEX and the OI-delta-flow
        # both consume it; rebuilding it 3× per second was wasted work that
        # ate into the 1 s analytics budget (SRE review)
        merged = self.prices.merged_oi()

        # 5. OI delta flow (dealer-hedging proxy): Σ delta × OI across chain
        self._compute_oi_delta_flow(spot, atm, T, now, merged)

        # 5b. GAMMA HEAT — the buyer's convexity weapon: delta gained per one
        # strike-step of spot movement. Near weekly expiry ATM gamma explodes;
        # in that regime winners go convex (and so do losers).
        try:
            import numpy as np
            iv = self.vol.chain_iv.get((atm, "call")) or self.vol.atm_iv
            if iv and spot > 0:
                g = gk.greeks(spot, np.array([atm]), T, np.array([iv]), "call")
                gamma = float(np.nan_to_num(g["gamma"][0]))
                self.gamma_heat = round(gamma * config.STRIKE_STEP, 3)
        except Exception:
            pass

        # 5c. DEALER GAMMA EXPOSURE (GEX) — net gamma of the dealers' book
        # estimated from the chain (long the calls customers sold, short the
        # puts customers bought, classic convention). The SIGN sets the tape:
        #   GEX > 0 → dealers hedge AGAINST moves → tape dampened, fades work
        #   GEX < 0 → dealers must CHASE moves → tape amplified, trends extend
        # An amplified tape is the option buyer's regime.
        try:
            import numpy as np
            gex = 0.0
            signed_g = {}     # strike -> net dealer signed gamma (for the flip level — free here)
            for right, sign in (("call", +1.0), ("put", -1.0)):
                ks, ois, ivs = [], [], []
                for (k, r), oi_val in merged.items():
                    if r != right or oi_val <= 0:
                        continue
                    iv_k = self.vol.chain_iv.get((k, r))
                    if iv_k:
                        ks.append(k)
                        ois.append(oi_val)
                        ivs.append(iv_k)
                if ks:
                    g = gk.greeks(spot, np.array(ks), T, np.array(ivs), right)
                    gam = np.nan_to_num(g["gamma"])
                    gex += sign * float((gam * np.array(ois)).sum())
                    for k_i, gm_i, oi_i in zip(ks, gam, ois):
                        signed_g[k_i] = signed_g.get(k_i, 0.0) + sign * float(gm_i) * oi_i
            # normalize to "delta the dealers must trade per 1% index move"
            self.gex = round(gex * spot * 0.01 / 1e5, 2)   # in lakh-deltas
            self.gamma_flip = self.gamma.flip_level(signed_g)   # the coil/pin level
        except Exception:
            pass

        # 6. histories for sparklines
        self.spot_hist.append((now, spot))

        # 6a. day high / low for the index spot (true session extremes, reset
        # each trading day) — the analytics loop is the one path BOTH live and
        # sim feed through, so 1 Hz here captures the day's range cleanly.
        if spot > 0:
            _d = datetime.now(IST).date()
            if getattr(self, "_hl_day", None) != _d:
                self._hl_day = _d
                self.spot_day_high = spot
                self.spot_day_low = spot
            else:
                self.spot_day_high = max(self.spot_day_high, spot)
                self.spot_day_low = min(self.spot_day_low, spot)

        # 6b. ACTIVITY RADARS — every contract, every instrument, every pass
        for (k, right), d in strikes.items():
            side = "CE" if right == "call" else "PE"
            if d["oi"] > 0:
                self.oi_radar.ingest_oi("NIFTY", f"{k:.0f}", side, d["oi"],
                                        bullish_when_build=(side == "PE"))
            if d["vol"] > 0:
                self.oi_radar.ingest_volume("NIFTY", f"{k:.0f} {side}", d["vol"])
            bq = self.prices.opt_bqty.get((k, right), 0.0)
            aq = self.prices.opt_aqty.get((k, right), 0.0)
            if bq > 0 and aq > 0:
                self.book_radar.ingest("NIFTY", f"{k:.0f} {side}", bq, aq,
                                       bullish_when_buyers=(side == "CE"))
        if self.prices.futures_oi > 0:
            self.oi_radar.ingest_oi("NIFTY FUT", "near-month", "FUT",
                                    self.prices.futures_oi,
                                    bullish_when_build=True)
        if self.prices.fut_bqty > 0 and self.prices.fut_aqty > 0:
            self.book_radar.ingest("NIFTY FUT", "near-month",
                                   self.prices.fut_bqty, self.prices.fut_aqty)
        for sym in list(self.prices.hw_ltp):
            v = self.prices.hw_vol.get(sym, 0.0)
            if v > 0:
                self.oi_radar.ingest_volume(sym, "cash", v)
            bq = self.prices.hw_bqty.get(sym, 0.0)
            aq = self.prices.hw_aqty.get(sym, 0.0)
            if bq > 0 and aq > 0:
                self.book_radar.ingest(sym, "cash", bq, aq)

        # 7. zone-hunter evaluation + entry
        decision = self.signals.evaluate()
        # long chime on a fresh ARMING — "get ready, a trade may be near"
        if not hasattr(self, "_hunt_states"):
            self._hunt_states = {"CE": "", "PE": ""}
            self._armed_alert_ts = {"CE": 0.0, "PE": 0.0}
        for d, view in (("CE", decision.ce), ("PE", decision.pe)):
            active = view.state in ("ARMED", "CONFIRMING", "FIRE")
            was = self._hunt_states[d] in ("ARMED", "CONFIRMING", "FIRE")
            if active and not was and now - self._armed_alert_ts[d] > 60.0:
                self._armed_alert_ts[d] = now
                self._push_event("armed",
                                 f"⚔ GET READY — {d} armed at "
                                 f"{view.kind.lower()} zone {view.zone_level:.0f} "
                                 f"(evidence {view.ok_count}/{view.needed})")
            self._hunt_states[d] = view.state
        self.score_hist.append((now,
                                decision.ce.ok_count / 8.0,
                                decision.pe.ok_count / 8.0))
        if decision.allowed and not self.trader.open:
            # learning recall: if this zone/direction has burned us the same way
            # before, prep the lesson — it's attached to the WHY only on a FILL.
            view = decision.ce if decision.direction == "CE" else decision.pe
            zb = round(view.zone_level / config.STRIKE_STEP) * config.STRIKE_STEP
            # learning reads (recall + the adaptive trust bar). HARDENED: a failure
            # anywhere in the learning layer must NEVER abort the analytics pass or
            # block a trade — it degrades to "no recall, base evidence bar" and the
            # entry proceeds normally. On 2026-06-15 a stale-code recall
            # AttributeError aborted the WHOLE pass 5× (no decision those cycles);
            # one ancillary failure can never again take down the decision.
            try:
                self._pending_recall = self.journal.recall(decision.direction, zb)
                # CLOSED ADAPTIVE LOOP — the TrustBook RAISES (never lowers) the
                # evidence bar for a context it has learned keeps failing. Touches
                # ONLY the entry bar — never an exit.
                bump = self.learning.trust.trust_gate(decision.direction, zb)
                brake = self.learning.trust.book_brake()   # book brake when losing
            except Exception as e:
                self._pending_recall = None
                bump = brake = 0
                log.debug("learning gate failed (entry proceeds at base bar): %s", e)
            total = bump + brake
            base_need = view.needed
            cap = max(base_need, len(view.evidence) - config.ADAPT_MIN_HEADROOM)
            effective_need = min(max(base_need, base_need + total), cap)
            self.learning.gated_now = {
                "dir": decision.direction, "zone_bucket": zb, "bump": bump,
                "brake": brake, "need": effective_need, "ok": view.ok_count}

            if view.ok_count < effective_need:
                why = (f"BOOK BRAKE +{brake} " if brake else "") + \
                      (f"trust +{bump} " if bump else "")
                decision.blocked = (f"{why}— {view.ok_count}/{effective_need} "
                                    f"(earning back trust)")
            else:
                t = self.trader.try_enter(decision, self.vol.expected_move, self.oi)
                if t:
                    self.signals.note_entry(t.direction)
        else:
            self.learning.gated_now = None

        # 7b. dynamic target for open trades — the market's structure moves,
        # so the objective moves with it (user: "why is the target static?")
        for t in self.trader.snapshot_open():
            if getattr(t, "pending", False):
                continue                          # resting limit — not filled yet
            self._update_dynamic_target(t, spot, T)

        # 7c. live position conviction — hold / be-ready / square-off verdict
        # (a pending limit has no position to judge — exclude it)
        open_snap = [x for x in self.trader.snapshot_open()
                     if not getattr(x, "pending", False)]
        if open_snap:
            conv = self.signals.position_conviction(open_snap[0].direction)
            prev_tone = getattr(self, "_last_conv_tone", "")
            if conv["tone"] == "danger" and prev_tone not in ("", "danger"):
                self._push_event("commentary",
                                 f"⚠ POSITION ALERT — {conv['verdict']} "
                                 f"({conv['ok']}/{conv['total']} factors left)")
            self._last_conv_tone = conv["tone"]
        else:
            self._last_conv_tone = ""

        # 7d. two-tier gamma-explosion forewarning (LOADING quiet → IGNITING loud).
        # Fires flat OR in-trade. Reuses market_state() COILING + the flip level
        # computed for free in the GEX loop + commentary._last_gex for prev-gex.
        now_mono = clk.mono()
        # fall/rip early-warning — fuse the leading→lagging signals into a 0-100
        # risk that builds WHILE a roll-over forms (read-only HUD; own try inside)
        self.risk.update(spot, now_mono)
        try:
            self.battle.update(spot, atm,           # battle-lines across the complex
                               getattr(self.prices, "fut_bqty", 0.0),
                               getattr(self.prices, "fut_aqty", 0.0))
        except Exception as e:
            log.debug("battle-lines update failed: %s", e)
        self._rv_hist.append((now_mono, self.vol.realized_vol_1m))
        rv_then = next((v for ts, v in self._rv_hist
                        if now_mono - ts >= GAMMA_COIL_WINDOW),
                       self.vol.realized_vol_1m)
        prev_gex = getattr(self.commentary, "_last_gex", 0.0)
        regime_state = self.signals.market_state()[0]
        self.gamma.flow_accel = self.flow.cvd.accelerating()
        self.gamma_stage = self.gamma.scan(
            spot, self.gamma_heat, self.gex, prev_gex, regime_state,
            self.vol.realized_vol_1m, rv_then, self.gamma_flip, now_mono,
            self.commentary.note, self.commentary._fire, bool(open_snap))
        # in-trade IGNITING → radar, so the heart's Slot C can flash it (the
        # commentary feed can't carry it — Slot C only reads BULLISH/BEARISH text)
        if self.gamma_stage == "igniting" and open_snap:
            tone = "bullish" if self.gamma.dir == "up" else "bearish"
            self.oi_radar._emit("blaster_igniting", "NIFTY",
                                f"{self.gamma_flip:.0f}", self.gamma.last_ignite_text,
                                tone, config.HEART_C_MAG_CONFIRM + 2.0)

        # 8. commentary + learning summaries + auto archive — ALL ancillary, so
        # hardened: a failure here degrades only these features (one cycle), never
        # the decision (already made above) nor the flight recorder below.
        try:
            self.commentary.scan(spot, atm, gamma_heat=self.gamma_heat,
                                 gex=self.gex, gamma_stage=self.gamma_stage,
                                 cross_blocked=getattr(
                                     getattr(self.signals, "last", None),
                                     "cross_blocked", ""))
            # persistent fall/rip tell — fires on its own cooldown (refreshes while
            # the risk stays elevated), so the warning stays on the marquee
            _rk = self.risk.loud_kind()
            if _rk:
                self.commentary._fire(_rk, self.risk.tell)
            # learning loop: finalize any post-exit watch that hit +60s, and the
            # once-daily EOD summary (read closed[] now — day-roll wipes it later)
            for entry, speak in self.learning.tick(now_mono, self.prices):
                if speak:
                    self.commentary.note(entry["verdict_line"], kind="learning")
            self.learning.eod_tick(datetime.now(IST),
                                   self.trader.snapshot_closed(0),
                                   self.commentary.note)
            self.archiver.tick()
            # PERSISTENT MARKET MEMORY — copy frozen primitives + enqueue (the
            # daemon does all folding + disk I/O off-thread). Throttled, and it
            # holds ZERO reference to any live structure, so it can't slow the pass
            # or re-trigger the cross-thread price-freeze. DISPLAY-ONLY.
            if now_mono - self._last_mem_ts >= config.MEM_OBSERVE_SEC:
                self._last_mem_ts = now_mono
                _st = (self.flow.supertrend.direction or "").lower()  # emits UP/DOWN
                _stb = 1.0 if _st == "up" else (-1.0 if _st == "down" else 0.0)
                _vw = self.flow.vwap.value
                _vb = (1.0 if spot > _vw else -1.0) if _vw > 0 else 0.0
                _cb = self.flow.cvd.slope(60)              # tear-proof
                _cbs = 1.0 if _cb > 0 else (-1.0 if _cb < 0 else 0.0)
                nifty_bias = max(-1.0, min(1.0, (_stb + _vb + _cbs) / 3.0))
                sisters = {}
                for _nm in ("BANKNIFTY", "FINNIFTY"):
                    _l = self.prices.idx_ltp.get(_nm, 0.0)      # atomic .get, no iter
                    _p = self.prices.idx_prev.get(_nm, 0.0)
                    if _l > 0 and _p > 0:
                        sisters[_nm] = (_l - _p) / _p * 100.0
                zones = [(z.kind, z.level, z.strength, z.oi, z.building)
                         for z in (list(self.oi.support_zones)
                                   + list(self.oi.resistance_zones))]
                self.memory.observe({
                    "ts": now_mono, "day": day, "spot": spot, "zones": zones,
                    "pcr": self.oi.near_pcr, "max_pain": self.oi.max_pain,
                    "nifty_bias": nifty_bias, "sisters": sisters,
                    "basket": self.basket.sentiment})
        except Exception as e:
            log.debug("commentary/learning/archive/memory step failed (ancillary): %s", e)

        # 9. FLIGHT RECORDER — snapshot the whole market so this exact session
        # can be replayed through a modified engine later (keystone)
        if config.REPLAY_RECORD and \
                now - self._last_frame_ts >= config.REPLAY_FRAME_SEC:
            self._last_frame_ts = now
            try:
                self.store.save_frame(day, now, self._build_frame(spot, atm))
            except Exception as e:
                log.debug("frame record failed: %s", e)

    def _update_dynamic_target(self, t, spot: float, T: float):
        """Target = the premium this option would carry if spot travelled to
        the nearest OPPOSING OI wall (resistance for CE, support for PE),
        capped by the expected move. Ratchets up only — never down — and never
        below the baseline entry+12. Display/intent only: exits remain owned
        by the trail, so a generous target can't hold a dying trade hostage."""
        try:
            from . import greeks as gk
            if t.direction == "CE":
                z = self.oi.nearest_resistance(spot)
                wall = z.level if z else 0.0
                if wall <= spot:
                    return
                if self.vol.expected_move > 0:
                    wall = min(wall, spot + self.vol.expected_move)
            else:
                z = self.oi.nearest_support(spot)
                wall = z.level if z else 0.0
                if wall <= 0 or wall >= spot:
                    return
                if self.vol.expected_move > 0:
                    wall = max(wall, spot - self.vol.expected_move)
            iv = (self.vol.chain_iv.get((t.strike, t.right))
                  or self.vol.atm_iv or 0.13)
            projected = float(gk.bs_price(wall, t.strike, T, iv, t.right))
            new_tgt = max(t.entry_price + config.TARGET_POINTS, round(projected, 2))
            if new_tgt > t.target:
                t.target = new_tgt
        except Exception as e:
            log.debug("dynamic target failed: %s", e)

    def _build_frame(self, spot: float, atm: float) -> dict:
        """Compact, complete market snapshot for the flight recorder — enough
        to reconstruct PriceStore and drive the engine in replay."""
        p = self.prices
        n = config.NUM_STRIKES
        opts = {}
        for off in range(-n, n + 1):
            k = atm + off * config.STRIKE_STEP
            for right in ("call", "put"):
                ltp = p.opt_ltp.get((k, right), 0.0)
                if ltp <= 0:
                    continue
                opts[f"{k:.0f}{right[0]}"] = [
                    round(ltp, 2),
                    round(p.opt_oi.get((k, right), 0.0), 0),
                    round(p.opt_vol.get((k, right), 0.0), 0),
                    round(p.opt_bid.get((k, right), 0.0), 2),
                    round(p.opt_ask.get((k, right), 0.0), 2),
                    round(p.opt_bqty.get((k, right), 0.0), 0),
                    round(p.opt_aqty.get((k, right), 0.0), 0),
                ]
        # wide chain OI (strikes beyond the WS band)
        chain = {f"{k:.0f}{r[0]}": round(v, 0)
                 for (k, r), v in p.chain_oi.items() if v > 0}
        # Heavyweight order-flow + per-symbol freshness, and sister-index
        # freshness. Price alone is ~coincident with Nifty (the basket IS
        # ~62% of it); only per-stock order-flow can genuinely LEAD. Record it
        # now — defensively, never trading on it (LEAD_ENTRY_MODE off) — so
        # that every live session from today forward builds the dataset a real
        # lead-signal must be calibrated and replay-proven against. All .get()
        # with defaults: this runs every frame on the live path, must not raise.
        now_m = clk.mono()
        hwf = {}
        for sym, ltp in p.hw_ltp.items():
            if ltp <= 0:
                continue
            hwf[sym] = [
                round(p.hw_bqty.get(sym, 0.0), 0),
                round(p.hw_aqty.get(sym, 0.0), 0),
                round(now_m - p.hw_ts.get(sym, now_m), 1),
            ]
        idx_age = {k: round(now_m - p.idx_ts.get(k, now_m), 1)
                   for k in p.idx_ltp}
        return {
            "spot": round(spot, 2),
            "fut": round(p.futures, 2),
            "fut_oi": round(p.futures_oi, 0),
            "fut_bq": round(p.fut_bqty, 0), "fut_aq": round(p.fut_aqty, 0),
            "fut_vol": round(getattr(self, "_frame_fut_vol", 0.0), 0),
            "vix": round(p.vix, 2),
            "atm": atm,
            "opts": opts,
            "chain": chain,
            "idx": {k: round(v, 2) for k, v in p.idx_ltp.items()},
            "idx_prev": {k: round(v, 2) for k, v in p.idx_prev.items()},
            "idx_age": idx_age,
            # sister-index OI digest (task #37) — empty until SISTER_CHAIN_ON
            "idx_pcr": {k: round(v, 3) for k, v in p.idx_pcr.items()},
            "idx_pw": {k: round(v, 0) for k, v in p.idx_put_wall.items()},
            "idx_cw": {k: round(v, 0) for k, v in p.idx_call_wall.items()},
            "hw": {k: round(v, 2) for k, v in p.hw_ltp.items()},
            "hwf": hwf,
        }

    def _compute_oi_delta_flow(self, spot, atm, T, now, merged=None):
        try:
            import numpy as np
            from . import greeks as gk
            if merged is None:
                merged = self.prices.merged_oi()
            total = 0.0
            for right in ("call", "put"):   # put deltas are negative — net flow
                ks, ois, ivs = [], [], []
                for (k, r), oi_val in merged.items():
                    if r != right or oi_val <= 0:
                        continue
                    iv = self.vol.chain_iv.get((k, r))
                    if not iv:
                        continue
                    ks.append(k)
                    ois.append(oi_val)
                    ivs.append(iv)
                if not ks:
                    continue
                g = gk.greeks(spot, np.array(ks), T, np.array(ivs), right)
                d = np.nan_to_num(g["delta"])
                total += float((d * np.array(ois)).sum())
            self.oi_delta_hist.append((now, round(total, 0)))
        except Exception as e:
            log.debug("oi delta flow failed: %s", e)

    # ── exit loop (4 Hz) ─────────────────────────────────────────────────────
    def _exit_loop(self):
        while not self._stop.is_set():
            try:
                if self.trader.open:
                    direction = self.trader.open[0].direction
                    slope = self.flow.cvd.slope(60)
                    trend_agrees = slope > 0 if direction == "CE" else slope < 0
                    # gamma ride: in a high-gamma regime with the trend agreed,
                    # convexity pays the patient — the trail earns extra room
                    gamma_ride = self.gamma_heat >= 0.18 and trend_agrees
                    # our-side premium velocity + live decision direction — used
                    # only by the pending-fill knife guard (cheap-entry fix)
                    try:
                        _pv = self.signals.prem.velocity(direction)
                        _ld = getattr(self.signals.last, "direction", "")
                    except Exception:
                        _pv, _ld = 0.0, ""
                    self.trader.check_exits(
                        self.signals.live_score(direction), trend_agrees,
                        gamma_ride, _pv, _ld)
            except Exception as e:
                log.exception("exit loop failed: %s", e)
            clk.sleep(config.EXIT_CHECK_SEC)

    # ── maintenance / pollers (live mode) ────────────────────────────────────
    def _maintenance_loop(self):
        while not self._stop.is_set():
            try:
                self.feed.reconnect_if_needed()
                self.feed.maintain_subscriptions()
                # defense-in-depth: if the WS spot has gone stale (or never
                # arrived because the index name string didn't match), pull it
                # from REST so the system is never blind to spot all day
                # (audit finding — spot was a single point of failure)
                spot_age = (clk.mono() - self.prices.spot_ts
                            if self.prices.spot_ts else 9e9)
                if spot_age > 10.0:
                    s = self.feed.fetch_spot_bootstrap()
                    if s > 0:
                        # spot recovered → make sure the chain is subscribed
                        self.feed.subscribe_strikes(self.prices.atm)
            except Exception as e:
                log.warning("maintenance failed: %s", e)
            clk.sleep(8.0)

    def _nifty_chain_loop(self):
        while not self._stop.is_set():
            clk.sleep(config.NIFTY_CHAIN_POLL_SEC)
            try:
                self._ingest_chain(self.feed.fetch_nifty_chain())
            except Exception as e:
                log.warning("nifty chain poll failed: %s", e)

    def _hw_chain_loop(self):
        symbols = list(config.HEAVYWEIGHTS)
        i = 0
        while not self._stop.is_set():
            clk.sleep(config.HW_POLL_STAGGER_SEC)
            sym = symbols[i % len(symbols)]
            i += 1
            try:
                ce, pe = self.feed.fetch_stock_chain(sym)
                if ce or pe:
                    self.basket.on_chain(sym, ce, pe)
                    # heavyweight strikes into the OI radar — "any strike of
                    # any instrument"
                    for rows, side in ((ce, "CE"), (pe, "PE")):
                        for r in rows or []:
                            try:
                                k = float(r.get("strike_price") or 0)
                                oi = float(r.get("open_interest") or 0)
                                if k > 0 and oi > 0:
                                    self.oi_radar.ingest_oi(
                                        sym, f"{k:g}", side, oi,
                                        bullish_when_build=(side == "PE"))
                            except (TypeError, ValueError):
                                continue
            except Exception as e:
                log.debug("HW chain %s failed: %s", sym, e)

    def _vix_loop(self):
        last_idx = 0.0
        last_sis = 0.0
        sis_i = 0
        sisters = list(getattr(config, "SENTIMENT_INDICES", {}).keys())
        while not self._stop.is_set():
            clk.sleep(config.SENTIMENT_POLL_SEC)
            try:
                self.feed.poll_sentiment_indices()
                if clk.mono() - last_idx >= config.VIX_POLL_SEC:
                    self.feed.fetch_vix()
                    last_idx = clk.mono()
                # SISTER OI (task #37, gated off by default): round-robin one
                # sister per cadence so total stays well under the REST cap.
                if (getattr(config, "SISTER_CHAIN_ON", False) and sisters
                        and clk.mono() - last_sis >= config.SISTER_CHAIN_POLL_SEC):
                    self.feed.fetch_sister_chain(sisters[sis_i % len(sisters)])
                    sis_i += 1
                    last_sis = clk.mono()
            except Exception as e:
                log.debug("sentiment/VIX poll failed: %s", e)
