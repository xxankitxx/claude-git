#!/usr/bin/env python3
"""
MYTHOS — launcher.

    python run_mythos.py            live trading day (Breeze feeds)
    python run_mythos.py --sim      simulation mode (synthetic market, no API)
    python run_mythos.py --sim2     SIMULATION at 10× speed — a full session in
                                    minutes (identical dynamics, time-warped)

Then open  http://127.0.0.1:8765  in a browser.
"""

import sys

try:  # Windows consoles default to cp1252 — force UTF-8 (run7 lesson)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    sim2 = "--sim2" in sys.argv
    sim = sim2 or ("--sim" in sys.argv)

    from mythos.app import MythosApp, setup_logging
    from mythos import config

    setup_logging()

    speed = config.SIM_SPEED if sim2 else 1.0
    banner = (f"SIM ×{config.SIM_SPEED:g}" if sim2
              else "SIMULATION" if sim else "LIVE")
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print(f"  ║   MYTHOS — Nifty Options Intelligence  [{banner:^10}] ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print(f"   Weekly expiry : {config.expiry_date()}"
          f"{'  (EXPIRY DAY)' if config.is_expiry_day() else ''}")
    print(f"   Futures expiry: {config.futures_expiry_date()}")
    print(f"   Capital       : ₹{config.STARTING_CAPITAL:,.0f} (daily reset)")
    print(f"   Dashboard     : http://{config.HOST}:{config.PORT}")
    print()

    app = MythosApp(sim=sim, speed=speed)
    try:
        app.start()
    except Exception as e:
        print(f"\n  FATAL during startup: {e}")
        if not sim:
            print("  Checklist: 1) fresh SESSION_KEY in mythos/credentials.py"
                  "  2) market hours  3) network")
        raise

    from mythos.server import run_server
    try:
        run_server(app)        # blocks until Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        print("\n  MYTHOS stopped.")


if __name__ == "__main__":
    main()
