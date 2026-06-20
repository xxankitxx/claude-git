# GREEKS-BASED ENTRY/AVOID INSTRUCTIONS FOR ATM OPTION BUYING — v2.0

> **Purpose:** Instruct an LLM on how to decide WHEN to ENTER and WHEN to AVOID a long ATM option (CE or PE) on NIFTY, with focus on **slow, range-bound markets** where decay destroys buyers.
>
> **Scope:** Direction-neutral. Every rule holds equally for ATM CE and ATM PE.
>
> **Market:** NSE / NIFTY. Session 09:15–15:30 IST (**6.25 trading hours**). Weekly expiry **Tuesday** (shifts to **Monday** if Tuesday is a holiday). 0DTE = expiry day.

---

## 0. HOW TO USE THIS FILE

This is a decision spec, not a narrative. Apply the gates in the stated **precedence order**. A later gate can never overturn an earlier veto. Where thresholds appear, treat them as **calibration parameters** (Section 9), not fixed truths — fit them to the trader's own historical log before relying on them.

---

## 1. WHAT CHANGED FROM v1 (AND WHY)

v1 was conceptually sound but a weak standalone filter. Corrections:

1. **Master rule was tautological (timeframe mismatch).** v1 compared the *full-life* straddle against *daily* theta — for any DTE > ~2 days this always says ENTER. **Fixed:** the correct comparison is **expected REALIZED move vs. IMPLIED (breakeven) move over the same window**, not "straddle vs theta." See Section 2–3.
2. **"IV rising" was pro-cyclical.** IV spikes *after* moves; buying on rising IV risks vega-crush. **Fixed:** replaced with an **IV-vs-realized (HV)** spread, demoted to a *secondary* filter behind price action. See Section 4, Gate C.
3. **No realized-movement gate.** The trader's actual failure mode is low realized range — a price footprint, not a Greek. **Fixed:** an **ATR/ROC price-action gate is now the PRIMARY veto.** See Section 4, Gate A.
4. **Linear theta scaling was wrong intraday.** **Fixed:** non-linear √τ time-value decay for intraday / expiry day. See Section 5.
5. **Ignored put skew.** **Fixed:** explicit CE/PE theta reconciliation. See Section 6.
6. **Ignored costs.** **Fixed:** round-trip cost buffer added to the breakeven. See Section 7.

---

## 2. THE CORRECTED MENTAL MODEL — REALIZED vs IMPLIED

An ATM option buyer does **not** profit because "the market moved." They profit when the market moves **more than the option's price already pays for**. The price already bakes in an expected move; you are paid only on the *excess* of realized over implied.

- **Implied move** = what you PAY for (embedded in premium / straddle / IV).
- **Realized move** = what you GET (actual spot travel while you hold).
- **Edge condition:** `Realized move > Implied move` (plus costs).

A slow range is precisely the state where **realized < implied** → structural loss. This is *why* the trader loses in stuck markets, stated exactly. The whole filter is an apparatus for estimating both sides and refusing the trade when realized is unlikely to clear implied.

---

## 3. THE GREEK-NATIVE ANSWER — THE GAMMA–THETA BREAKEVEN MOVE

The original question was "which Greek tells me when to enter." The precise answer is **the ratio of Theta to Gamma, expressed as a breakeven spot move.**

For a long option, P&L over a small interval Δt with spot move ΔS (delta-neutralised) is the classic gamma–theta tradeoff:

```
P&L ≈ ½ · Γ · (ΔS)²  −  |Θ| · Δt
```

Set to zero → the **breakeven move** that makes convexity pay for one Δt of decay:

```
ΔS_BE = √( 2 · |Θ| · Δt / Γ )      [in spot points]
```

- **Γ (gamma)** = the payoff accelerator (highest at ATM).
- **Θ (theta)** = the cost (highest at ATM, symmetric across CE/PE at the exact strike).
- Their ratio collapses to a single number in **spot points**: how far NIFTY must travel just to break even.

