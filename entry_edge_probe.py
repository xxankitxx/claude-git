#!/usr/bin/env python3
"""
⚠ KNOWN CONFOUNDS (teardown 2026-06-20) — the per-signal EDGE numbers this probe
produced are NOT trustworthy yet; the "exhaustion is the edge / vote dilutes it"
conclusion is RETRACTED. Before re-running, fix all four:
  1. _leaning_side picks CE/PE by ok_count on NEUTRAL bars → the VOTE filter is
     partly tautological. Run a DECISION-DRIVEN variant (only engine-fired bars).
  2. exhaustion_ok is read from signals.py evidence, which is only populated on
     zone-eval bars → EXHAUST bucket is zone-survivor-biased. Compute exhaustion
     INDEPENDENTLY for ALL bars.
  3. entry uses LTP (_opt_ltp v[0]); a buyer pays ASK (v[4]). Use ASK for entry.
  4. (conditional_edge) trend two-pointer lacks a lookback-staleness tolerance.
The engine-drive itself IS faithful (gate reproduces 19 trades / −143.2 on 06-16).
Also: make it FAST (sample 1-in-N bars + vectorize the forward walk) — the full
O(bars×horizon) run is ~30 min/tape and is not to be used as-is.

MYTHOS — ENTRY-SIGNAL EDGE PROBE (evidence only; flips nothing).

Measures the EDGE of each entry signal on REAL recorded tape by labelling the
forward +12/-10 outcome of a hypothetical ATM entry from EVERY bar, then
splitting that outcome population by whether each named signal was TRUE/FALSE on
the leaning side at that bar.

It reuses replay.py's engine drive verbatim (same _patch_clock / _FakeDatetime /
_load_frame / per-bar pump) so the reconstructed engine state is byte-faithful;
the FORWARD-SIM is a SEPARATE, exit-doctrine-independent label read straight off
the recorded option tape (frame opts), not the trader path.

A MANDATORY correctness gate calls replay._run(frames, {}) and confirms the
06-16 BASELINE (19 trades, net -143.2 pts) before any mining is reported — this
proves the engine drive faithfully reconstructs state.

    python entry_edge_probe.py 2026-06-16
    python entry_edge_probe.py 2026-06-16 --json

CAVEATS (see notes in JSON):
  * Entry strike is the ATM proxy K = round(spot/50)*50, right=call(CE)/put(PE);
    this is the doctrine's traded strike (ATM-default) but a real fire could pick
    an OTM strike on strong conviction — so the label is the ATM-entry edge.
  * Forward label uses the recorded LTP of that SAME fixed K at each later bar;
    horizon cap = 1800s of tape; neither +12 nor -10 reached => TIMEOUT (excluded
    from winrate, counted as 0 pts in exp_pts).
"""
import json
import sys

import replay
from replay import _FakeDatetime, _load_frame, _patch_clock, _VCLOCK

# Known baseline for the correctness gate (the engine-drive faithfulness proof).
GATE_DAY = "2026-06-16"
GATE_TRADES = 19
GATE_NET_PTS = -143.2

WIN_PTS = 12.0          # +12 doctrine
LOSS_PTS = -10.0        # -10 doctrine
HORIZON_S = 1800.0      # forward-walk cap in seconds of tape


def _opt_ltp(opts: dict, strike: int, right: str):
    """LTP of a fixed strike from a recorded frame's opts dict, or None.
    opts key is '<intstrike>c'/'<intstrike>p'; value is [ltp,oi,vol,bid,ask,bq,aq].
    """
    key = f"{int(strike)}{'c' if right == 'call' else 'p'}"
    v = opts.get(key)
    if not v:
        return None
    ltp = v[0]
    return ltp if ltp and ltp > 0 else None


def _leaning_side(dec):
    """The side the engine is leaning toward: decision.direction if it is CE/PE,
    else whichever side currently has the higher ok_count (tie -> CE)."""
    if dec.direction in ("CE", "PE"):
        return dec.direction
    return "CE" if dec.ce.ok_count >= dec.pe.ok_count else "PE"


