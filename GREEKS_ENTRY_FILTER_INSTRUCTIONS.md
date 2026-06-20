# GREEKS-BASED ENTRY/AVOID INSTRUCTIONS FOR ATM OPTION BUYING

> **Purpose:** Instruct an LLM on how to use option Greeks to decide WHEN to ENTER and WHEN to AVOID a long ATM option trade (CE or PE) on NIFTY, with specific focus on **slow, range-bound markets** where premium decay destroys option buyers.
>
> **Scope:** Direction-neutral. Every rule below must hold equally for ATM CE and ATM PE.

---

## 1. THE CORE PROBLEM YOU ARE SOLVING

The trader is a **long option buyer** (buys ATM CE or PE). The recurring losing condition is:

- Market is **slow / stuck in a narrow range**.
- **Rate of change of spot is low.**
- **Rate of change of premium is low.**
- The position bleeds value even though direction was not wrong — it simply did not move enough, fast enough.

Your job is to identify this dead-market condition **before entry** and flag it as **AVOID**, and to identify genuine movement conditions and flag them as **ENTER**.

---

## 2. THE GREEK HIERARCHY (use in this order)

### 2.1 THETA — the cost that kills you in a slow market
- **What it is:** Daily premium decay. For a long option, theta is always working *against* you.
- **Why it matters here:** In a stuck range, theta is the dominant force. No movement = pure theta bleed.
- **ATM property:** Theta is **highest at ATM** and accelerates into expiry.
- **Symmetry:** Theta is effectively **symmetric for ATM CE and ATM PE** at the same strike/expiry — so this rule is valid for both legs.
- **Use as:** Your **cost benchmark**. Theta is the hurdle the spot move must clear.

### 2.2 GAMMA — your payoff accelerator (entry quality)
- **What it is:** Rate of change of delta. High gamma means premium **accelerates** as spot moves.
- **ATM property:** Gamma is **highest at ATM** — this is exactly why ATM is bought for movement.
- **Why it matters here:** High gamma gives a large premium move per point of spot move — the only way to out-earn theta.
- **Caution:** Gamma is only *potential*. In a dead range nothing moves, so high gamma realizes nothing. Gamma tells you payoff-per-point, NOT whether the move will happen.

### 2.3 VEGA + IMPLIED VOLATILITY (IV) — the regime detector
- **What it is:** Vega = sensitivity to IV. IV (and India VIX) = the market's pricing of expected movement.
- **Why it matters here:** This is the Greek/metric that actually **distinguishes "dead range" from "about to move."**
  - **Low + flat IV / VIX** = market pricing in no movement = classic dead range = **AVOID**.
  - **Rising IV / VIX** = expected range expanding = movement likely = **ENTER candidate**.
- **Symmetry:** IV expansion lifts both ATM CE and PE premiums — valid for both.

### 2.4 DELTA — context only
- ATM delta ≈ 0.5. Not a primary slow-market filter. Use only to confirm the option is genuinely ATM.

---

## 3. THE MASTER DECISION RULE (direction-neutral)

> **Compare the market's EXPECTED MOVE against the THETA you will pay over your holding period.**

- **Expected move proxy = ATM straddle premium** (ATM CE price + ATM PE price). This is the move the market is pricing in, and it is direction-neutral by construction.
- **Cost = total theta** you will pay over your intended holding window.

**Rule:**
- If `Expected Move (straddle-implied) > Theta cost over holding period` → market is pricing in enough movement → **ENTER is viable.**
- If `Theta cost ≥ Expected Move` → range is too dead to buy → **AVOID.**

Because both inputs (straddle premium and ATM theta) are symmetric across CE and PE, this rule applies identically to a CE buy and a PE buy.

---

## 4. ENTER vs AVOID CHECKLIST

### ✅ ENTER (conditions favor the buyer)
- IV / India VIX is **turning up** (not low-and-flat).
- ATM straddle-implied expected move **exceeds** theta cost for the holding period.
- Spot is showing **acceleration** (rising rate of change), not drifting sideways.
- High ATM gamma is present **and** there is realized movement to convert it into premium gain.

### ❌ AVOID (slow-market trap)
- IV / India VIX is **low and flat**.
- Theta cost for the holding period **meets or exceeds** the straddle-implied expected move.
- Premium is **not responding** to small spot ticks (low realized gamma payoff).
- Spot stuck in a narrow range with **low rate of change**.

---

## 5. ONE-LINE SUMMARY FOR THE LLM

> **THETA is the cost, GAMMA is the potential payoff, and IV/VEGA tells you whether the payoff will actually arrive. Avoid buying ATM CE or PE whenever theta decay over the holding period is larger than the ATM straddle-implied expected move, especially when IV/VIX is low and flat. Enter only when expected move clears the theta hurdle and volatility is expanding. All rules are symmetric for ATM CE and PE.**

---

## 6. PRACTICAL NUMERIC FORM (optional implementation hint)

```
theta_cost      = abs(ATM_theta_per_day) * holding_period_in_days
                  (or intraday-scaled theta for same-day exits)

expected_move   = ATM_CE_premium + ATM_PE_premium   # ATM straddle

go_no_go        = "ENTER" if expected_move > theta_cost and IV_trend == "rising"
                  else "AVOID"
```

Apply the same computation regardless of whether the intended trade is a CE or a PE — the filter is direction-neutral.
