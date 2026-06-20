#!/usr/bin/env python3
"""Realized-P&L A/B of leaner entry gates, via replay._run with config overrides.
Evidence only — flips NOTHING (no default changed; this just scores hypothetical
configs on tape). Bridges the probe's per-bar edge to realized trade P&L.

The probe showed: exhaustion-into-zone (RAIL #3, already REQUIRED) carries the
edge, while the 6-of-14 vote tally ON TOP of it selects later, slightly-worse
entries (exhaustion&vote<6 +0.62/bar  >  exhaustion&vote>=6 +0.52/bar). So firing
on FEWER votes (still gated by exhaustion + the other rails) should enter earlier.
Test EVIDENCE_NEED 6(base)->5,4,3 and the high-vote-veto ceiling, per day.

  python realized_ab.py <day>     -> writes edge_out/ab_<day>.json + <day>.abdone

NOTE: SL/profit/sizing untouched (out of scope) — only the entry-confirmation
count varies. Exit doctrine stays +12/-10.
"""
import json
import os
import sys

import replay
from mythos import config

CONFIGS = {
    "baseline":  {},
    "need5":     {"EVIDENCE_NEED": 5},
    "need4":     {"EVIDENCE_NEED": 4},
    "need3":     {"EVIDENCE_NEED": 3},
    "ceil8":     {"ENTRY_SCORE_CEILING": 8},
    "need4+ceil10": {"EVIDENCE_NEED": 4, "ENTRY_SCORE_CEILING": 10},
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_out")


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-06-16"
    # never clobber the real trade file
    config.TRADES_JSON = config.TRADES_JSON + ".ab"

    from mythos.store import Store
    store = Store(config.DB_PATH)
    frames = store.load_frames(day)
    store.stop()
    if not frames:
        print(f"no frames for {day}")
        return 1

    os.makedirs(OUT, exist_ok=True)
    res = {"day": day, "runs": {}}
    for name, ov in CONFIGS.items():
        r = replay._run(frames, ov)
        res["runs"][name] = {
            "trades": r["trades"], "wins": r["wins"], "losses": r["losses"],
            "win_rate": round(r["win_rate"], 1), "net_pts": round(r["pts"], 1),
            "cash": round(r["cash"], 0),
        }
    with open(os.path.join(OUT, f"ab_{day}.json"), "w") as f:
        json.dump(res, f, indent=1)
    open(os.path.join(OUT, f"{day}.abdone"), "w").close()
    print(f"  {day} done: " + " | ".join(
        f"{k} {v['net_pts']:+.0f}p/{v['trades']}t" for k, v in res["runs"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
