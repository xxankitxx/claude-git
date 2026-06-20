#!/usr/bin/env python3
"""
MYTHOS — Monday-morning preflight: tests the ENTIRE live Breeze path and
prints GO / NO-GO before you risk the trading day on it.

    python preflight.py          (run at ~08:55 IST after pasting the day's
                                  SESSION_KEY into mythos/credentials.py)

Checks, in order:
  1. credentials + session  (generate_session)
  2. Nifty spot quote       (REST get_quotes)
  3. India VIX              (REST get_quotes INDVIX)
  4. Weekly option chain    (REST get_option_chain_quotes, both rights)
  5. Heavyweight resolution (get_names for all 14 stocks)
  6. One heavyweight quote  (RELIANCE ltp + previous_close)
  7. Websocket connect + spot subscription (5 s tick wait; before 09:15
     ticks may be zero — that alone is not a failure pre-open)

Every step prints PASS/FAIL with the exact error. Run it any time —
it never places orders and never writes to MYTHOS data files.
"""

import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [warn]"


def main() -> int:
    failures = 0
    print("\n  MYTHOS PREFLIGHT — live Breeze path check\n  " + "─" * 50)

    # 0. local config sanity (no network) — paper-only + dashboard port free.
    #    A stale MYTHOS holding port 8765 makes run_mythos crash on bind at the
    #    09:14 window (the "won't load" trap); catch it now, before the session.
    try:
        import socket
        from mythos import config as _c0
        if getattr(_c0, "LIVE_ORDERS", False):
            failures += 1
            print(f"{FAIL} 0. LIVE_ORDERS is True — MYTHOS must be PAPER-ONLY. "
                  f"Set config.LIVE_ORDERS=False.")
        else:
            print(f"{PASS} 0. Paper-only (LIVE_ORDERS=False)")
        if getattr(_c0, "LIVE_ENTRY_CURE", False):
            print(f"{WARN} 0. ENTRY CURE #5 ACTIVE — ZONE_BAND={_c0.ZONE_BAND:.0f} / "
                  f"TURN_CONFIRM={_c0.TURN_CONFIRM_PTS:.0f} (structural-purity, PAPER "
                  f"validation of an UNPROVEN edge). Disable: LIVE_ENTRY_CURE=False.")
        else:
            print(f"{PASS} 0. Entry cure OFF — baseline entry logic")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((_c0.HOST, _c0.PORT))
            print(f"{PASS} 0. Dashboard port {_c0.PORT} is free")
        except OSError:
            failures += 1
            print(f"{FAIL} 0. Port {_c0.PORT} in use — a previous MYTHOS is still "
                  f"running. Stop it (Ctrl+C) before launching.")
        finally:
            s.close()
    except Exception as e:
        print(f"{WARN} 0. Config sanity skipped: {e}")

    # 1. session
    try:
        from breeze_connect import BreezeConnect
        from mythos import config, credentials
        breeze = BreezeConnect(api_key=credentials.API_KEY)
        r = breeze.generate_session(api_secret=credentials.API_SECRET,
                                    session_token=credentials.SESSION_KEY)
        print(f"{PASS} 1. Session established (key {credentials.SESSION_KEY})")
    except Exception as e:
        print(f"{FAIL} 1. Session: {e}")
        print("        → Refresh SESSION_KEY in mythos/credentials.py "
              "(see README, takes 60 seconds) and re-run.")
        return 1

    # 2. spot
    spot = 0.0
    try:
        r = breeze.get_quotes(stock_code="NIFTY", exchange_code="NSE",
                              product_type="cash", expiry_date="", right="",
                              strike_price="")
        rows = (r or {}).get("Success") or []
        spot = float(rows[0].get("ltp") or 0) if rows else 0.0
        if spot > 1000:
            print(f"{PASS} 2. Nifty spot quote: {spot:.2f}")
        else:
            failures += 1
            print(f"{FAIL} 2. Nifty spot quote empty: {str(r)[:120]}")
    except Exception as e:
        failures += 1
        print(f"{FAIL} 2. Nifty spot: {e}")

    # 3. VIX
    try:
        r = breeze.get_quotes(stock_code="INDVIX", exchange_code="NSE",
                              product_type="cash", expiry_date="", right="",
                              strike_price="")
        rows = (r or {}).get("Success") or []
        vix = float(rows[0].get("ltp") or 0) if rows else 0.0
        if 0 < vix < 5:
            vix *= 100
        if 5 <= vix <= 90:
            print(f"{PASS} 3. India VIX: {vix:.2f}")
        else:
            print(f"{WARN} 3. VIX implausible ({vix}) — system will use "
                  f"straddle proxy, not fatal")
    except Exception as e:
        print(f"{WARN} 3. VIX: {e} — proxy fallback will engage, not fatal")

    # 4. weekly chain
    try:
        exp = config.rest_expiry(config.expiry_date())
        ok = 0
        for right in ("call", "put"):
            r = breeze.get_option_chain_quotes(
                stock_code="NIFTY", exchange_code="NFO",
                product_type="options", expiry_date=exp, right=right,
                strike_price="")
            rows = (r or {}).get("Success") or []
            good = sum(1 for it in rows
                       if float(it.get("strike_price") or 0) > 0
                       and float(it.get("open_interest") or 0) > 0)
            ok += good
        if ok > 20:
            print(f"{PASS} 4. Weekly chain {config.expiry_date()}: "
                  f"{ok} strikes with OI")
        else:
            failures += 1
            print(f"{FAIL} 4. Chain for {config.expiry_date()} returned only "
                  f"{ok} usable rows.")
            print("        → If a holiday moved the expiry, set "
                  "EXPIRY_OVERRIDE in mythos/config.py")
    except Exception as e:
        failures += 1
        print(f"{FAIL} 4. Weekly chain: {e}")

    # 5. heavyweight resolution
    try:
        resolved, missing = [], []
        for sym in config.HEAVYWEIGHTS:
            try:
                rr = breeze.get_names(exchange_code="NSE", stock_code=sym)
                if isinstance(rr, dict) and rr.get("isec_stock_code"):
                    resolved.append(sym)
                else:
                    missing.append(sym)
            except Exception:
                missing.append(sym)
        if not missing:
            print(f"{PASS} 5. Heavyweights resolved: all {len(resolved)}")
        else:
            print(f"{WARN} 5. Heavyweights: {len(resolved)} ok, "
                  f"missing {missing} — basket degrades gracefully, not fatal")
    except Exception as e:
        print(f"{WARN} 5. Heavyweight resolution: {e}")

    # 6. heavyweight quote
    try:
        rr = breeze.get_names(exchange_code="NSE", stock_code="RELIANCE")
        isec = rr.get("isec_stock_code", "RELIND")
        r = breeze.get_quotes(stock_code=isec, exchange_code="NSE",
                              product_type="cash", expiry_date="", right="",
                              strike_price="")
        rows = (r or {}).get("Success") or []
        ltp = float(rows[0].get("ltp") or 0) if rows else 0.0
        prev = float(rows[0].get("previous_close") or 0) if rows else 0.0
        if ltp > 0:
            tag = f"prev_close {prev:.1f}" if prev > 0 else \
                  "previous_close field empty — %change degrades, not fatal"
            print(f"{PASS} 6. RELIANCE quote: {ltp:.2f} ({tag})")
        else:
            print(f"{WARN} 6. RELIANCE quote empty — basket %change degrades")
    except Exception as e:
        print(f"{WARN} 6. Heavyweight quote: {e}")

    # 6b. sister indices (BankNifty / FinNifty sentiment inputs)
    try:
        from mythos import config as _cfg
        for name, candidates in _cfg.SENTIMENT_INDICES.items():
            hit = ""
            for code in candidates:
                try:
                    r = breeze.get_quotes(stock_code=code, exchange_code="NSE",
                                          product_type="cash", expiry_date="",
                                          right="", strike_price="")
                    rows = (r or {}).get("Success") or []
                    if rows and float(rows[0].get("ltp") or 0) > 1000:
                        hit = f"{code} (ltp {float(rows[0]['ltp']):.0f})"
                        break
                except Exception:
                    continue
            if hit:
                print(f"{PASS} 6b. {name}: {hit}")
            else:
                print(f"{WARN} 6b. {name}: no working code in {candidates} — "
                      f"sentiment degrades gracefully, not fatal")
    except Exception as e:
        print(f"{WARN} 6b. sister indices: {e}")

    # 6c. NIFTY spot token (spot routing robustness — the #1 live risk)
    try:
        rr = breeze.get_names(exchange_code="NSE", stock_code="NIFTY")
        tok = rr.get("isec_token", "") if isinstance(rr, dict) else ""
        if tok:
            print(f"{PASS} 6c. NIFTY spot token resolved: {tok}")
        else:
            print(f"{WARN} 6c. NIFTY token unresolved — WS spot will rely on "
                  f"stock_name match + REST refresh safety net")
    except Exception as e:
        print(f"{WARN} 6c. NIFTY token: {e}")

    # 7. websocket + a real option-strike subscription (the subscribe path)
    try:
        ticks = {"n": 0, "opt": 0}

        def on_ticks(t):
            ticks["n"] += 1
            if isinstance(t, dict) and "NSE Futures" in str(t.get("exchange", "")):
                ticks["opt"] += 1

        breeze.on_ticks = on_ticks
        breeze.ws_connect()
        breeze.subscribe_feeds(stock_code="NIFTY", exchange_code="NSE",
                               product_type="cash", get_exchange_quotes=True,
                               get_market_depth=False)
        # also exercise the OPTION subscribe path with the live expiry + a
        # near-ATM round strike, so the scrip-master lookup is verified
        try:
            from mythos import config as _cfg
            atm = int(round(spot / 50.0) * 50) if spot > 1000 else 25000
            wexp = _cfg.ws_expiry(_cfg.expiry_date())
            sresp = breeze.subscribe_feeds(
                stock_code="NIFTY", exchange_code="NFO",
                product_type="options", expiry_date=wexp,
                strike_price=str(atm), right="call",
                get_exchange_quotes=True, get_market_depth=False)
            ok_sub = "exception" not in str(sresp).lower() and \
                     "error" not in str(sresp).lower()
            print(f"{PASS if ok_sub else WARN} 7a. Option subscribe "
                  f"({atm}CE {wexp}): {'accepted' if ok_sub else sresp}")
        except Exception as e:
            print(f"{WARN} 7a. Option subscribe: {e}")
        time.sleep(5.0)
        try:
            breeze.ws_disconnect()
        except Exception:
            pass
        if ticks["n"] > 0:
            print(f"{PASS} 7. Websocket: connected, {ticks['n']} ticks "
                  f"({ticks['opt']} F&O) in 5 s")
        else:
            print(f"{WARN} 7. Websocket connected, 0 ticks in 5 s "
                  f"(normal before 09:07 / after 15:30; a FAIL only if "
                  f"market is open)")
    except Exception as e:
        failures += 1
        print(f"{FAIL} 7. Websocket: {e}")

    print("  " + "─" * 50)
    if failures == 0:
        print("  VERDICT: GO — start MYTHOS with:  python run_mythos.py\n")
        return 0
    print(f"  VERDICT: NO-GO — {failures} hard failure(s) above. "
          f"Fix and re-run.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
