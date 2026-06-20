#!/usr/bin/env python3
"""
MYTHOS — CONSENSUS-VOTE CALIBRATION (prove-first; the FIRST deliverable).

PURPOSE (non-negotiable discipline): BEFORE any gate is built, dump — at every
real ENTRY on a recorded tape — each independent PANEL's directional vote, the
fused net consensus C in [-1,+1], and a CONTESTED measure, and SHOW whether C
cleanly separates the LOSERS (should be split / opposite the fired side) from
the WINNERS (should be an aligned consensus on the fired side). If C does NOT
separate them, this script says so and the gate is NOT built.

It re-uses replay.py's EXACT engine loop by calling replay._run — there is ZERO
re-implementation and therefore zero strategy drift. The only instrumentation is
a monkeypatch on PaperTrader.try_enter that, the instant a real trade is created,
snapshots the four-panel consensus (computed from already-computed engine fields)
and tags it onto the trade object. After the run, each closed trade is paired
with the consensus that stood at its entry and its eventual P&L.

    python calibrate_consensus.py 2026-06-16
    python calibrate_consensus.py 2026-06-15
    python calibrate_consensus.py 2026-06-13 --sim
    python calibrate_consensus.py 2026-06-14 --sim
    python calibrate_consensus.py 2026-06-16 --set CONSENSUS_MIN=0.25 --set CONTESTED_MAX=0.5
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    args = sys.argv[1:]
    sim = "--sim" in args
    args = [a for a in args if a != "--sim"]
    overrides = {}
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--set" and i + 1 < len(args):
            k, _, v = args[i + 1].partition("=")
            overrides[k.strip()] = v.strip()
            i += 2
        else:
            rest.append(args[i])
            i += 1
    day = rest[0] if rest else None

    import consensus_core as cc
    for k, v in overrides.items():       # gate-threshold overrides for A/B
        if hasattr(cc, k):
            setattr(cc, k, float(v))

    from mythos import config
    if sim:
        config.DB_PATH = config.DB_PATH.replace("mythos.db", "mythos_sim.db")
    config.TRADES_JSON = config.TRADES_JSON + ".replay"

    from mythos.store import Store
    store = Store(config.DB_PATH)
    days = store.frame_days()
    if not day:
        print("\n  Recorded days:", days,
              "\n  usage: calibrate_consensus.py <day> [--sim] [--set K=V]\n")
        store.stop()
        return 0
    frames = store.load_frames(day)
    store.stop()
    if not frames:
        print(f"  No frames for {day}. Available: {days}")
        return 1

    print(f"\n  CONSENSUS CALIBRATION — {day} — {len(frames)} frames"
          f"{'  [SIM DB]' if sim else ''}")
    rows = _run_instrumented(frames)
    _report(day, rows)
    return 0


def _run_instrumented(frames):
    """Call replay._run, but monkeypatch PaperTrader.try_enter to snapshot the
    consensus the moment a trade is created — using the SAME engine objects the
    decision was made on (captured via the SignalEngine the trader sees)."""
    import replay
    import consensus_core as cc
    from mythos.trader import PaperTrader
    from mythos import clk

    # we need the engine objects at entry time. replay._run builds them locally;
    # the trader holds `prices`. We reconstruct the panel inputs from a registry
    # that replay populates — simplest robust hook: wrap _run to expose engines.
    captured = {}            # trade_id -> consensus snapshot
    idx_hist = {}

    # Hook: replay._run calls trader.try_enter(decision, expected_move, oi).
    # We wrap it so we can read oi + the live flow/basket/prices from a context
    # that _run shares. Easiest: patch SignalEngine.evaluate to stash the latest
    # engine refs, then read them inside the try_enter wrap.
    import mythos.signals as signals
    _orig_eval = signals.SignalEngine.evaluate
    ctx = {}

    def _eval_wrap(self):
        ctx["sig"] = self
        ctx["oi"] = self.oi
        ctx["flow"] = self.flow
        ctx["basket"] = self.basket
        ctx["prices"] = self.prices
        return _orig_eval(self)

    _orig_try = PaperTrader.try_enter

    def _try_wrap(self, decision, expected_move, oi=None):
        t = _orig_try(self, decision, expected_move, oi)
        if t is not None and ctx:
            spot, futp, atm, ce, pe = ctx["prices"].freeze_core()
            votes = cc.panel_votes(spot, atm, ctx["oi"], ctx["flow"],
                                   ctx["basket"], ctx["prices"], ctx["sig"],
                                   idx_hist, clk.now())
            cons = cc.fuse(votes)
            captured[t.id] = {"dir": t.direction, "kind": decision.kind,
                              "votes": votes, "cons": cons}
        return t

    signals.SignalEngine.evaluate = _eval_wrap
    PaperTrader.try_enter = _try_wrap
    try:
        res = replay._run(frames, {})
    finally:
        signals.SignalEngine.evaluate = _orig_eval
        PaperTrader.try_enter = _orig_try

    out = []
    for d in res["detail"]:
        snap = captured.get(d["id"])
        if not snap:
            continue
        snap = dict(snap)
        snap.update(id=d["id"], pnl_pts=d["pnl_pts"], pnl_cash=d["pnl_cash"],
                    exit=d["exit"], win=d["pnl_pts"] >= 0)
        out.append(snap)
    return out


def _report(day, rows):
    import consensus_core as cc
    if not rows:
        print("  (no trades on this tape under baseline config)\n")
        return
    print(f"\n  {'#':>3} {'dir':>3} {'kind':>7}  "
          f"{'FLOW':>6} {'STRUC':>6} {'BRDTH':>6} {'TREND':>6}  "
          f"{'C':>6} {'cont':>5}  {'fires?':>6}  {'pnl':>6}  res  flag")
    kept_good = cut_bad = kept_bad = cut_good = 0
    for r in sorted(rows, key=lambda r: r["id"]):
        v = r["votes"]
        c = r["cons"]
        fires = cc.gate_pass(r["dir"], c)
        res = "WIN " if r["win"] else "LOSS"
        if r["win"] and fires:
            kept_good += 1; flag = "KEEP-GOOD"
        elif (not r["win"]) and (not fires):
            cut_bad += 1; flag = "CUT-BAD"
        elif (not r["win"]) and fires:
            kept_bad += 1; flag = "kept-bad"
        else:
            cut_good += 1; flag = "CUT-GOOD!"
        print(f"  #{r['id']:>2} {r['dir']:>3} {r['kind']:>7}  "
              f"{v['FLOW']['vote']:>+6.2f} {v['STRUCTURE']['vote']:>+6.2f} "
              f"{v['BREADTH']['vote']:>+6.2f} {v['TREND']['vote']:>+6.2f}  "
              f"{c['C']:>+6.2f} {c['contested']:>5.2f}  {str(fires):>6}  "
              f"{r['pnl_pts']:>+6.1f}  {res}  {flag}")
    n = len(rows)
    wins = [r for r in rows if r["win"]]
    losses = [r for r in rows if not r["win"]]
    print(f"\n  baseline: {n} trades, {len(wins)}W / {len(losses)}L")
    print(f"  GATE would: KEEP {kept_good} winners · CUT {cut_bad} losers · "
          f"kept {kept_bad} losers · CUT {cut_good} winners")
    base_net = sum(r["pnl_cash"] for r in rows)
    gated = [r for r in rows if cc.gate_pass(r["dir"], r["cons"])]
    gated_net = sum(r["pnl_cash"] for r in gated)
    gw = sum(1 for r in gated if r["win"])
    print(f"  net P&L: baseline ₹{base_net:+,.0f} ({len(wins)}W/{len(losses)}L)"
          f"  →  gated ₹{gated_net:+,.0f} ({gw}W/{len(gated)-gw}L over "
          f"{len(gated)}/{n} trades)")

    def cside(r):
        return r["cons"]["C"] * cc.want_sign(r["dir"])
    win_c = [cside(r) for r in wins]
    loss_c = [cside(r) for r in losses]
    if win_c or loss_c:
        import statistics as st
        print(f"\n  SEPARATION (signed C on fired side; want winners>0, losers<0):")
        if win_c:
            print(f"    winners: mean {st.fmean(win_c):+.2f}  "
                  f"[{min(win_c):+.2f}..{max(win_c):+.2f}]  "
                  f"contested {st.fmean([r['cons']['contested'] for r in wins]):.2f}")
        if loss_c:
            print(f"    losers : mean {st.fmean(loss_c):+.2f}  "
                  f"[{min(loss_c):+.2f}..{max(loss_c):+.2f}]  "
                  f"contested {st.fmean([r['cons']['contested'] for r in losses]):.2f}")
    print(f"\n  thresholds: CONSENSUS_MIN={cc.CONSENSUS_MIN} "
          f"CONTESTED_MAX={cc.CONTESTED_MAX}  weights={cc.PANEL_WEIGHTS}\n")


if __name__ == "__main__":
    sys.exit(main())
