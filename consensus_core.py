"""
MYTHOS — WEIGHTED DIRECTIONAL CONSENSUS: panels + aggregator + gate.

ONE source of truth for the consensus math. Both the prove-first calibration
(calibrate_consensus.py) and the live seam (signals.evaluate, flag-gated) import
THIS — never two drifting copies. Pure reads of already-computed engine fields;
no engine state is mutated here.

Each PANEL returns {"vote": v in [-1,+1] (bear..bull), "conf": c in [0,1]}.
The aggregator fuses them into:
    C          net consensus in [-1,+1]  = Σ (w_i · conf_i · vote_i) / Σ (w_i · conf_i)
    contested  how SPLIT the board is in [0,1] — both-sides-strongly-lit OR
               high dispersion. This is the "is the other side also loud?" test.

GATE (demote-only): fire trigger d ONLY if
    sign(C) == want(d)  AND  |C| >= CONSENSUS_MIN  AND  contested < CONTESTED_MAX
otherwise STAND ASIDE (the FIRE is demoted to CONFIRMING; never fabricated).

Weights encode the STEP-0 lesson: STRUCTURE (which contains the max_pain pull
that pointed DOWN all of 06-16 while price closed UP) is ONE vote and is the
LOWEST-weighted panel, so a confident FLOW+BREADTH+TREND majority out-votes a
wrong structure read. These are PRIORS — calibrate_consensus.py is what decides
whether they actually separate winners from losers on the real tapes.
"""
from collections import deque

# ── tunables (mirrored into config behind the flag; here as the import-light
#    defaults so calibration runs without touching config) ──────────────────
PANEL_WEIGHTS = {
    "FLOW":      0.32,   # order-flow is the most LEADING, most causal read
    "BREADTH":   0.28,   # sisters + heavyweights lead Nifty (banks ~35%)
    "TREND":     0.25,   # price-action / structure of the tape itself
    "STRUCTURE": 0.15,   # OI walls + PCR + max_pain — LOWEST (STEP-0: can be wrong)
}
CONSENSUS_MIN = 0.30    # |C| must reach this to fire (PRIOR — calibrated)
CONTESTED_MAX = 0.55    # board must be less split than this (PRIOR — calibrated)
STRONG_VOTE   = 0.50    # a panel vote |v| >= this with conf>=0.5 counts as "lit"


def want_sign(direction: str) -> float:
    """The sign C must carry for this trigger: CE wants bullish (+1)."""
    return 1.0 if direction == "CE" else -1.0