**Key result (why this unifies everything):** for an ATM option under Black–Scholes, Θ/Γ ≈ −½·S²·σ², so

```
ΔS_BE(ATM) = √(2·|Θ|·Δt/Γ) ≈ S · σ · √Δt  =  the 1-SD IMPLIED move over Δt.
```

So **the gamma–theta breakeven move for an ATM option equals its own 1-standard-deviation implied move.** The Greek view and the straddle/IV view are the *same thing*. This is the mathematical statement of Section 2: to win you need realized movement to exceed the implied (breakeven) move — on average it won't, so you only buy when you have a specific reason to expect realized > implied (cheap vol, a catalyst, or a directional edge).

> **Directional nuance (do not omit):** a *directional* ATM buyer who is right also collects the delta term `Δ·ΔS`, which lowers their true breakeven below ΔS_BE. ΔS_BE is therefore the **conservative, direction-neutral hurdle** — correct for the CE/PE-symmetric framing the trader asked for, and the right number when direction is uncertain.

---

## 4. DECISION HIERARCHY (STRICT PRECEDENCE)

Apply in order. **Gate A has veto power over B and C. B has veto power over C.** Never let a volatility reading (C) override a dead-range reading (A).

### GATE A — PRICE-ACTION / REALIZED-RANGE VETO (primary)
The footprint of the slow market, independent of any Greek.
- **AVOID if** `ATR(5) < ATR(20)` (realized range contracting), **OR**
- **AVOID if** spot Rate-of-Change over the intraday window is below `ROC_MIN` (pure drift), **OR**
- **AVOID if** spot has been pinned inside a tight band (e.g. < ~0.4% of spot) for the last N minutes.
- If any trigger → **AVOID. Stop. Do not evaluate B or C.**

### GATE B — CORE EDGE TEST: REALIZED vs BREAKEVEN (with costs)
- Compute `ΔS_BE` (Section 3) over the intended holding window.
- Estimate `Expected_Realized_Move` over the same window from recent realized vol / ATR (Section 5).
- **AVOID if** `Expected_Realized_Move ≤ (ΔS_BE + Costs_in_points) × buffer`.
- Convexity cannot pay for decay + frictions → no trade.

### GATE C — VOLATILITY FILTER: PAY LESS THAN YOU EXPECT TO GET (secondary)
Backward-looking proxy for "implied < realized." Use only after A and B pass.
- **Prefer ENTER if** `IV < HV(20)` (implied cheaper than recent realized) **AND** India VIX rank is low.
- **AVOID if** India VIX rank is rich (high percentile) → vega-crush risk on any stall, even mid-move.
- **CAUTION (size down) if** vol is mid-range and not clearly cheap.

### RESULT
`ENTER` only if A passes, B passes, and C is favorable/neutral. Otherwise `AVOID` (or `CAUTION`).

> **Why this ordering matters:** Gate C's "cheap vol" can persist for days after a one-off event while the market goes dead — buying then bleeds. Gate A (realized range) must therefore veto Gate C, never the reverse. This is the single most important structural fix over v1.

---

## 5. TIME NORMALIZATION (the fix for the v1 timeframe bug)

### 5.1 Scaling implied move to the holding window (DTE ≥ 2)
Under constant IV the straddle scales with √time. The implied move over your window:

```
Implied_Move(window) = ATM_Straddle × √(hold / DTE)
```

*Footnote:* the ATM straddle ≈ the **expected absolute** move ≈ `0.8 × (1-SD)` (the √(2/π) factor). Treat "straddle = expected move" as a ~0.8-SD heuristic, not an identity. For the breakeven, prefer the Greek form `ΔS_BE` (Section 3), which is exact for ATM.

### 5.2 Expected realized move over the window
```
Expected_Realized_Move = ( HV(20) × S / √252 ) × √(hold_in_trading_days)
```
(ATR over your timeframe is an acceptable substitute for the realized estimate.)

