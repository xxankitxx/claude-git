#!/usr/bin/env python3
"""Wait for the realized A/B runs, then tabulate net pts / trades / win-rate per
config across days + pooled. Evidence only."""
import glob
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "edge_out")
DAYS = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]
ORDER = ["baseline", "need5", "need4", "need3", "ceil8", "need4+ceil10"]


def main():
    deadline = time.time() + 3000
    while time.time() < deadline:
        if len(glob.glob(os.path.join(OUT, "*.abdone"))) >= len(DAYS):
            break
        time.sleep(10)

    data = {}
    for d in DAYS:
        p = os.path.join(OUT, f"ab_{d}.json")
        if os.path.exists(p):
            data[d] = json.load(open(p))
    if not data:
        print("no A/B results")
        return 1

    print(f"\n  REALIZED ENTRY A/B — net pts (trades, win%) by config\n"
          f"  {len(data)}/{len(DAYS)} days ready\n")
    hdr = "  " + f"{'day':<12}" + "".join(f"{c:>16}" for c in ORDER)
    print(hdr)
    pooled = {c: {"net": 0.0, "trades": 0, "wins": 0, "losses": 0} for c in ORDER}
    for d in DAYS:
        if d not in data:
            continue
        runs = data[d]["runs"]
        cells = []
        for c in ORDER:
            r = runs.get(c)
            if not r:
                cells.append(f"{'—':>16}")
                continue
            cells.append(f"{r['net_pts']:>+7.0f}/{r['trades']:>2}t/{r['win_rate']:>4.0f}%")
            pooled[c]["net"] += r["net_pts"]
            pooled[c]["trades"] += r["trades"]
            pooled[c]["wins"] += r["wins"]
            pooled[c]["losses"] += r["losses"]
        print(f"  {d:<12}" + "".join(cells))

    print("\n  " + "-" * (12 + 16 * len(ORDER)))
    pcells = []
    for c in ORDER:
        p = pooled[c]
        dec = p["wins"] + p["losses"]
        wr = 100 * p["wins"] / dec if dec else 0
        pcells.append(f"{p['net']:>+7.0f}/{p['trades']:>2}t/{wr:>4.0f}%")
    print(f"  {'POOLED':<12}" + "".join(pcells))
    base_net = pooled["baseline"]["net"]
    print(f"\n  pooled net vs baseline ({base_net:+.0f} pts):")
    for c in ORDER:
        if c == "baseline":
            continue
        print(f"    {c:<14} {pooled[c]['net']-base_net:>+7.0f} pts "
              f"({pooled[c]['net']:+.0f} total, {pooled[c]['trades']} trades)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