def _clamp(x, lo=-1.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


# ── PANELS ──────────────────────────────────────────────────────────────────
def panel_flow(flow) -> dict:
    """FLOW: futures order-flow. cvd.slope(30) sign + acceleration + fut_oi
    quadrant. Most LEADING, most causal of a directional move."""
    try:
        s30 = flow.cvd.slope(30)
        s120 = flow.cvd.slope(120)
        quad = flow.fut_oi.quadrant
    except Exception:
        return {"vote": 0.0, "conf": 0.0}
    # slope sign saturates at ~300 contracts/s
    v_slope = _clamp(s30 / 300.0)
    # acceleration: same-sign and growing strengthens; divergence weakens
    accel = 1.0
    if s30 * s120 >= 0 and abs(s30) > abs(s120):
        accel = 1.25
    elif s30 * s120 < 0:
        accel = 0.6        # 30s flipped against the 2m — fading, trust less
    v = _clamp(v_slope * accel)
    # quadrant adds a directional lean (the user's reversal-fuel read)
    q = {"SHORT_COVERING": 0.5, "LONG_BUILDUP": 0.35,
         "LONG_UNWINDING": -0.5, "SHORT_BUILDUP": -0.35}.get(quad, 0.0)
    v = _clamp(0.7 * v + 0.3 * q)
    # confidence: low when the 30s slope is tiny (no flow to read)
    conf = _clamp(abs(s30) / 150.0 + (0.2 if quad != "NEUTRAL" else 0.0), 0.0, 1.0)
    return {"vote": round(v, 3), "conf": round(conf, 3)}


def panel_structure(spot, atm, oi) -> dict:
    """STRUCTURE: OI walls (defended side) + near_pcr level + near_pcr_change +
    max_pain pull. DELIBERATELY the lowest-weighted panel (STEP-0: max_pain was
    backwards all of 06-16). max_pain is just ONE of four sub-components and is
    DAMPENED so it can never dominate even within this panel."""
    sub = []
    # 1. nearest-wall lean: a strong defended support below = bullish cushion;
    #    a strong resistance above pressing = bearish cap. Net of the two.
    try:
        sup = max((z for z in oi.support_zones if z.level <= spot),
                  key=lambda z: z.strength, default=None)
        res = min((z for z in oi.resistance_zones if z.level >= spot),
                  key=lambda z: z.level, default=None)
        s_str = sup.strength if sup else 0.0
        r_str = res.strength if res else 0.0
        sub.append(_clamp(s_str - r_str))
    except Exception:
        pass
    # 2. near-ATM PCR LEVEL: >1.1 puts dominate near money (bullish), <0.8 calls
    try:
        npcr = oi.near_pcr
        sub.append(_clamp((npcr - 0.95) / 0.5))
    except Exception:
        pass
    # 3. near-ATM PCR CHANGE(180): writers stepping in = the user's key tell
    try:
        dp = oi.near_pcr_change(180)
        sub.append(_clamp(dp / 0.10))
    except Exception:
        pass
    # 4. max_pain pull — DAMPENED. mp above spot = upward pin (bullish) and
    #    mirror, but STEP-0 proved this can be flat-out wrong, so it gets HALF
    #    weight inside an already-low-weight panel.
    try:
        mp = oi.max_pain
        if mp > 0 and spot > 0:
            pull = _clamp((mp - spot) / 40.0)
            sub.append(0.5 * pull)
    except Exception:
        pass
    if not sub:
        return {"vote": 0.0, "conf": 0.0}
    v = _clamp(sum(sub) / max(1, len([s for s in sub if abs(s) > 1e-9]) or 1))
    # average over present components; confidence scales with how many are non-trivial
    nontrivial = sum(1 for s in sub if abs(s) > 0.05)
    conf = _clamp(0.3 + 0.2 * nontrivial, 0.0, 0.9)
    return {"vote": round(v, 3), "conf": round(conf, 3)}


def panel_breadth(basket, prices, idx_hist, now) -> dict:
    """BREADTH: BankNifty/FinNifty 180s momentum + heavyweight basket sentiment.
    Sisters LEAD Nifty (banks ~35%). MUST abstain (conf→0) when sisters are
    stale/absent — PROVEN necessary: the 06-16 recording has NO FinNifty and a
    fabricated breadth vote would be noise. Degrades to basket-only at reduced
    confidence, and to nothing if neither is present."""
    sis_votes = []
    try:
        for name in ("BANKNIFTY", "FINNIFTY"):
            ltp = prices.idx_ltp.get(name, 0.0)
            if ltp <= 0:
                continue
            ts = prices.idx_ts.get(name, 0.0)
            # staleness gate, mirrors signals._sister_alignment
            try:
                from mythos import clk
                if ts > 0 and (clk.mono() - ts) > 60.0:
                    continue
            except Exception:
                pass
            h = idx_hist.setdefault(name, deque(maxlen=400))
            if not h or now - h[-1][0] >= 1.0:
                h.append((now, ltp))
            past = next((v for t, v in h if now - t <= 180), None)
            if past and past > 0:
                chg = (ltp - past) / past * 100.0
                sis_votes.append(_clamp(chg / 0.20))   # ±0.20% saturates
    except Exception:
        pass
    # heavyweight basket sentiment 0..100 → -1..+1
    bask_v = bask_conf = 0.0
    try:
        sent = float(getattr(basket, "sentiment", 50.0))
        bask_v = _clamp((sent - 50.0) / 30.0)
        bask_conf = 0.5 if abs(sent - 50.0) > 2.0 else 0.2
    except Exception:
        pass
    # sister-index OPTION OI PCR (task #39) — BankNifty/FinNifty written-OI bias,
    # when SISTER_CHAIN is flowing. PCR>1 = puts written below = bullish floor.
    oi_votes = []
    try:
        idx_pcr = getattr(prices, "idx_pcr", {})
        for name in ("BANKNIFTY", "FINNIFTY"):
            pcr = idx_pcr.get(name, 0.0)
            if pcr and pcr > 0:
                oi_votes.append(_clamp((pcr - 1.0) / 0.5))   # 1.5→+1, 0.5→-1
    except Exception:
        pass
    parts = []
    conf = 0.0
    if sis_votes:
        parts.append(("sis", sum(sis_votes) / len(sis_votes), 0.6 * (len(sis_votes) / 2.0) + 0.2))
    if oi_votes:
        parts.append(("sisOI", sum(oi_votes) / len(oi_votes), 0.5 * (len(oi_votes) / 2.0) + 0.2))
    if bask_conf > 0:
        parts.append(("bask", bask_v, bask_conf))
    if not parts:
        return {"vote": 0.0, "conf": 0.0}    # ABSTAIN — no breadth data
    wsum = sum(c for _, _, c in parts)
    v = sum(val * c for _, val, c in parts) / wsum if wsum else 0.0
    conf = _clamp(wsum, 0.0, 1.0)
    return {"vote": round(_clamp(v), 3), "conf": round(conf, 3)}


def panel_trend(spot, flow, prices) -> dict:
    """TREND / PRICE-ACTION: supertrend dir + spot-vs-VWAP + AVWAP reclaim/loss
    + swing structure (higher-highs vs lower-lows). The structure of the tape
    itself, independent of OI and of flow's CVD."""
    sub = []
    fut = (prices.futures or spot)
    # 1. supertrend direction
    try:
        st = (flow.supertrend.direction or "").upper()
        if st == "UP":
            sub.append(0.6)
        elif st == "DOWN":
            sub.append(-0.6)
    except Exception:
        pass
    # 2. spot vs session VWAP
    try:
        vw = flow.vwap.value
        if vw > 0:
            sub.append(_clamp((fut - vw) / 25.0))
    except Exception:
        pass
    # 3. AVWAP reclaim/loss (trapped-trader lens)
    try:
        av = flow.avwap
        if av.from_high > 0 and fut > av.from_high:
            sub.append(0.4)                 # bears who sold the high underwater
        if av.from_low > 0 and fut < av.from_low:
            sub.append(-0.4)                # bulls who bought the low underwater
    except Exception:
        pass
    # 4. swing structure: most-recent pivot kind
    try:
        sup = flow.swings.supports()
        res = flow.swings.resistances()
        last_pivots = list(getattr(flow.swings, "pivots", []))
        if last_pivots:
            kind = last_pivots[-1][1]
            sub.append(0.3 if kind == "L" else -0.3)  # last pivot a LOW → turning up
    except Exception:
        pass
    if not sub:
        return {"vote": 0.0, "conf": 0.0}
    v = _clamp(sum(sub) / 1.5)              # ~2-3 aligned subs saturate
    conf = _clamp(0.3 + 0.18 * len(sub), 0.0, 0.9)
    return {"vote": round(v, 3), "conf": round(conf, 3)}


def panel_votes(spot, atm, oi, flow, basket, prices, sig, idx_hist, now) -> dict:
    """All four panels in one call. `sig` is accepted for parity with the live
    seam (future panels may read sig state) but the four base panels are pure
    reads of oi/flow/basket/prices."""
    return {
        "FLOW":      panel_flow(flow),
        "STRUCTURE": panel_structure(spot, atm, oi),
        "BREADTH":   panel_breadth(basket, prices, idx_hist, now),
        "TREND":     panel_trend(spot, flow, prices),
    }


# ── AGGREGATOR ────────────────────────────────────────────────────────────────
def fuse(votes: dict, weights: dict = None) -> dict:
    """Fuse panel votes → net consensus C in [-1,+1] + a contested measure.

    C = Σ (w_i · conf_i · vote_i) / Σ (w_i · conf_i)   (confidence-weighted mean).
        A panel that abstains (conf=0) drops OUT of the denominator — it does
        NOT pull C toward 0 (critical: a missing-FinNifty BREADTH must be silent,
        not a phantom neutral vote).

    contested = max(bull_pressure, bear_pressure-side detection): how strongly is
        the LOSING side lit?  Two independent reads, take the max so EITHER trips:
          (a) both-sides-lit: min(bull_w, bear_w) / total_w  — share of effective
              weight pulling AGAINST the net. ~0 = one-sided, ~0.5 = even split.
          (b) dispersion: confidence-weighted stdev of votes, normalised. A board
              where panels scatter widely is contested even if it nets nonzero.
    """
    w = weights or PANEL_WEIGHTS
    num = den = 0.0
    eff = {}    # panel -> effective weight (w*conf)
    for name, pv in votes.items():
        ew = w.get(name, 0.0) * pv.get("conf", 0.0)
        eff[name] = ew
        num += ew * pv.get("vote", 0.0)
        den += ew
    C = (num / den) if den > 1e-9 else 0.0
    C = _clamp(C)

    # (a) both-sides-lit: effective weight pulling each way (vote sign × eff w)
    bull_w = sum(eff[n] for n, pv in votes.items() if pv.get("vote", 0.0) > 0.05)
    bear_w = sum(eff[n] for n, pv in votes.items() if pv.get("vote", 0.0) < -0.05)
    tot_w = bull_w + bear_w
    split = (min(bull_w, bear_w) / tot_w) if tot_w > 1e-9 else 0.0
    # both-sides STRONGLY lit gets an extra penalty (the 06-16 failure mode):
    strong_both = (
        any(pv.get("vote", 0) >= STRONG_VOTE and pv.get("conf", 0) >= 0.5
            for pv in votes.values())
        and any(pv.get("vote", 0) <= -STRONG_VOTE and pv.get("conf", 0) >= 0.5
                for pv in votes.values()))
    if strong_both:
        split = max(split, 0.6)

    # (b) confidence-weighted dispersion of votes (normalised to [0,1])
    if den > 1e-9:
        mean = C
        var = sum(eff[n] * (pv.get("vote", 0.0) - mean) ** 2
                  for n, pv in votes.items()) / den
        disp = min(1.0, var ** 0.5)         # stdev in vote-units, ≤1
    else:
        disp = 1.0                          # nothing to read = maximally uncertain
    contested = max(split, disp)
    return {"C": round(C, 3), "contested": round(contested, 3),
            "bull_w": round(bull_w, 3), "bear_w": round(bear_w, 3),
            "agreement": round(1.0 - contested, 3)}


def gate_pass(direction: str, cons: dict,
              consensus_min: float = None, contested_max: float = None) -> bool:
    """The fire rule. True = the consensus permits trigger `direction` to fire;
    False = STAND ASIDE (the live seam demotes FIRE→CONFIRMING). DEMOTE-ONLY:
    this can never turn a non-fire INTO a fire."""
    cmin = CONSENSUS_MIN if consensus_min is None else consensus_min
    cmax = CONTESTED_MAX if contested_max is None else contested_max
    C = cons["C"]
    if cons["contested"] >= cmax:
        return False                       # board split — stand aside
    if abs(C) < cmin:
        return False                       # consensus too weak
    return (C > 0) == (direction == "CE")  # sign must match the trigger


def consensus_for(spot, atm, oi, flow, basket, prices, sig, idx_hist, now,
                  weights=None) -> dict:
    """Live-seam one-call (task #39): panels → fuse → dict {C, contested, votes,...}.
    Pure reads; mutates only idx_hist (the sister-momentum ring, same as the
    calibrator). Keeps the consensus math in ONE place — signals.py imports this,
    never a copy."""
    votes = panel_votes(spot, atm, oi, flow, basket, prices, sig, idx_hist, now)
    cons = fuse(votes, weights)
    cons["votes"] = votes
    return cons