def _record_signals(sig, dec, config):
    """Build the {signal_name: bool} dict for the leaning side at this bar.
    EVERY value is a plain bool so each is independently probeable."""
    d = _leaning_side(dec)
    view = dec.ce if d == "CE" else dec.pe
    sigs = {}

    # the hated vote tally, as a >= threshold predicate (live EVIDENCE_NEED)
    need = int(getattr(config, "EVIDENCE_NEED", 6))
    sigs[f"vote_ok_count>={need}"] = bool(view.ok_count >= need)

    # structural / state signals
    sigs["kind==BREAK"] = bool(view.kind == "BREAK")
    sigs["cross_blocked"] = bool(dec.cross_blocked)

    # each named evidence vote of the leaning side becomes its own signal
    for e in view.evidence:
        sigs[f"ev:{e.name}"] = bool(e.ok)

    # the novel velocity-inflection trigger (call directly, ignore config flags)
    try:
        sigs["vis_inflection"] = bool(sig._vis_inflection(d))
    except Exception:
        sigs["vis_inflection"] = False

    # kinematics of the leaning side
    try:
        ks = sig.kin["spot"]
        ka = sig.kin["ce" if d == "CE" else "pe"]
        sigs["kin_spot_a>0"] = bool(ks.a > 0)
        sigs["kin_spot_v>0"] = bool(ks.v > 0)
        sigs["kin_prem_a>0"] = bool(ka.a > 0)
    except Exception:
        sigs["kin_spot_a>0"] = False
        sigs["kin_spot_v>0"] = False
        sigs["kin_prem_a>0"] = False

    return d, sigs


def _drive_and_probe(frames):
    """Drive the real engine through the frames (replay's per-bar pump) and, at
    EACH bar, record (a) the leaning-side signal booleans and (b) the fixed ATM
    entry strike/right so the forward outcome can be labelled afterwards.

    Returns: bars = list of dicts {i, ts, K, right, entry_prem, sigs} for bars
    where a clean ATM entry premium exists, plus tape = list of (ts, opts) so the
    forward walk can read the SAME fixed K's later LTP off the recorded tape.
    """
    from mythos import config
    from mythos import greeks as gk
    from mythos.feed import PriceStore
    from mythos.flow import FlowStack
    from mythos.heavyweights import HeavyweightBasket
    from mythos.oi_engine import OIEngine
    from mythos.signals import SignalEngine
    from mythos.vol import VolEngine
    import mythos.signals as signals_mod
    import numpy as np

    _patch_clock()
    _orig_dt = signals_mod.datetime
    signals_mod.datetime = _FakeDatetime

    tape = []          # (ts, opts_dict) for the forward walk
    bars = []          # per-bar entry candidate + recorded signals

    try:
        prices = PriceStore()
        oi = OIEngine()
        flow = FlowStack()
        vol = VolEngine()
        basket = HeavyweightBasket()
        sig = SignalEngine(oi, flow, vol, basket, prices)

        for i, (ts, fr) in enumerate(frames):
            _VCLOCK[0] = ts
            _load_frame(prices, fr)
            spot, futp, atm, ce, pe = prices.freeze_core()
            # always append to the tape (forward walk needs every bar's opts)
            tape.append((ts, fr.get("opts", {})))
            if spot <= 0 or atm <= 0:
                continue
            # ── identical engine pump to replay._run (state reconstruction) ──
            while prices.fut_ticks:
                pr, qty, bid, ask, foi = prices.fut_ticks.popleft()
                flow.vwap.update(pr, qty)
                flow.avwap.update(pr, qty)
                flow.swings.update(pr)
                flow.cvd.on_tick(pr, qty, bid, ask)
                if foi > 0:
                    flow.fut_oi.update(pr, foi)
                closed = flow.candles_1m.update(pr, qty)
                if closed:
                    flow.rsi.on_candle(closed)
                    flow.atr.on_candle(closed)
                    flow.supertrend.on_candle(closed)
                    flow.adx.on_candle(closed)
            strikes = prices.snapshot_strikes(atm_override=atm)
            for (k, right), dd in strikes.items():
                if dd["oi"] > 0:
                    oi.update_strike(k, right, dd["oi"], ts)
                if dd["vol"] > 0:
                    oi.update_volume_baseline(k, right, dd["vol"])
            oi.note_spot(spot, ts)
            oi.recompute(atm, spot)
            T = gk.years_to_expiry(config.expiry_dt_ist(), _FakeDatetime.now(replay.IST))
            vol.update_chain(spot, strikes, T, atm)
            vol.update_spot(spot)
            for sym, ltp in list(prices.hw_ltp.items()):
                basket.on_tick(sym, ltp)
            basket.recompute(spot)
            # ── the decision (NO trade taken — probe is read-only) ──
            dec = sig.evaluate()
            d, sigs = _record_signals(sig, dec, config)

            # ATM entry proxy for the forward label
            right = "call" if d == "CE" else "put"
            K = int(round(spot / 50.0) * 50)
            entry_prem = _opt_ltp(fr.get("opts", {}), K, right)
            if entry_prem is None:
                continue
            bars.append({"i": i, "ts": ts, "K": K, "right": right,
                         "spot": float(spot), "lean": d,
                         "entry_prem": entry_prem, "sigs": sigs})
        return bars, tape
    finally:
        signals_mod.datetime = _orig_dt
        replay._restore_clock()


