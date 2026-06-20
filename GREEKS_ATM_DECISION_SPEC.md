# GREEKS-BASED ENTRY / AVOID DECISION SPEC — ATM NIFTY OPTION BUYING

> **Purpose.** Decide WHEN to ENTER and WHEN to AVOID a long ATM option (CE or PE) on NIFTY, with primary focus on **slow, range-bound markets** where decay quietly destroys buyers.
>
> **Direction-neutral.** Every rule holds identically for ATM CE and ATM PE.
>
> **Market.** NSE / NIFTY. Session 09:15–15:30 IST (**6.25 trading hours**). Weekly expiry **Tuesday** (shifts to **Monday** if Tuesday is a holiday). 0DTE = expiry day.

---

## 0. HOW TO USE

This is an executable decision spec. Apply the gates in **strict precedence order**: an earlier gate's veto can never be overturned by a later gate. Numeric thresholds are **calibration parameters** (Section 8) — fit them to the trader's own executed-trade log before relying on them.

---

## 1. CORE PRINCIPLE — REALIZED vs IMPLIED

An ATM option buyer does not profit because the market "moved." The premium already prices in an expected move. The buyer is paid only on the **excess of realized movement over what was already priced**.

- **Implied move** = what you PAY for (embedded in IV / premium).
- **Realized move** = what you GET (actual spot travel while you hold).
- **Edge condition:** `Realized move > Implied move`, after costs.

A slow range is exactly the state where `realized < implied` → structural loss. Every gate below exists to estimate both sides and refuse the trade when realized is unlikely to clear implied.

---

## 2. THE BREAKEVEN HURDLE (ΔS_BE)

For a long option the per-interval P&L is the gamma–theta tradeoff `½·Γ·(ΔS)² − |Θ|·Δt`. Setting it to zero gives the breakeven spot move. For an **ATM** option this has a closed form, because Θ/Γ = −½·S²·σ²:

```
ΔS_BE = S · σ · √Δt          [spot points]
```

- `σ` = ATM implied volatility (annualized, decimal).
- `Δt` = holding window in years (trading-day basis: `hold_days / 252`).
- `S` = NIFTY spot.

**What this number means.** ΔS_BE is simultaneously (a) the gamma–theta breakeven move where convexity exactly pays for decay, and (b) the option's own **1-standard-deviation implied move** over the holding window. The Greek view and the IV/straddle view are the same number. Theta is therefore **already accounted for inside ΔS_BE** — you do not subtract it again. To profit, **realized movement must exceed ΔS_BE** (i.e. realized vol must exceed implied vol), plus frictions.

> Compute ΔS_BE **directly from IV** as above. This is exact for ATM and is the sole hurdle used in the go/no-go (Gate B). It needs no theta input and is immune to broker day-count conventions.

---

## 3. DECISION HIERARCHY (STRICT PRECEDENCE)

Apply in order. **Gate A vetoes B and C. Gate B vetoes C.** A volatility reading must never override a dead-range reading.

### GATE A — PRICE-ACTION / REALIZED-RANGE VETO (primary)
The footprint of the slow market, independent of any Greek. Composite test so that a *high-volatility* contraction is not mistaken for a *dead* market:

- **AVOID** if range is both **contracting and absolutely low**:
  `ATR(5) < ATR(20)` **AND** `ATR(5) < ATR_FLOOR`.
- **CAUTION (size down)** if `ATR(5) < ATR(20)` but `ATR(5) ≥ ATR_FLOOR` (shrinking, still alive).
- **AVOID** if intraday spot Rate-of-Change is below `ROC_MIN` (pure drift).
- **AVOID** if spot has been pinned inside a tight band (≈ `BAND_PCT` of spot) for the last N minutes.

`ATR_FLOOR` is best set relative to the framework, not as a fixed point value: default it to the **1-day breakeven** `S·σ/√252` (if the typical daily range cannot clear the priced breakeven, the market is structurally too quiet), or as a simple alternative a fixed `% of spot`. Recalibrate as NIFTY's level drifts.

If any AVOID trigger fires → **stop. Do not evaluate B or C.**

### GATE B — MAGNITUDE EDGE: REALIZED vs BREAKEVEN + COSTS
- `Expected_Realized_Move` over the holding window (Section 4.1).
- `ΔS_BE` (Section 2) over the same window.
- **AVOID** if `Expected_Realized_Move ≤ (ΔS_BE + Costs_in_points) × BUFFER`.

Convexity cannot pay for decay + frictions → no trade. (Note this gate effectively requires recent realized vol to exceed implied vol with margin.)

