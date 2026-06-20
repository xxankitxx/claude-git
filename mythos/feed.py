"""
MYTHOS — Breeze data layer: websocket feed + REST pollers.

Architecture carried over from run7 (proven zero tick drops across months of
live sessions):

    WS callback (breeze thread)  →  writes plain dicts on PriceStore. No lock,
                                    no queue. Single writer; CPython GIL makes
                                    each dict assignment atomic.
    Analytics / trader / server  →  read the same dicts lock-free.

REST pollers run in their own daemon threads on fixed budgets:
    * Nifty option chain  (both rights)        every 90 s   ≈ 1.3 calls/min
    * Heavyweight chains  (round-robin 1 stock / 20 s)      ≈ 6   calls/min
    * India VIX                                 every 60 s  ≈ 1   call/min
  Total ≈ 9 calls/min against Breeze's 100/min cap — comfortable headroom.

Heavyweight tick routing: equity ticks are identified by token
(tick['symbol'] == "4.1!<token>"); tokens are resolved at startup via
get_names() which is an offline CSV lookup inside breeze_connect (zero API
cost). No hardcoded ISEC codes anywhere.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from . import clk, config

log = logging.getLogger("mythos.feed")

NEW_TICK_EVENT = threading.Event()


# ═════════════════════════════════════════════════════════════════════════════
# PRICE STORE — lock-free, single-writer (WS callback), multi-reader
# ═════════════════════════════════════════════════════════════════════════════
class PriceStore:
    def __init__(self):
        self._snap_lock = threading.Lock()      # only for snapshot_strikes()

        # index
        self.spot: float = 0.0
        self.spot_ts: float = 0.0
        self.futures: float = 0.0
        self.futures_ts: float = 0.0
        self.vix: float = 0.0

        # futures microstructure for CVD (written per tick, drained by analytics).
        # 20000 ≈ 6-15 min of buffer even on a violent day (Nifty futures peak
        # ~30-50 ticks/s) — analytics drains ALL each pass, so this only fills
        # if analytics stalls badly; fut_ticks_dropped makes any loss VISIBLE.
        self.fut_ticks: deque = deque(maxlen=20000)  # (price, qty, bid, ask, oi)
        self.fut_ticks_dropped: int = 0
        self.futures_oi: float = 0.0

        # options around ATM: key = (strike, 'call'|'put')
        self.opt_ltp: Dict[Tuple[float, str], float] = {}
        self.opt_ts:  Dict[Tuple[float, str], float] = {}
        self.opt_bid: Dict[Tuple[float, str], float] = {}
        self.opt_ask: Dict[Tuple[float, str], float] = {}
        self.opt_oi:  Dict[Tuple[float, str], float] = {}
        self.opt_vol: Dict[Tuple[float, str], float] = {}
        self.opt_bqty: Dict[Tuple[float, str], float] = {}   # best-bid quantity
        self.opt_aqty: Dict[Tuple[float, str], float] = {}   # best-ask quantity

        # full-chain OI from REST (wider than WS subscriptions)
        self.chain_oi: Dict[Tuple[float, str], float] = {}
        self.chain_ts: float = 0.0

        # heavyweights: NSE symbol -> ltp / volume / book sizes
        self.hw_ltp: Dict[str, float] = {}
        self.hw_ts: Dict[str, float] = {}
        self.hw_vol: Dict[str, float] = {}
        self.hw_bqty: Dict[str, float] = {}
        self.hw_aqty: Dict[str, float] = {}

        # futures book sizes (latest)
        self.fut_bqty: float = 0.0
        self.fut_aqty: float = 0.0

        # sister indices (BANKNIFTY/FINNIFTY) — sentiment only, never traded
        self.idx_ltp: Dict[str, float] = {}
        self.idx_prev: Dict[str, float] = {}
        self.idx_ts: Dict[str, float] = {}
        # sister-index option-chain digest (task #37, records from now forward;
        # lightweight PCR + walls per the basket abstraction, not full per-strike)
        self.idx_pcr: Dict[str, float] = {}
        self.idx_put_wall: Dict[str, float] = {}
        self.idx_call_wall: Dict[str, float] = {}
        self.idx_chain_ts: Dict[str, float] = {}

        # counters
        self.tick_count = 0
        self.spot_ticks = 0
        self.futures_ticks = 0
        self.option_ticks = 0
        self.hw_ticks = 0

    # ── derived reads (all GIL-atomic) ───────────────────────────────────────
    @property
    def atm(self) -> float:
        s = self.spot
        return round(s / config.STRIKE_STEP) * config.STRIKE_STEP if s > 0 else 0.0

    def freeze_core(self) -> tuple:
        """Single consistent read of (spot, futures, atm, ce_ltp, pe_ltp) —
        run7 lesson: deriving ATM twice mid-read tears on fast tape."""
        spot = self.spot
        atm = round(spot / config.STRIKE_STEP) * config.STRIKE_STEP if spot > 0 else 0.0
        fut = self.futures if self.futures > 0 else spot
        return (spot, fut, atm,
                self.opt_ltp.get((atm, "call"), 0.0),
                self.opt_ltp.get((atm, "put"), 0.0))

    def option_price(self, strike: float, right: str) -> float:
        return self.opt_ltp.get((strike, right), 0.0)

    def option_age(self, strike: float, right: str) -> float:
        ts = self.opt_ts.get((strike, right), 0.0)
        return clk.mono() - ts if ts > 0 else 9e9

    def atm_option_age(self) -> float:
        """Staleness of the ATM call/put feed (the two legs the trader actually
        buys). feed_alive() deliberately watches only spot/futures (the 06-15
        freeze was a spot-tick loss), so the OPTION feed could silently stall —
        prices frozen on the dashboard — with spot still ticking. This is the
        missing liveness probe for that case. Returns the WORSE (older) of the
        two ATM legs; 9e9 until both have been seen at least once.

        DRIFT GUARD: when spot crosses to a new ATM strike that the WS hasn't
        quoted yet (normal 1-2 tick subscription lag on a fast move), the naive
        ATM read returns 9e9 and FALSE-alarms the dashboard 'OPT FEED STALE' even
        though the feed is alive. So if the exact ATM legs aren't both quoted yet,
        fall back to the freshest call/put within +-2 strikes: any fresh near-ATM
        leg means the option feed is flowing."""
        s = self.spot
        if s <= 0:
            return 9e9
        atm = round(s / config.STRIKE_STEP) * config.STRIKE_STEP
        direct = max(self.option_age(atm, "call"), self.option_age(atm, "put"))
        if direct < 9e8:                     # both ATM legs quoted → trust it
            return direct
        best = 9e9                           # ATM not quoted yet — freshest nearby
        for off in range(-2, 3):
            k = atm + off * config.STRIKE_STEP
            best = min(best, self.option_age(k, "call"), self.option_age(k, "put"))
        return best

    def option_feed_alive(self, max_age: float = 45.0) -> bool:
        return self.atm_option_age() <= max_age

    def snapshot_strikes(self, n: int = config.NUM_STRIKES,
                         atm_override: float = 0.0) -> dict:
        """Consistent multi-field read of the nearby chain (the one locked
        path, called ~1/sec from analytics only)."""
        with self._snap_lock:
            atm = atm_override or self.atm
            if atm <= 0:
                return {}
            out = {}
            for off in range(-n, n + 1):
                k = atm + off * config.STRIKE_STEP
                for right in ("call", "put"):
                    key = (k, right)
                    ltp = self.opt_ltp.get(key, 0.0)
                    if ltp <= 0:
                        continue
                    out[key] = {
                        "ltp": ltp,
                        "oi": self.opt_oi.get(key) or self.chain_oi.get(key, 0.0),
                        "vol": self.opt_vol.get(key, 0.0),
                        "bid": self.opt_bid.get(key, 0.0),
                        "ask": self.opt_ask.get(key, 0.0),
                    }
            return out

    def merged_oi(self) -> Dict[Tuple[float, str], float]:
        """WS OI (fresh, near ATM) overlaid on REST chain OI (wide).
        The WS thread may add a key mid-iteration (RuntimeError) — retry once;
        a missed cycle is harmless."""
        for _ in range(2):
            try:
                out = dict(self.chain_oi)
                out.update({k: v for k, v in self.opt_oi.items() if v > 0})
                return out
            except RuntimeError:
                continue
        return dict(self.chain_oi)

    # ── writers (WS callback thread ONLY) ────────────────────────────────────
    def _write_spot(self, ltp: float):
        self.spot = ltp
        self.spot_ts = clk.mono()
        self.spot_ticks += 1
        self.tick_count += 1
        NEW_TICK_EVENT.set()

    def _write_futures(self, ltp: float, qty: float, bid: float, ask: float,
                       oi: float = 0.0):
        self.futures = ltp
        self.futures_ts = clk.mono()
        if oi > 0:
            self.futures_oi = oi
        # detect (never hide) buffer overflow — a full deque drops the oldest
        if len(self.fut_ticks) >= self.fut_ticks.maxlen:
            self.fut_ticks_dropped += 1
        self.fut_ticks.append((ltp, qty, bid, ask, oi))
        self.futures_ticks += 1
        self.tick_count += 1
        NEW_TICK_EVENT.set()

    def _write_option(self, strike: float, right: str, ltp: float,
                      oi: float, vol: float, bid: float, ask: float,
                      bqty: float = 0.0, aqty: float = 0.0):
        key = (strike, right)
        if ltp > 0:
            self.opt_ltp[key] = ltp
            self.opt_ts[key] = clk.mono()
        if oi > 0:
            self.opt_oi[key] = oi
        if vol > 0:
            self.opt_vol[key] = vol
        if bid > 0:
            self.opt_bid[key] = bid
        if ask > 0:
            self.opt_ask[key] = ask
        if bqty > 0:
            self.opt_bqty[key] = bqty
        if aqty > 0:
            self.opt_aqty[key] = aqty
        self.option_ticks += 1
        self.tick_count += 1
        NEW_TICK_EVENT.set()

    def _write_hw(self, symbol: str, ltp: float, vol: float = 0.0,
                  bqty: float = 0.0, aqty: float = 0.0):
        self.hw_ltp[symbol] = ltp
        self.hw_ts[symbol] = clk.mono()
        if vol > 0:
            self.hw_vol[symbol] = vol
        if bqty > 0:
            self.hw_bqty[symbol] = bqty
        if aqty > 0:
            self.hw_aqty[symbol] = aqty
        self.hw_ticks += 1
        self.tick_count += 1


# ═════════════════════════════════════════════════════════════════════════════
# FEED MANAGER — connect, subscribe, route, reconnect
# ═════════════════════════════════════════════════════════════════════════════
class BreezeFeed:
    RECONNECT_WAIT = 5.0
    MAX_RECONNECT = 30

    def __init__(self, prices: PriceStore):
        self.prices = prices
        self.breeze = None
        self.connected = False
        self._reconnect_needed = False
        self._reconnect_attempts = 0
        self._subscribed: set = set()
        self._sub_lock = threading.Lock()
        self._token_to_symbol: Dict[str, str] = {}   # equity token -> NSE symbol
        self._hw_isec: Dict[str, str] = {}           # NSE symbol -> ISEC code
        self._spot_token: str = ""                    # NIFTY index token (robust routing)
        self.expiry = config.expiry_date()
        self.fut_expiry = config.futures_expiry_date()
        self.rest_calls = 0
        self._rest_lock = threading.Lock()

    # ── session ──────────────────────────────────────────────────────────────
    def login(self):
        from breeze_connect import BreezeConnect
        from . import credentials
        self.breeze = BreezeConnect(api_key=credentials.API_KEY)
        resp = self.breeze.generate_session(
            api_secret=credentials.API_SECRET,
            session_token=credentials.SESSION_KEY)
        log.info("generate_session: %s", resp)
        return resp

    def _count_rest(self):
        with self._rest_lock:
            self.rest_calls += 1

    # ── instrument resolution ────────────────────────────────────────────────
    def resolve_heavyweights(self):
        """NSE symbol → (ISEC code, token) via offline CSV in breeze_connect."""
        for sym in config.HEAVYWEIGHTS:
            try:
                r = self.breeze.get_names(exchange_code="NSE", stock_code=sym)
                if isinstance(r, dict) and r.get("isec_stock_code"):
                    self._hw_isec[sym] = r["isec_stock_code"]
                    token = str(r.get("isec_token", "")).strip()
                    if token:
                        self._token_to_symbol[token] = sym
                else:
                    log.warning("get_names failed for %s: %s", sym, r)
            except Exception as e:
                log.warning("get_names error for %s: %s", sym, e)
        log.info("Resolved %d/%d heavyweights", len(self._hw_isec),
                 len(config.HEAVYWEIGHTS))
        # resolve the NIFTY index token so spot can be routed by TOKEN, not
        # by a fragile stock_name string match (live-readiness hardening)
        try:
            r = self.breeze.get_names(exchange_code="NSE",
                                      stock_code=config.STOCK_CODE)
            if isinstance(r, dict):
                self._spot_token = str(r.get("isec_token", "")).strip()
                log.info("NIFTY spot token resolved: %s (stock_name fragment "
                         "fallback: '%s')", self._spot_token or "—",
                         config.SPOT_FRAGMENT)
        except Exception as e:
            log.warning("NIFTY token resolution failed (will rely on "
                        "stock_name fragment): %s", e)

    # ── websocket ────────────────────────────────────────────────────────────
    def connect_ws(self, max_retries: int = 5, base_wait: float = 3.0):
        prices = self.prices
        token_map = self._token_to_symbol
        spot_frag = config.SPOT_FRAGMENT
        spot_token = self._spot_token

        def on_ticks(t):
            """HOT PATH — plain dict writes only, never raises.
            Runs SYNCHRONOUSLY on the breeze socketio receiver thread, so it
            must stay microsecond-cheap: only dict assignments + one deque
            append, no locks, no logging, no allocation beyond the deque."""
            if not isinstance(t, dict):
                return
            try:
                ltp = float(t.get("last") or 0)
                if ltp <= 0:
                    return
                ex = t.get("exchange", "")
                if ex == "NSE Equity":
                    sym_field = t.get("symbol") or ""
                    token = sym_field.split("!")[-1] if "!" in sym_field else ""
                    # spot routing by TOKEN first (robust), stock_name fallback
                    if spot_token and token == spot_token:
                        prices._write_spot(ltp)
                        return
                    hw_sym = token_map.get(token)
                    if hw_sym:
                        prices._write_hw(hw_sym, ltp,
                                         float(t.get("ttq") or 0),
                                         float(t.get("bQty") or 0),
                                         float(t.get("sQty") or 0))
                        return
                    name = str(t.get("stock_name", "")).upper()
                    if spot_frag in name:
                        prices._write_spot(ltp)
                elif ex == "NSE Futures & Options":
                    prod = (t.get("product_type") or "").strip().lower()
                    if prod == "futures":
                        qty = float(t.get("ltq") or 0)
                        bid = float(t.get("bPrice") or 0)
                        ask = float(t.get("sPrice") or 0)
                        foi = float(t.get("OI") or 0)
                        prices.fut_bqty = float(t.get("bQty") or 0) or prices.fut_bqty
                        prices.fut_aqty = float(t.get("sQty") or 0) or prices.fut_aqty
                        prices._write_futures(ltp, qty, bid, ask, foi)
                    elif prod == "options":
                        sr = t.get("strike_price", "")
                        rr = (t.get("right") or "").strip().lower()
                        if sr and rr in ("call", "put"):
                            strike = float(sr)
                            if strike > 0:
                                prices._write_option(
                                    strike, rr, ltp,
                                    float(t.get("OI") or 0),
                                    float(t.get("ttq") or 0),
                                    float(t.get("bPrice") or 0),
                                    float(t.get("sPrice") or 0),
                                    float(t.get("bQty") or 0),
                                    float(t.get("sQty") or 0))
            except Exception:
                pass  # hot path must never crash the socket thread

        self.breeze.on_ticks = on_ticks

        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                self.breeze.ws_connect()
                self.connected = True
                log.info("WS connected (attempt %d)", attempt)
                return
            except Exception as e:
                last_err = e
                wait = base_wait * (2 ** (attempt - 1))
                log.warning("WS connect failed (%d/%d): %s — retry in %.0fs",
                            attempt, max_retries, e, wait)
                clk.sleep(wait)
        raise ConnectionError(
            f"WebSocket failed after {max_retries} attempts: {last_err}. "
            f"Check session key freshness, market hours, network.")

    # ── subscriptions ────────────────────────────────────────────────────────
    def subscribe_index(self):
        try:
            self.breeze.subscribe_feeds(
                stock_code=config.STOCK_CODE, exchange_code="NSE",
                product_type="cash", get_exchange_quotes=True,
                get_market_depth=False)
        except Exception as e:
            log.error("subscribe spot failed: %s", e)
        try:
            self.breeze.subscribe_feeds(
                stock_code=config.STOCK_CODE, exchange_code="NFO",
                product_type="futures",
                expiry_date=config.ws_expiry(self.fut_expiry),
                get_exchange_quotes=True, get_market_depth=False)
        except Exception as e:
            log.error("subscribe futures failed: %s", e)

    @staticmethod
    def _sub_ok(resp) -> bool:
        """breeze subscribe_feeds returns a success dict or an 'Exception …'
        STRING on failure (it catches internally, never raises). Treat any
        string mentioning an exception/error as failure."""
        if resp is None:
            return False
        s = str(resp).lower()
        return not ("exception" in s or "error" in s or "invalid" in s
                    or "not exist" in s)

    def subscribe_strikes(self, atm: float, n: int = config.NUM_STRIKES) -> int:
        added = 0
        exp = config.ws_expiry(self.expiry)
        for off in range(-n, n + 1):
            strike = atm + off * config.STRIKE_STEP
            for right in ("call", "put"):
                key = (strike, right)
                with self._sub_lock:
                    if key in self._subscribed:
                        continue
                try:
                    r = self.breeze.subscribe_feeds(
                        stock_code=config.STOCK_CODE, exchange_code="NFO",
                        product_type="options", expiry_date=exp,
                        strike_price=str(int(strike)), right=right,
                        get_exchange_quotes=True, get_market_depth=False)
                    # the library RETURNS an error string instead of raising;
                    # only mark subscribed on real success, else leave it out
                    # so this strike is retried next pass (audit finding)
                    if self._sub_ok(r):
                        with self._sub_lock:
                            self._subscribed.add(key)
                        added += 1
                    else:
                        log.debug("subscribe %s %s rejected: %s",
                                  strike, right, str(r)[:80])
                except Exception as e:
                    log.debug("subscribe %s %s failed: %s", strike, right, e)
        if added:
            log.info("subscribe_strikes: ATM=%s +%d (total %d)",
                     atm, added, len(self._subscribed))
        return added

    def subscribe_heavyweights(self):
        for sym, isec in self._hw_isec.items():
            try:
                self.breeze.subscribe_feeds(
                    stock_code=isec, exchange_code="NSE", product_type="cash",
                    get_exchange_quotes=True, get_market_depth=False)
            except Exception as e:
                log.warning("subscribe HW %s(%s) failed: %s", sym, isec, e)

    def maintain_subscriptions(self):
        """Called periodically: follow ATM drift with fresh strike subs."""
        atm = self.prices.atm
        if atm > 0:
            self.subscribe_strikes(atm)

    def _resubscribe_strike(self, strike: float, right: str):
        """Subscribe one (strike, right) — used to replay the wide band on
        reconnect so a held-position strike is never dropped."""
        try:
            self.breeze.subscribe_feeds(
                stock_code=config.STOCK_CODE, exchange_code="NFO",
                product_type="options", expiry_date=config.ws_expiry(self.expiry),
                strike_price=str(int(strike)), right=right,
                get_exchange_quotes=True, get_market_depth=False)
            with self._sub_lock:
                self._subscribed.add((strike, right))
        except Exception as e:
            log.debug("resubscribe %s %s failed: %s", strike, right, e)

    # ── reconnect ────────────────────────────────────────────────────────────
    def flag_reconnect(self):
        self._reconnect_needed = True

    def feed_alive(self, max_age: float = 30.0) -> bool:
        ts = max(self.prices.spot_ts, self.prices.futures_ts)
        return ts > 0 and (clk.mono() - ts) < max_age

    @staticmethod
    def _market_hours() -> bool:
        from datetime import datetime
        now = datetime.now(config.IST)
        mins = now.hour * 60 + now.minute
        return (9 * 60 + 14) <= mins <= (15 * 60 + 31) and now.weekday() < 5

    def reconnect_if_needed(self):
        # outside market hours a silent feed is EXPECTED, not a failure —
        # without this gate the 10 s maintenance loop burned all 30 reconnect
        # attempts before 09:15 and left the socket dead for the whole day
        # (teardown finding #1)
        if not self._market_hours():
            self._reconnect_attempts = 0
            self._reconnect_needed = False
            return
        if not self._reconnect_needed and self.feed_alive():
            self._reconnect_attempts = 0     # healthy feed clears the budget
            return
        if self._reconnect_attempts >= self.MAX_RECONNECT:
            log.critical("Max reconnect attempts (%d) reached", self.MAX_RECONNECT)
            return
        self._reconnect_needed = False
        self._reconnect_attempts += 1
        log.warning("Reconnecting WS (%d/%d)…",
                    self._reconnect_attempts, self.MAX_RECONNECT)
        try:
            try:
                self.breeze.ws_disconnect()
            except Exception:
                pass
            # snapshot the WIDE accumulated band BEFORE clearing — a held
            # option whose strike drifted >8 strikes from ATM during the
            # session must be re-subscribed, or its quote freezes and the
            # trader dumps the position on a stale price (audit finding)
            with self._sub_lock:
                prev = set(self._subscribed)
                self._subscribed.clear()
            clk.sleep(self.RECONNECT_WAIT)
            self.connect_ws(max_retries=3, base_wait=2.0)
            self.subscribe_index()
            self.subscribe_heavyweights()
            atm = self.prices.atm
            if atm > 0:
                self.subscribe_strikes(atm)
            # replay every strike that was live before the drop (union with
            # the fresh ATM band already subscribed above)
            for strike, right in prev:
                if (strike, right) not in self._subscribed:
                    self._resubscribe_strike(strike, right)
            log.info("Reconnect resubscribed %d strikes (incl. %d from prior "
                     "band)", len(self._subscribed), len(prev))
            self._reconnect_attempts = 0
            log.info("Reconnect OK — all feeds resubscribed")
        except Exception as e:
            log.error("Reconnect failed: %s", e)
            self._reconnect_needed = True

    # ── REST helpers ─────────────────────────────────────────────────────────
    def fetch_spot_bootstrap(self) -> float:
        """Seed the spot before the first WS tick (and ATM for subscriptions)."""
        try:
            self._count_rest()
            r = self.breeze.get_quotes(stock_code=config.STOCK_CODE,
                                       exchange_code="NSE", product_type="cash",
                                       expiry_date="", right="", strike_price="")
            rows = (r or {}).get("Success") or []
            if rows:
                ltp = float(rows[0].get("ltp") or 0)
                if ltp > 0:
                    self.prices._write_spot(ltp)
                    return ltp
        except Exception as e:
            log.warning("spot bootstrap failed: %s", e)
        return 0.0

    def fetch_vix(self) -> float:
        try:
            self._count_rest()
            r = self.breeze.get_quotes(stock_code="INDVIX",
                                       exchange_code="NSE", product_type="cash",
                                       expiry_date="", right="", strike_price="")
            rows = (r or {}).get("Success") or []
            if rows:
                v = float(rows[0].get("ltp") or 0)
                if 0 < v < 5.0:          # decimal-shifted reading (run7 lesson)
                    v *= 100.0
                if 5.0 <= v <= 90.0:
                    self.prices.vix = v
                    return v
        except Exception as e:
            log.debug("VIX fetch failed: %s", e)
        return 0.0

    def fetch_nifty_chain(self) -> Dict[Tuple[float, str], dict]:
        """Full Nifty weekly chain (both rights) via REST. Returns
        {(strike, right): {ltp, oi, vol}} and refreshes PriceStore.chain_oi."""
        out: Dict[Tuple[float, str], dict] = {}
        exp = config.rest_expiry(self.expiry)
        for right in ("call", "put"):
            try:
                self._count_rest()
                r = self.breeze.get_option_chain_quotes(
                    stock_code=config.STOCK_CODE, exchange_code="NFO",
                    product_type="options", expiry_date=exp, right=right,
                    strike_price="")
                for row in (r or {}).get("Success") or []:
                    try:
                        k = float(row.get("strike_price") or 0)
                        if k <= 0:
                            continue
                        # 'open_interest' is the production-validated OI field
                        # (run7). Volume field name varies across Breeze
                        # versions — try the known aliases, default 0 (the OI
                        # radar's option volume comes from WS ttq anyway).
                        vol = (row.get("total_quantity_traded")
                               or row.get("volume")
                               or row.get("ttq") or 0)
                        out[(k, right)] = {
                            "ltp": float(row.get("ltp") or 0),
                            "oi": float(row.get("open_interest")
                                        or row.get("OI") or 0),
                            "vol": float(vol),
                        }
                    except (TypeError, ValueError):
                        continue
            except Exception as e:
                # Transient Breeze blips (ConnectionReset 10054, timeouts) recover
                # on the next ~90s poll — log them at INFO with a rolling streak so
                # they don't alarm the operator or bloat the log; escalate to
                # WARNING only when the streak shows a REAL outage (>5 in a row).
                msg = str(e)[:120]
                self._chain_fail_streak = getattr(self, "_chain_fail_streak", 0) + 1
                transient = any(s in str(e) for s in (
                    "ConnectionReset", "Connection aborted", "10054", "timed out",
                    "Read timed out", "RemoteDisconnected", "Max retries"))
                if transient and self._chain_fail_streak <= 5:
                    log.info("nifty chain (%s) transient blip #%d (recovers next "
                             "poll): %s", right, self._chain_fail_streak, msg)
                else:
                    log.warning("nifty chain (%s) failed (streak %d): %s",
                                right, self._chain_fail_streak, msg)
        if out:
            self.prices.chain_oi = {k: d["oi"] for k, d in out.items()
                                    if d["oi"] > 0}
            self.prices.chain_ts = clk.mono()
            self._chain_fail_streak = 0          # recovered — reset the streak
        return out

    def fetch_stock_chain(self, sym: str) -> Tuple[List[dict], List[dict]]:
        """Heavyweight monthly option chain rows (ce_rows, pe_rows)."""
        isec = self._hw_isec.get(sym)
        if not isec:
            return [], []
        exp = config.rest_expiry(self.fut_expiry)   # stock monthly = same last-Tuesday cycle
        rows = {"call": [], "put": []}
        for right in ("call", "put"):
            try:
                self._count_rest()
                r = self.breeze.get_option_chain_quotes(
                    stock_code=isec, exchange_code="NFO",
                    product_type="options", expiry_date=exp, right=right,
                    strike_price="")
                rows[right] = (r or {}).get("Success") or []
            except Exception as e:
                log.debug("HW chain %s %s failed: %s", sym, right, e)
        return rows["call"], rows["put"]

    def fetch_sister_chain(self, name: str):
        """BankNifty/FinNifty option-chain PCR + walls (task #37, sentiment/record
        only — NEVER traded). Mirrors fetch_stock_chain. Writes nothing on failure
        so a wrong code/expiry degrades gracefully. PROBE the expiry + NFO code
        against a live Breeze response before enabling SISTER_CHAIN_ON."""
        code = getattr(self, "_idx_codes", {}).get(name)
        if not code:
            return
        exp = config.rest_expiry(self.fut_expiry)   # sisters are MONTHLY in 2026
        ce_rows, pe_rows = [], []
        for right in ("call", "put"):
            try:
                self._count_rest()
                r = self.breeze.get_option_chain_quotes(
                    stock_code=code, exchange_code="NFO",
                    product_type="options", expiry_date=exp, right=right,
                    strike_price="")
                rows = (r or {}).get("Success") or []
                if right == "call":
                    ce_rows = rows
                else:
                    pe_rows = rows
            except Exception as e:
                log.debug("sister chain %s %s failed: %s", name, right, e)

        def agg(rows):
            tot, walls = 0.0, {}
            for r in rows or []:
                try:
                    k = float(r.get("strike_price") or 0)
                    oi = float(r.get("open_interest") or r.get("OI") or 0)
                    if k > 0 and oi > 0:
                        tot += oi
                        walls[k] = walls.get(k, 0.0) + oi
                except (TypeError, ValueError):
                    continue
            return tot, walls

        tot_ce, ce_w = agg(ce_rows)
        tot_pe, pe_w = agg(pe_rows)
        ltp = self.prices.idx_ltp.get(name, 0.0)
        if tot_ce > 0:
            self.prices.idx_pcr[name] = tot_pe / tot_ce
        if pe_w and ltp > 0:
            below = {k: v for k, v in pe_w.items() if k <= ltp}
            if below:
                self.prices.idx_put_wall[name] = max(below, key=below.get)
        if ce_w and ltp > 0:
            above = {k: v for k, v in ce_w.items() if k >= ltp}
            if above:
                self.prices.idx_call_wall[name] = max(above, key=above.get)
        if tot_ce > 0 or tot_pe > 0:
            self.prices.idx_chain_ts[name] = clk.mono()

    def resolve_sentiment_indices(self):
        """Find the working Breeze code for each sister index by simply asking
        the API — candidates from config, first that answers wins."""
        self._idx_codes: Dict[str, str] = {}
        for name, candidates in config.SENTIMENT_INDICES.items():
            for code in candidates:
                try:
                    self._count_rest()
                    r = self.breeze.get_quotes(stock_code=code,
                                               exchange_code="NSE",
                                               product_type="cash",
                                               expiry_date="", right="",
                                               strike_price="")
                    rows = (r or {}).get("Success") or []
                    if rows and float(rows[0].get("ltp") or 0) > 1000:
                        self._idx_codes[name] = code
                        self.prices.idx_ltp[name] = float(rows[0]["ltp"])
                        self.prices.idx_prev[name] = \
                            float(rows[0].get("previous_close") or 0)
                        self.prices.idx_ts[name] = clk.mono()
                        log.info("Sister index %s resolved to code %s "
                                 "(ltp %.1f)", name, code,
                                 self.prices.idx_ltp[name])
                        break
                except Exception:
                    continue
            else:
                log.warning("Sister index %s: no working code among %s — "
                            "sentiment degrades gracefully", name, candidates)

    def poll_sentiment_indices(self):
        # self-heal: if resolution failed at startup (API blip), retry every
        # ~5 min instead of staying blind all day
        if not getattr(self, "_idx_codes", {}):
            if clk.mono() - getattr(self, "_idx_retry_ts", 0.0) > 300:
                self._idx_retry_ts = clk.mono()
                self.resolve_sentiment_indices()
        for name, code in getattr(self, "_idx_codes", {}).items():
            try:
                self._count_rest()
                r = self.breeze.get_quotes(stock_code=code,
                                           exchange_code="NSE",
                                           product_type="cash",
                                           expiry_date="", right="",
                                           strike_price="")
                rows = (r or {}).get("Success") or []
                if rows:
                    ltp = float(rows[0].get("ltp") or 0)
                    if ltp > 0:
                        self.prices.idx_ltp[name] = ltp
                        self.prices.idx_ts[name] = clk.mono()
            except Exception as e:
                log.debug("sister index poll %s failed: %s", name, e)

    def fetch_stock_quote(self, sym: str) -> Tuple[float, float]:
        """(ltp, previous_close) for a heavyweight — startup bootstrap."""
        isec = self._hw_isec.get(sym)
        if not isec:
            return 0.0, 0.0
        try:
            self._count_rest()
            r = self.breeze.get_quotes(stock_code=isec, exchange_code="NSE",
                                       product_type="cash", expiry_date="",
                                       right="", strike_price="")
            rows = (r or {}).get("Success") or []
            if rows:
                return (float(rows[0].get("ltp") or 0),
                        float(rows[0].get("previous_close") or 0))
        except Exception as e:
            log.debug("HW quote %s failed: %s", sym, e)
        return 0.0, 0.0