def _label_forward(bars, tape):
    """For each candidate bar, walk forward over the tape reading the SAME fixed K
    LTP; the FIRST of pnl>=+12 (WIN) / pnl<=-10 (LOSS) within HORIZON_S wins.
    Neither by cap/EOD => TIMEOUT (secondary final-sign label kept). Mutates each
    bar with 'label' in {WIN,LOSS,TIMEOUT}, 'pts' (the doctrine payoff), and
    'final_sign'."""
    n = len(tape)
    for b in bars:
        i = b["i"]
        K, right, entry = b["K"], b["right"], b["entry_prem"]
        t0 = b["ts"]
        label = "TIMEOUT"
        last_pnl = 0.0
        for j in range(i + 1, n):
            tj, opts_j = tape[j]
            if tj - t0 > HORIZON_S:
                break
            prem_j = _opt_ltp(opts_j, K, right)
            if prem_j is None:
                continue
            pnl = prem_j - entry
            last_pnl = pnl
            if pnl >= WIN_PTS:
                label = "WIN"
                break
            if pnl <= LOSS_PTS:
                label = "LOSS"
                break
        b["label"] = label
        b["pts"] = (WIN_PTS if label == "WIN"
                    else LOSS_PTS if label == "LOSS" else 0.0)
        b["final_sign"] = (1 if last_pnl > 0 else -1 if last_pnl < 0 else 0)
    return bars


def _bucket_stats(bars_subset):
    """winrate (WIN/(WIN+LOSS), timeouts ignored) and exp_pts (mean payoff
    +12/-10/0 across ALL bars incl. timeouts)."""
    n = len(bars_subset)
    wins = sum(1 for b in bars_subset if b["label"] == "WIN")
    losses = sum(1 for b in bars_subset if b["label"] == "LOSS")
    decided = wins + losses
    winrate = (wins / decided) if decided else 0.0
    exp_pts = (sum(b["pts"] for b in bars_subset) / n) if n else 0.0
    return {"n": n, "winrate": round(winrate, 4), "exp_pts": round(exp_pts, 4),
            "wins": wins, "losses": losses}


def _build_table(bars):
    """One row per signal: TRUE-bucket vs FALSE-bucket stats + edge. Plus an
    always-true 'base_rate' pseudo-signal row."""
    # collect the universe of signal names (some bars may lack a name if the
    # leaning view had no evidence list — default missing to False)
    names = set()
    for b in bars:
        names.update(b["sigs"].keys())
    names = sorted(names)

    rows = []
    # base_rate pseudo-signal: always-true bucket = all bars
    base = _bucket_stats(bars)
    rows.append({
        "signal": "base_rate",
        "true": {"n": base["n"], "winrate": base["winrate"],
                 "exp_pts": base["exp_pts"]},
        "false": {"n": 0, "winrate": 0.0, "exp_pts": 0.0},
        "edge": round(base["exp_pts"], 4),
        "_true_wl": (base["wins"], base["losses"]),
    })

    for name in names:
        t_bucket = [b for b in bars if b["sigs"].get(name, False)]
        f_bucket = [b for b in bars if not b["sigs"].get(name, False)]
        ts_ = _bucket_stats(t_bucket)
        fs_ = _bucket_stats(f_bucket)
        rows.append({
            "signal": name,
            "true": {"n": ts_["n"], "winrate": ts_["winrate"],
                     "exp_pts": ts_["exp_pts"]},
            "false": {"n": fs_["n"], "winrate": fs_["winrate"],
                      "exp_pts": fs_["exp_pts"]},
            "edge": round(ts_["exp_pts"] - fs_["exp_pts"], 4),
            "_true_wl": (ts_["wins"], ts_["losses"]),
        })
    # sort by edge descending (base_rate kept on top regardless)
    head = [r for r in rows if r["signal"] == "base_rate"]
    tail = sorted([r for r in rows if r["signal"] != "base_rate"],
                  key=lambda r: -r["edge"])
    return head + tail