### GATE C — VEGA / LEVEL RISK (secondary)
Not a re-test of cheap-vs-expensive (Gate B already encodes that). This gate asks: *even if the move is sufficient, is the implied vol so rich that an IV mean-reversion crushes gamma gains the moment spot stalls?*

- **AVOID** if India VIX rank is rich (`VIX_RANK_HIGH`) → vega-crush risk dominates.
- **Tailwind (confidence up)** if `IV < HV(20)` **and** VIX rank is low → you are buying vol cheaper than recently realized.
- **CAUTION (size down)** if vol is mid-range and not clearly cheap.

### RESULT
`ENTER` only if A passes, B passes, and C is favorable/neutral. Otherwise `AVOID` or `CAUTION`.

---

## 4. SUPPORTING CALCULATIONS

### 4.1 Expected realized move over the window
```
Expected_Realized_Move = ( HV(20) · S / √252 ) · √(hold_days)
```
A timeframe-appropriate ATR may substitute for the realized estimate.

### 4.2 Straddle cross-check (sanity only, not a gate)
```
Straddle_Implied_Move = ATM_Straddle × √(hold_days / DTE)
```
Compare against ΔS_BE. If the two **diverge by more than ~20%**, flag a model/data inconsistency (skew, stale IV, bad quote) and investigate before trading — do **not** trade on the divergence itself. *(The straddle ≈ the expected absolute move ≈ 0.8 × 1-SD, the √(2/π) factor; treat "straddle = expected move" as a heuristic, ΔS_BE as the exact hurdle.)*

### 4.3 Theta bleed — for sizing and exits (not for the go/no-go)
ΔS_BE already embeds theta, so decay is not subtracted in the gates. Estimate explicit bleed only to **size the position and set exits** — i.e. how much premium evaporates if your timing is wrong. ATM time value ≈ `C·√τ`, so:

- **Multi-day holds (DTE > 1):** theta is near-constant → linear `|Θ_day| × hold_days` is acceptable.
- **Expiry day (0DTE):** decay is non-linear on the session clock. Holding from `τ₁` to `τ₂` hours-to-close:
  ```
  Bleed ≈ TimeValue × ( 1 − √(τ₂ / τ₁) )
  ```
  Enter at open (6.25h left), exit +1h: ≈ 8% of remaining time value. Enter with 1h left, hold to 15 min: ≈ 50% in 45 minutes. The last ~30–60 minutes is a distinct pin/settlement regime where the smooth model breaks — flag it rather than trust the formula.

---

## 5. SKEW & CE/PE SYMMETRY

NIFTY usually carries a **put skew** (ATM PE often slightly richer IV/theta than ATM CE).
- For bleed/sizing and any conservative AVOID, use the **higher** of CE/PE theta (or their average).
- ATM CE and PE **gamma** are near-identical; either is fine for ΔS_BE.
- Skew only nudges the cost side; it never flips the CE-vs-PE logic. All gates stay direction-neutral.

---

## 6. COSTS

`Costs_in_points` = brokerage + STT (on premium) + exchange charges + GST + stamp + **slippage**, expressed in premium points and added to ΔS_BE in Gate B. On a quiet day this is frequently the margin between a marginal win and a net loss, so it is not optional.

---

## 7. PSEUDOCODE