### 5.3 Intraday / expiry-day theta is NON-LINEAR
Linear `theta_per_day × days` is fine for multi-day weekday holds but **wrong intraday and badly wrong on expiry afternoons.** ATM time value ≈ `C·√τ` (τ = time to expiry), so decay over a hold from τ₁ → τ₂ is:

```
Decay ≈ TimeValue(τ₁) × ( 1 − √(τ₂ / τ₁) )
```

Using NSE session hours on 0DTE (expiry at close, ~6.25h day): entering with H₁ hours left, exiting with H₂ left, decay fraction ≈ `1 − √(H₂/H₁)`.
- Enter at open (6.25h), exit +1h (5.25h left): ≈ **8%** of remaining TV gone.
- Enter with 1h left, hold to 15 min left: ≈ **50%** gone in 45 minutes.

This replaces DeepSeek's `(hours/6.5)^1.5` heuristic (which used a US 6.5h session and an arbitrary exponent) with a principled √τ form on the correct NSE 6.25h session. *Caveat:* the √τ law assumes the option stays ATM and IV is constant; in the final ~30–60 min of expiry, pin/settlement discreteness breaks it — treat the last hour as a special, high-risk regime.

---

## 6. SKEW & CE/PE SYMMETRY

Theta is symmetric at the *exact* ATM strike, but NIFTY usually carries a **put skew** (ATM PE often slightly richer IV/theta than ATM CE).
- For the **cost / breakeven (AVOID) decision**, use the **conservative (higher) of CE and PE theta**, or their average.
- For Γ in `ΔS_BE`, ATM CE and PE gamma are near-identical; either is fine.
- All gates remain direction-neutral; skew only nudges the cost side, never flips CE-vs-PE logic.

---

## 7. COSTS (must net out)

A real filter clears frictions, not just theta. Round-trip cost in premium points = brokerage + STT (on premium) + exchange txn charges + GST + stamp + **slippage**. Express as `Costs_in_points` and add it to the breakeven in Gate B. On NIFTY this is small but non-trivial relative to a quiet day's move — and on a slow day it is often the difference between marginal-win and net-loss.

---

## 8. PSEUDOCODE (corrected)

```python
# --- LIVE INPUTS ---
S            # NIFTY spot
sigma_iv     # ATM IV, annualized decimal (use avg(CE,PE); use max for conservative AVOID)
theta_atm    # ATM theta, premium pts/day (avg or max-magnitude of CE,PE) -- match day-convention to IV
gamma_atm    # ATM gamma
straddle     # ATM CE premium + ATM PE premium
DTE          # days to expiry (calendar)
hold         # intended holding window (trading days; intraday as fraction of session)
atr5, atr20  # 5- / 20-day ATR of spot (points)
hv20         # 20-day realized vol, annualized decimal
vix_rank     # India VIX percentile/rank over lookback (0-100)
roc          # spot rate-of-change over intraday window (%)
costs_pts    # round-trip costs in premium points (incl. slippage)

# --- GREEK-NATIVE BREAKEVEN (Section 3) ---
be_move = (2 * abs(theta_atm) * hold / gamma_atm) ** 0.5     # spot pts; for ATM ≈ S*sigma_iv*sqrt(hold/252)

# --- EXPECTED REALIZED MOVE over same window (Section 5.2) ---
exp_real_move = (hv20 * S / (252 ** 0.5)) * (hold ** 0.5)    # ATR-based estimate also acceptable

# === GATE A: PRICE-ACTION VETO (primary; overrides all) ===
if (atr5 < atr20) or (roc < ROC_MIN):
    return "AVOID"   # contracting / dead range

# === GATE B: REALIZED vs BREAKEVEN + COSTS ===
if exp_real_move <= (be_move + costs_pts) * BUFFER:
    return "AVOID"   # convexity won't pay for decay + frictions

# === GATE C: VOLATILITY FILTER (secondary) ===
if vix_rank > VIX_RANK_HIGH:
    return "AVOID"            # rich vol -> vega-crush risk
cheap_vol = (sigma_iv < hv20)  # implied < realized
if (not cheap_vol) and (vix_rank > VIX_RANK_MID):
    return "CAUTION"         # size down

return "ENTER"
```
> On expiry day, replace the linear `theta_atm * hold` notion of decay inside `be_move` with the √τ time-value decay of Section 5.3.