def run_probe(day):
    """Returns (result_dict, gate_ok, gate_stats)."""
    from mythos import config
    # never clobber the real trade file during the gate run (mirror replay.main)
    config.TRADES_JSON = config.TRADES_JSON + ".probe"

    from mythos.store import Store
    store = Store(config.DB_PATH)
    frames = store.load_frames(day)
    store.stop()
    if not frames:
        raise SystemExit(f"No frames recorded for {day}.")

    # ── CORRECTNESS GATE: replay._run must reproduce the known baseline ──
    base = replay._run(frames, {})
    gate_trades = base["trades"]
    gate_net = round(base["pts"], 1)
    gate_ok = True
    gate_note = ""
    if day == GATE_DAY:
        gate_ok = (gate_trades == GATE_TRADES
                   and abs(base["pts"] - GATE_NET_PTS) < 0.05)
        if not gate_ok:
            gate_note = (f"GATE FAIL: expected {GATE_TRADES} trades / "
                         f"{GATE_NET_PTS} pts, got {gate_trades} / {gate_net}")

    # ── PROBE: drive again (read-only), record signals, label forward ──
    bars, tape = _drive_and_probe(frames)
    _label_forward(bars, tape)
    table = _build_table(bars)

    result = {
        "day": day,
        "trades_actual": gate_trades,
        "net_pts_actual": gate_net,
        "n_bars": len(bars),
        "table": [{k: v for k, v in r.items() if not k.startswith("_")}
                  for r in table],
    }
    return result, gate_ok, gate_note, base, bars, table


def _dump_bars(day, bars):
    """Write per-bar rows (signals + forward label + causal context) to a native
    path BOTH bash and Windows-Python agree on, so any conditional edge question
    (exhaustion filter, regime/trend veto, combinations) is an instant offline
    computation — no 30-min engine re-run. One JSON object per line."""
    import os
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"bars_{day}.jsonl")
    with open(path, "w") as f:
        for b in bars:
            f.write(json.dumps({
                "ts": b["ts"], "lean": b["lean"], "right": b["right"],
                "spot": b["spot"], "entry_prem": b["entry_prem"],
                "label": b["label"], "pts": b["pts"], "sigs": b["sigs"],
            }) + "\n")
    return path


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    do_dump = "--dump" in args
    args = [a for a in args if a not in ("--json", "--dump")]
    day = args[0] if args else GATE_DAY

    result, gate_ok, gate_note, base, bars, table = run_probe(day)

    if do_dump:
        p = _dump_bars(day, bars)
        if not as_json:
            print(f"  per-bar dump -> {p}  ({len(bars)} rows)")

    if as_json:
        print(json.dumps(result))
        return 0 if gate_ok else 2

    # human-readable
    print(f"\n  ENTRY-EDGE PROBE — {day}")
    print(f"    correctness gate: replay._run -> {base['trades']} trades, "
          f"{base['pts']:+.1f} pts  "
          f"[{'PASS' if gate_ok else 'FAIL'}]")
    if gate_note:
        print(f"    {gate_note}")
    n_to = sum(1 for b in bars if b["label"] == "TIMEOUT")
    print(f"    probed bars: {len(bars)}  "
          f"(timeouts {n_to} = {100*n_to/max(1,len(bars)):.1f}%)")
    print(f"\n    {'signal':<26} {'T.n':>6} {'T.wr':>6} {'T.exp':>7} "
          f"{'F.exp':>7} {'edge':>7}")
    for r in table:
        t, f = r["true"], r["false"]
        print(f"    {r['signal']:<26} {t['n']:>6} {t['winrate']*100:>5.1f}% "
              f"{t['exp_pts']:>+7.2f} {f['exp_pts']:>+7.2f} {r['edge']:>+7.2f}")
    print()
    return 0 if gate_ok else 2


if __name__ == "__main__":
    sys.exit(main())
