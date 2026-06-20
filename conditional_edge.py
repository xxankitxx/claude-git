#!/usr/bin/env python3
"""⚠ CONFOUNDED (teardown 2026-06-20): results here inherit the probe's confounds
(direction-contamination, exhaustion zone-survivor bias, LTP-not-ASK) PLUS a
trend two-pointer with no lookback-staleness tolerance (_trend_flags accepts a
1-2s ref on early/sparse bars instead of the intended 60-300s). The exhaustion/
vote conclusions are RETRACTED until the probe is rebuilt. See entry_edge_probe.py.

Offline conditional entry-edge analysis from the probe's per-bar dumps.
Evidence only — flips nothing. Reads edge_out/bars_<day>.jsonl (one row per bar:
ts, lean CE/PE, spot, label WIN/LOSS/TIMEOUT, pts, sigs{}) and scores candidate
ENTRY FILTERS by forward +12/-10 expectancy, per day and pooled:

  ALL            every leaning bar (baseline)
  VOTE>=6        the live tally gate
  EXHAUST        only bars where seller-exhaustion-into-zone fired (the one
                 cross-day-consistent positive marginal signal)
  TREND          parameter-free causal regime filter: take CE only when spot is
                 above its value ~LOOKBACK_S ago (uptrend), PE only when below
                 (downtrend) — i.e. don't buy against the tape
  EXH+TREND      both filters
  EXHAUST|TREND  either

A filter is interesting only if it RAISES exp_pts/bar AND win-rate vs ALL while
keeping enough bars to matter — and (key for the -143 day) cuts the bleed on the
negative-regime tapes. Per-bar marginal, NOT per-trade P&L (see probe caveats)."""
import glob
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "edge_out")
DAYS = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]
LOOKBACKS_S = [60.0, 180.0, 300.0]      # trend horizons to scan
EXH_KEY = "ev:Exhaustion (fall into zone)"


def _wait_for_dumps(timeout_s=2400):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        done = glob.glob(os.path.join(OUT, "*.done"))
        if len(done) >= len(DAYS):
            return True
        time.sleep(10)
    return False


def _load(day):
    path = os.path.join(OUT, f"bars_{day}.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["ts"])
    return rows


def _trend_flags(rows, lookback_s):
    """For each row, is spot above/below its value ~lookback_s ago? Two-pointer
    over ts-sorted rows. Returns list of +1 (up) / -1 (down) / 0 (unknown)."""
    out = [0] * len(rows)
    j = 0
    for i, r in enumerate(rows):
        t0 = r["ts"] - lookback_s
        while j < i and rows[j]["ts"] < t0:
            j += 1
        # rows[j] is the earliest bar at/after t0; use the one just before it for
        # a value ~lookback ago (fall back to j)
        ref = rows[max(0, j - 1)]
        if ref is r:
            out[i] = 0
            continue
        out[i] = 1 if r["spot"] > ref["spot"] else -1 if r["spot"] < ref["spot"] else 0
    return out


def _stats(rows):
    n = len(rows)
    wins = sum(1 for r in rows if r["label"] == "WIN")
    losses = sum(1 for r in rows if r["label"] == "LOSS")
    dec = wins + losses
    wr = wins / dec if dec else 0.0
    exp = sum(r["pts"] for r in rows) / n if n else 0.0
    return {"n": n, "wr": wr, "exp": exp, "wins": wins, "losses": losses}


def _scenarios(rows, trend):
    def exh(r):
        return bool(r["sigs"].get(EXH_KEY, False))

    def vote(r):
        return any(k.startswith("vote_ok_count") and v for k, v in r["sigs"].items())

    def trend_ok(i, r):
        t = trend[i]
        return (r["lean"] == "CE" and t > 0) or (r["lean"] == "PE" and t < 0)

    sc = {
        "ALL": rows,
        "VOTE>=6": [r for r in rows if vote(r)],
        "EXHAUST": [r for r in rows if exh(r)],
        "TREND": [r for i, r in enumerate(rows) if trend_ok(i, r)],
        "EXH+TREND": [r for i, r in enumerate(rows) if exh(r) and trend_ok(i, r)],
        "EXHAUST|TREND": [r for i, r in enumerate(rows) if exh(r) or trend_ok(i, r)],
    }
    return {k: _stats(v) for k, v in sc.items()}


def main():
    if not _wait_for_dumps():
        have = [os.path.basename(p) for p in glob.glob(os.path.join(OUT, "*.done"))]
        print(f"TIMEOUT — done markers: {have}")
        # continue with whatever dumps exist
    data = {d: _load(d) for d in DAYS}
    data = {d: r for d, r in data.items() if r}
    if not data:
        print("no per-bar dumps found")
        return 1

    for lb in LOOKBACKS_S:
        print(f"\n{'='*78}\n  CONDITIONAL ENTRY EDGE — trend lookback {int(lb)}s\n{'='*78}")
        order = ["ALL", "VOTE>=6", "EXHAUST", "TREND", "EXH+TREND", "EXHAUST|TREND"]
        pooled = {k: {"n": 0, "wins": 0, "losses": 0, "ptsum": 0.0} for k in order}

        for d in DAYS:
            rows = data.get(d)
            if not rows:
                continue
            trend = _trend_flags(rows, lb)
            sc = _scenarios(rows, trend)
            print(f"\n  {d}   (base exp {sc['ALL']['exp']:+.3f}/bar, "
                  f"{sc['ALL']['n']} bars)")
            print(f"    {'filter':<14}{'n':>7}{'win%':>7}{'exp/bar':>9}{'vs ALL':>8}")
            base_exp = sc["ALL"]["exp"]
            for k in order:
                s = sc[k]
                d_exp = s["exp"] - base_exp
                print(f"    {k:<14}{s['n']:>7}{s['wr']*100:>6.1f}%{s['exp']:>+9.3f}"
                      f"{d_exp:>+8.3f}")
                pooled[k]["n"] += s["n"]
                pooled[k]["wins"] += s["wins"]
                pooled[k]["losses"] += s["losses"]
                pooled[k]["ptsum"] += s["exp"] * s["n"]

        print(f"\n  POOLED (all {len(data)} days)")
        print(f"    {'filter':<14}{'n':>8}{'win%':>7}{'exp/bar':>9}{'vs ALL':>8}")
        base_exp = pooled["ALL"]["ptsum"] / pooled["ALL"]["n"] if pooled["ALL"]["n"] else 0.0
        for k in order:
            p = pooled[k]
            exp = p["ptsum"] / p["n"] if p["n"] else 0.0
            dec = p["wins"] + p["losses"]
            wr = p["wins"] / dec if dec else 0.0
            print(f"    {k:<14}{p['n']:>8}{wr*100:>6.1f}%{exp:>+9.3f}"
                  f"{exp-base_exp:>+8.3f}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