```python
import math

# ---------- LIVE INPUTS ----------
S            # NIFTY spot
sigma_iv     # ATM IV, annualized decimal (avg of CE,PE; use max for conservative AVOID)
straddle     # ATM CE premium + ATM PE premium
DTE          # days to expiry (calendar)
hold_days    # intended holding window in TRADING-DAY units (1.0 = one full session)
atr5, atr20  # 5- / 20-day ATR of spot (points)
hv20         # 20-day realized vol, annualized decimal
vix_rank     # India VIX percentile / rank (0-100)
roc          # spot rate-of-change over intraday window (%)
costs_pts    # round-trip costs in premium points (incl. slippage)

# ---------- HURDLE (from IV; exact for ATM) ----------
dt_years = hold_days / 252.0
be_move  = S * sigma_iv * math.sqrt(dt_years)          # = 1-SD implied move = gamma-theta breakeven

# ---------- EXPECTED REALIZED MOVE (same window) ----------
exp_real_move = (hv20 * S / math.sqrt(252.0)) * math.sqrt(hold_days)

# ---------- ATR floor tied to the framework ----------
be_move_daily = S * sigma_iv / math.sqrt(252.0)        # 1-day breakeven
atr_floor     = be_move_daily                          # or: BAND_PCT_FLOOR * S

# ================= GATE A: PRICE-ACTION VETO (primary) =================
if (atr5 < atr20 and atr5 < atr_floor) or (roc < ROC_MIN):
    decision = "AVOID"
elif atr5 < atr20:                                     # contracting but still alive
    decision = "CAUTION"
else:
    # ============= GATE B: MAGNITUDE EDGE =============
    if exp_real_move <= (be_move + costs_pts) * BUFFER:
        decision = "AVOID"
    else:
        # ============= GATE C: VEGA / LEVEL RISK =============
        if vix_rank > VIX_RANK_HIGH:
            decision = "AVOID"                          # rich vol -> vega-crush risk
        elif (sigma_iv < hv20) and (vix_rank < VIX_RANK_LOW):
            decision = "ENTER"                          # cheap vol tailwind
        elif vix_rank > VIX_RANK_MID:
            decision = "CAUTION"
        else:
            decision = "ENTER"

# ---------- Straddle cross-check (sanity flag, not a gate) ----------
straddle_move = straddle * math.sqrt(hold_days / DTE)
if abs(straddle_move - be_move) / be_move > 0.20:
    flag = "MODEL/DATA INCONSISTENCY — investigate IV/quote/skew before acting"

# ---------- Theta bleed for SIZING/EXITS only (DTE<=1 uses sqrt-tau) ----------
def theta_bleed(theta_day, time_value, hold_days, DTE,
                hours_to_close_entry, session_hours=6.25):
    if DTE > 1:
        return abs(theta_day) * hold_days
    tau1 = hours_to_close_entry
    tau2 = max(tau1 - hold_days * session_hours, 0.0)
    return time_value * (1.0 - math.sqrt(tau2 / tau1))
```

---

## 8. CALIBRATION PARAMETERS (starting points — FIT TO THE TRADER'S OWN LOG)

Tune against historical fills: label each past trade win/loss, then choose thresholds that best separate winners from losers (including slippage).

| Parameter | Starting value | Meaning |
|---|---|---|
| `ROC_MIN` | ~0.15–0.25% / 15 min | Min intraday motion to not be "drift" |
| `ATR_FLOOR` | `S·σ/√252` (1-day breakeven) | Absolute "still alive" floor for Gate A |
| `BAND_PCT` | ~0.4% of spot | Pin / tight-band detection |
| `BUFFER` | 1.1–1.2 | Margin by which realized must beat breakeven |
| `VIX_RANK_HIGH` | ~75 | Rich vol → vega-crush veto |
| `VIX_RANK_MID` | ~50 | Above this + not cheap → caution |
| `VIX_RANK_LOW` | ~35 | Below this + IV<HV → cheap-vol tailwind |
| `costs_pts` | broker-specific | Round-trip frictions incl. slippage |

---

## 9. OPERATING ASSUMPTIONS & BOUNDARIES

The spec is valid within these bounds; outside them, treat outputs as approximate.

1. **Realized history proxies forward vol.** `HV(20)` is backward-looking and blind to scheduled catalysts. Overlay an **event calendar** — RBI, US data prints, expiry mechanics, and global/geopolitical shocks (crude, conflict) — so a quiet HV ahead of a known catalyst can still justify a cheap-vol long that the spread alone would miss.
2. **Black–Scholes idealization.** `ΔS_BE = S·σ·√Δt` and the `√τ` bleed model assume constant IV, no jumps, and the option staying ATM. Gaps, IV regime shifts, and the option drifting off ATM break these.
3. **Expiry final hour is its own regime.** Pin risk and discrete settlement invalidate smooth-decay modeling in roughly the last 30–60 minutes; flag this window rather than trust the formulas.
4. **No directional alpha here.** This is a go/no-go on whether movement clears the breakeven, not a CE-vs-PE chooser. Pair it with a separate directional/structural model to pick the side.
5. **Validity depends on calibration.** Section 8 values are placeholders. The spec is only as good as its fit to the trader's real fills, including slippage. Backtest before live reliance.

---

## 10. ONE-LINE SUMMARY

> An ATM option buyer profits only when REALIZED move exceeds the IMPLIED breakeven `ΔS_BE = S·σ·√Δt` — which for an ATM option is simultaneously the gamma–theta breakeven and the 1-SD implied move, with theta already inside it. Gate trades in strict order: (A) a composite ATR/ROC price-action veto for dead ranges overrides everything, distinguishing a dead market from a merely contracting one; (B) refuse unless expected realized move clears `ΔS_BE` plus costs; (C) only then, avoid richly-priced vol (vega-crush risk) and favor entries where IV is cheap versus realized. Use the straddle only as a sanity cross-check, reserve `√τ` decay for sizing and exits, take the conservative CE/PE theta for skew, net out costs, and calibrate every threshold to the trader's own log. All rules are symmetric for ATM CE and PE.