---

## 9. PARAMETERS TO CALIBRATE (starting points — FIT TO THE TRADER'S OWN LOG)

These are **defaults to be tuned against historical trades, not fixed constants.** Calibrate on the trader's own executed-trade log (label each past trade win/loss, then choose thresholds that best separate the two).

| Parameter | Starting value | Meaning |
|---|---|---|
| `ROC_MIN` | ~0.15–0.25% / 15 min | Min intraday spot motion to not be "drift" |
| `ATR(5)/ATR(20)` veto | < 1.0 | Realized range contracting |
| `BUFFER` | 1.1–1.2 | Margin by which realized must beat breakeven |
| `VIX_RANK_HIGH` | ~75 | Rich-vol → avoid (vega crush) |
| `VIX_RANK_MID` | ~50 | Above this + not cheap → caution |
| `costs_pts` | broker-specific | Round-trip frictions incl. slippage |
| Tight-band width (Gate A) | ~0.4% of spot | Pin detection |

---

## 10. KNOWN LIMITATIONS & ASSUMPTIONS (read before trusting)

State these honestly; they bound the filter's validity.

1. **HV is backward-looking.** Gate C uses realized history as a proxy for *forward* realized vol. It is blind to scheduled catalysts. Overlay an **event calendar** (RBI, US data, expiry mechanics, global/geopolitical risk such as crude/Israel-Iran shocks): a quiet HV before a known catalyst can justify a cheap-vol long that the spread alone would miss.
2. **Black–Scholes assumptions.** ΔS_BE = S·σ·√Δt and the √τ decay assume constant IV, no jumps, and the option staying ATM. Gaps, IV regime shifts, and the option drifting away from ATM all break these. Treat outputs as approximations, not guarantees.
3. **Day-count convention mismatch.** Broker theta is often per-*calendar*-day while IV annualizes on ~252 *trading* days. Reconcile, or expect a small systematic bias in `be_move` vs the S·σ·√t cross-check.
4. **Expiry final hour is a different regime.** Pin risk and discrete settlement invalidate the smooth-decay model in roughly the last 30–60 minutes; the filter should flag this window rather than trust the formulas.
5. **No directional alpha here.** This filter only decides whether *movement is likely to clear the breakeven*. It is a go/no-go on convexity, not a direction signal. Pair it with a separate directional/structural model to choose CE vs PE.
6. **Unvalidated until calibrated.** Section 9 thresholds are placeholders. The filter is only as good as its calibration against the trader's real fills, including slippage. Backtest before live reliance.

---

## 11. ONE-LINE SUMMARY FOR THE LLM

> **An ATM option buyer profits only when REALIZED move exceeds the IMPLIED (breakeven) move. The breakeven is Greek-native: ΔS_BE = √(2·|Θ|·Δt/Γ), which for an ATM option equals its own 1-SD implied move. Gate trades in strict order: (A) a price-action/ATR-ROC veto for dead ranges overrides everything; (B) refuse unless expected realized move clears the breakeven plus costs; (C) only then, prefer entries where implied vol is cheap vs realized and VIX rank is low, avoiding rich vol. Normalize all moves to the holding window (√time), use non-linear √τ decay intraday/expiry, take the conservative CE/PE theta for skew, net out costs, and calibrate every threshold to the trader's own log. All rules are symmetric for ATM CE and PE.**
