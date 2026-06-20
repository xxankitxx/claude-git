#!/usr/bin/env python3
"""Wait for the 5 per-day entry-edge probe JSONs, then build a cross-day table.
Evidence only — reads /tmp/edge_<day>.json, prints a ranked signal-edge summary."""
import json
import os
import tempfile
import time

DAYS = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]
# bash redirected to MSYS /tmp == %TEMP%; Windows Python's gettempdir() is the
# SAME directory, so read from there (not the literal "/tmp" which Win-Python
# resolves to D:\tmp).
_TMP = tempfile.gettempdir()
PATHS = {d: os.path.join(_TMP, f"edge_{d}.json") for d in DAYS}


def _ready():
    out = {}
    for d, p in PATHS.items():
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return None
        try:
            with open(p) as f:
                out[d] = json.load(f)
        except (json.JSONDecodeError, ValueError):
            return None
    return out


def main():
    # wait up to 30 min for all five runs to finish writing
    data = None
    for _ in range(180):
        data = _ready()
        if data is not None:
            break
        time.sleep(10)
    if data is None:
        # report what we DO have rather than nothing
        have = [d for d in DAYS if os.path.exists(PATHS[d]) and os.path.getsize(PATHS[d]) > 0]
        print(f"TIMEOUT — only {len(have)}/5 ready: {have}")
        data = {}
        for d in have:
            try:
                data[d] = json.load(open(PATHS[d]))
            except Exception as e:
                print(f"  {d}: unreadable ({e})")
    if not data:
        return 1

    days = sorted(data.keys())
    print(f"\n  ENTRY-EDGE — CROSS-DAY SUMMARY ({len(days)} tapes: {', '.join(days)})\n")
    print("  per-day realized engine result (trades / net pts):")
    for d in days:
        r = data[d]
        print(f"    {d}:  {r['trades_actual']:>3} trades   {r['net_pts_actual']:>+8.1f} pts"
              f"   ({r['n_bars']} probed bars)")

    # base rate per day (the always-buy-ATM-leaning exp_pts)
    base = {}
    for d in days:
        for row in data[d]["table"]:
            if row["signal"] == "base_rate":
                base[d] = row["true"]["exp_pts"]
    print("\n  base_rate exp_pts/bar by day:",
          "  ".join(f"{d[-5:]} {base.get(d,0):+.2f}" for d in days))

    # gather every signal's per-day edge + true_n + true_winrate
    sig_rows = {}
    for d in days:
        for row in data[d]["table"]:
            s = row["signal"]
            if s == "base_rate":
                continue
            rec = sig_rows.setdefault(s, {"edge": {}, "tn": {}, "twr": {}})
            rec["edge"][d] = row["edge"]
            rec["tn"][d] = row["true"]["n"]
            rec["twr"][d] = row["true"]["winrate"]

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else 0.0

    summary = []
    for s, rec in sig_rows.items():
        edges = [rec["edge"].get(d) for d in days]
        present = [e for e in edges if e is not None]
        days_pos = sum(1 for e in present if e > 0)
        summary.append({
            "signal": s,
            "mean_edge": mean(present),
            "days_pos": days_pos,
            "days_present": len(present),
            "min_edge": min(present) if present else 0.0,
            "max_edge": max(present) if present else 0.0,
            "mean_tn": mean([rec["tn"].get(d) for d in days]),
            "mean_twr": mean([rec["twr"].get(d) for d in days]),
            "edges": edges,
        })

    summary.sort(key=lambda r: -r["mean_edge"])
    print(f"\n  {'signal':<28}{'mean':>7}{'min':>7}{'max':>7}  {'d+>0':>4} "
          f"{'~n':>7} {'~wr':>6}   per-day edge")
    for r in summary:
        per = " ".join(f"{(e if e is not None else float('nan')):+5.2f}" for e in r["edges"])
        print(f"  {r['signal']:<28}{r['mean_edge']:>+7.2f}{r['min_edge']:>+7.2f}"
              f"{r['max_edge']:>+7.2f}  {r['days_pos']}/{r['days_present']:<2}"
              f"{r['mean_tn']:>7.0f} {r['mean_twr']*100:>5.1f}%   {per}")

    # spotlight the hated tally
    print("\n  SPOTLIGHT — the voting tally vs its anti-predictive claim:")
    for r in summary:
        if r["signal"].startswith("vote_ok_count"):
            consistent = r["days_pos"] == 0
            print(f"    {r['signal']}: mean edge {r['mean_edge']:+.2f} pts, "
                  f"positive on {r['days_pos']}/{r['days_present']} days, "
                  f"mean win-rate {r['mean_twr']*100:.1f}%  "
                  f"-> {'ANTI-PREDICTIVE on every day' if consistent else 'mixed'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
