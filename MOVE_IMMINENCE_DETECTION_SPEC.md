# MOVE-IMMINENCE DETECTION & ENTRY-TIMING SPEC — ATM NIFTY OPTION ENTRIES

> **Task.** Predict WHEN a NIFTY expansion is about to begin, enter a long ATM option (CE or PE) **at the cusp of the move**, and **cut the trade fast if the move fails to materialize** — so the buyer is neither chasing a spent move nor bleeding to a stop while waiting.
>
> **Premise.** A long ATM buyer loses in slow, range-bound conditions where spot and premium barely move. This spec treats that quiet state as the **loaded spring** from which the next move launches — and treats a stalled entry as something to **abandon immediately**, not nurse.
>
> **Two failures this spec is built to prevent (read first):**
> 1. **LATE ENTRY** — entering after the move is mostly spent, so the remaining travel can't clear cost. Prevented by the **Freshness gate (§5)**.
> 2. **STALL-TO-STOP** — entering, then waiting through chop and theta until a wide stop is hit. Prevented by the **Invalidation & Fast-Exit package (§6)**.
>
> **Market.** NSE / NIFTY. Session 09:15–15:30 IST (**6.25 trading hours**). Weekly expiry **Tuesday** (shifts to **Monday** if Tuesday is a holiday). 0DTE = expiry day.
>
> **Hard rule for the implementer.** An entry signal is **invalid unless emitted together with its freshness check, invalidation level, time-stop, and required follow-through**. The system must never open a position from the imminence score alone.

---

## 0. CENTRAL THESIS — COMPRESSION PRECEDES EXPANSION, AND BAD TRADES MUST DIE FAST

Volatility is mean-reverting and cyclical: quiet coils, then releases. The move you want is preceded by measurable **compression** and fires on a **trigger**. But a trigger is a probability, not a promise — so every entry carries, from the first second, the precise conditions under which it is **wrong** and must be exited.

```
ENTER  ⟺  Compression primed  AND  Trigger near  AND  enough move REMAINS to pay.
HOLD   ⟺  the move keeps confirming (follow-through).
EXIT NOW ⟺  invalidation hit  OR  no follow-through in the window  OR  premium not responding.
```

Compression without a trigger is **ARM**, never ENTER. A fired entry without prompt follow-through is **scratched**, not nursed. These two disciplines together end the slow-market bleed.

---

## 1. THE LAYERS

| Layer | Question | Output |
|---|---|---|
| **Engine 1 — Compression** | Is energy stored? | Score 0–100 |
| **Engine 2 — Trigger** | Is release imminent? | Score 0–100 |
| **Gate — Feasibility + Freshness** | Does enough move REMAIN to pay from here? | Pass / reject |
| **Package — Invalidation & Exit** | How is this trade proven wrong, fast? | Levels + time-stop |

A position opens only when all four are satisfied.

---

## 2. ENGINE 1 — COMPRESSION (stored potential energy)

Score how coiled the market is. **Higher = more primed.** Use **current-regime** measures (short windows / intraday); a violent last month must not mask a dead present.

- **Bollinger Band width percentile** — width in the bottom of its lookback → squeezed. BB contained inside Keltner Channels is the strongest single squeeze flag.
- **ATR percentile** — current ATR low within lookback (intraday ATR intraday, daily ATR swing). *Low*, not merely falling.
- **Short-window realized vol percentile** — HV(5) / intraday realized vol at a low percentile.
- **Implied vol / India VIX percentile** — low → cheap optionality, primed asymmetry.
- **Range-contraction structure** — NR7 / NR4 / stacked inside bars; tightening swings; coiling.
- **Volume dry-up** — recent volume below its moving average → apathy before release.

```
compression_score = Σ wᵢ · signalᵢ      # high when vol/width/range low AND squeeze/NR/dry-up on
```

> High compression = **WATCHLIST**, not buy. Energy loaded; release not started.

---

## 3. ENGINE 2 — TRIGGER (release imminence)

Score how close the release is. **Higher = sooner.**

- **Catalyst proximity** — scheduled event within the horizon (RBI, FOMC/Fed, US CPI/NFP, expiry mechanics, earnings, live crude/geopolitical shock). Score ramps as the clock approaches.
- **Time-of-day window** — score **up** in high-expansion windows (open ~09:15–10:15; expiry-day gamma/close ~13:30–15:30; first 15–30 min post-release); **down** in the mid-session lull (~11:30–13:00).
- **Range-boundary break + volume surge** — price exiting the compression band *with* a volume/range-expansion bar → release at inception. Cleanest "now" trigger.
- **Pre-market / gap signal** — GIFT Nifty gap beyond threshold, large overnight global/crude move, extreme FII/DII positioning → elevated open-session expansion probability.
- **Positioning / OI dislocation** — rapid OI unwind at a key strike, PCR swing, max-pain dislocation, AVWAP break → directional pressure building.

```
trigger_score = Σ vⱼ · signalⱼ
```

---

## 4. FEASIBILITY — DOES A MOVE PAY AT ALL?

For an **ATM** option the volatility breakeven has a closed form (since Θ/Γ = −½·S²·σ²):

```
ΔS_BE = S · σ · √Δt        # spot points; delta-neutral volatility hurdle = 1-SD implied move over the hold
```

`σ` = ATM IV (annualized decimal); `Δt` = hold in years (`hold_days/252`). ΔS_BE is the **direction-neutral, expected-value** hurdle: a buyer with no directional edge profits only if realized movement exceeds it. Convert option costs to spot units before comparing:

```
costs_spot_equiv = costs_premium_points / |option_delta|
```

This gives the baseline "is a move worth taking" test. **§5 sharpens it to the move that REMAINS from your actual entry price**, which is what prevents late entries.

---

## 5. FRESHNESS GATE — DO NOT ENTER LATE  ⟵ prevents Failure #1

A trigger can fire when most of the move is already gone. Entering then means the leftover travel can't clear cost — a structural loss even though the call was "right." The gate measures how much move **remains from the intended entry price** and rejects if too little is left.

Define the **trigger reference price** = the breakout level / the price at the bar the trigger fired. At the moment of intended entry:

```
travel_since_trigger = |entry_price − trigger_reference_price|        # distance already traveled in trade direction
remaining_expected   = max(expected_expansion − travel_since_trigger, 0)
```

**Enter only if the REMAINING move still pays:**

```
freshness_ok = remaining_expected > (ΔS_BE + costs_spot_equiv) · BUFFER
```

Plus two fast pre-checks that reject a chase outright:

- **Chase cap:** reject if `travel_since_trigger > MAX_CHASE_FRACTION · expected_expansion` (e.g. > ⅓ of the move already gone), or equivalently `> MAX_CHASE_ATR · ATR`.
- **Exhaustion / climax guard:** reject if the trigger bar is itself a blow-off — bar range `> EXT_ATR · ATR`, a velocity/RSI spike, or price stretched far from VWAP/AVWAP. Buying the climax is the worst late entry.

> The elegance: by testing the *remaining* move against breakeven, "too late" is enforced by the same cost logic as everything else — if you chased, what's left no longer pays, and the entry auto-rejects.

---

## 6. INVALIDATION & FAST-EXIT PACKAGE — DO NOT BLEED TO A STOP  ⟵ prevents Failure #2

Every `ENTER_EARLY` **must emit these three exits atomically with the entry**. A move is a probability; if it does not confirm promptly, the trade is wrong and is closed **immediately** — you scratch small instead of waiting through chop and theta until a wide stop is hit. This is the direct cure for "waiting and waiting then stop loss."

**(a) Structural invalidation — tight, immediate (this IS the stop).**
The price that proves the break failed — not an arbitrary wide level chop wanders to. Typically the first of:
- a **close back inside** the compression band, or
- a break of the **trigger bar's opposite extreme**, or
- a cross back through the broken boundary by more than a noise buffer.
Exit at once on hit. Because it's defined by the structure that triggered entry, it is naturally **tight**.

**(b) Time-stop / follow-through window — kills the waiting.**
The move must **confirm within `FOLLOW_THROUGH_BARS` (e.g. 2–4 execution bars) or `FOLLOW_THROUGH_MIN` minutes**. Confirmation = continuation: subsequent bars extend in the trade direction, volume sustains, range keeps expanding. If the window passes **without** confirmation — price stalls, volume dies, range re-contracts — **EXIT immediately at market, even if the structural stop was never hit.** No "give it more time."

**(c) Premium-response exit — ties to the original symptom.**
If, within the follow-through window, the option premium has not advanced in your favor by at least `MIN_PREMIUM_RESPONSE` (because realized gamma payoff is absent — the low premium rate-of-change that defines the dead market), **exit.** A move that doesn't move your premium is not your move.

**Exit on the FIRST of (a), (b), (c). Never widen the stop. Never average down. Never wait for price to "come back."**

---

## 7. DECISION STATES

### 🟢 ENTER-EARLY (move imminent, fresh, and armed with exits)
`compression ≥ C_HIGH` AND `trigger ≥ T_HIGH` AND `freshness_ok` AND feasibility favorable.
→ Open the long ATM **and simultaneously register the §6 invalidation, time-stop, and premium-response exits.** Take the side indicated by structure/OI; if direction is genuinely unknown and IV is cheap, a long straddle expresses the expansion.

### ⛔ CHASE-REJECT (right idea, too late)
`compression ≥ C_HIGH` AND `trigger ≥ T_HIGH` but `freshness_ok` is **false** (chase cap / exhaustion / remaining move won't pay).
→ **No entry.** The move is largely spent. Wait for the next compression cycle; do not chase.

### 🟡 ARM / WATCHLIST (loaded, not released)
`compression ≥ C_HIGH` but `trigger < T_HIGH`.
→ **Do not buy** — the slow-market bleed trap. Set alerts on the range boundaries and the catalyst clock; convert to ENTER only when a trigger fires *and* freshness passes.

### 🟠 STAND-BY (mixed)
Trigger rising but compression not yet primed, or feasibility marginal.
→ No anticipatory entry; wait, or take only a confirmed-break entry with the §6 package and tight risk.

### 🔴 STAND-DOWN (avoid)
`compression < C_LOW` (already expanded), OR IV rich with fading momentum, OR feasibility fails.
→ No long ATM.

---

## 8. PSEUDOCODE

```python
import math

# ---------- REGIME & CORE ----------
regime          # "intraday"/"0DTE" or "swing" -> selects intraday vs daily series
S; sigma_iv; opt_delta; hold_days       # hold_days in TRADING-DAY units (1.0 = full session)

# ---------- COMPRESSION INPUTS (intraday series if regime != swing) ----------
bb_width_pctile; squeeze_on; atr_pctile; rv_short_pctile; vix_pctile
nr_count; vol_dryup_ratio

# ---------- TRIGGER INPUTS ----------
mins_to_catalyst; in_expansion_window; in_lull_window
boundary_break; break_volume_surge; gap_signal; oi_dislocation

# ---------- FRESHNESS INPUTS ----------
trigger_reference_price; entry_price; trade_direction
expected_expansion; atr_now; bar_range; stretch_from_vwap   # for chase/exhaustion checks

# ---------- COST ----------
costs_premium_pts

# ===== ENGINE 1: COMPRESSION =====
compression = (
      W_BB*(100-bb_width_pctile) + W_SQ*(100 if squeeze_on else 0)
    + W_ATR*(100-atr_pctile)     + W_RV*(100-rv_short_pctile)
    + W_VIX*(100-vix_pctile)     + W_NR*min(nr_count*25,100)
    + W_VOL*(100 if vol_dryup_ratio < VOL_DRYUP_TH else 0)
) / W_COMP_SUM

# ===== ENGINE 2: TRIGGER =====
catalyst_score = 100*max(0, 1 - mins_to_catalyst/CATALYST_HORIZON_MIN) \
                 if mins_to_catalyst is not None else 0
trigger = (
      V_CAT*catalyst_score
    + V_TOD*(100 if in_expansion_window else (0 if in_lull_window else 40))
    + V_BRK*(100 if (boundary_break and break_volume_surge) else (50 if boundary_break else 0))
    + V_GAP*(100 if gap_signal else 0) + V_OI*(100 if oi_dislocation else 0)
) / V_TRIG_SUM

# ===== FEASIBILITY (on REMAINING move from entry) =====
dt_years         = hold_days/252.0
be_move          = S*sigma_iv*math.sqrt(dt_years)
costs_spot_equiv = costs_premium_pts/max(opt_delta,0.05)
travel           = abs(entry_price - trigger_reference_price)
remaining        = max(expected_expansion - travel, 0)
favorable        = remaining > (be_move + costs_spot_equiv)*BUFFER

# ===== FRESHNESS GATE (Failure #1 guard) =====
chase_ok      = travel <= MAX_CHASE_FRACTION*expected_expansion   # or travel <= MAX_CHASE_ATR*atr_now
not_climax    = (bar_range <= EXT_ATR*atr_now) and (stretch_from_vwap <= MAX_STRETCH)
freshness_ok  = favorable and chase_ok and not_climax

# ===== DECISION =====
if compression < C_LOW or (vix_pctile > VIX_RICH and not boundary_break):
    state = "STAND_DOWN"
elif compression >= C_HIGH and trigger >= T_HIGH and freshness_ok:
    state = "ENTER_EARLY"
    exits = build_exit_package(...)        # MANDATORY -- see below
elif compression >= C_HIGH and trigger >= T_HIGH and not freshness_ok:
    state = "CHASE_REJECT"                 # right idea, too late -> no entry
elif compression >= C_HIGH:
    state = "ARM_WATCHLIST"                # loaded, no trigger -> do NOT buy
else:
    state = "STAND_BY"

# ===== EXIT PACKAGE (Failure #2 guard) -- emitted WITH every entry =====
def build_exit_package(compression_band, trigger_bar, entry_premium, trade_direction):
    return {
      # (a) structural invalidation -- tight, immediate
      "invalidation_price": invalidation_level(compression_band, trigger_bar, trade_direction),
      # (b) time-stop / follow-through
      "follow_through_bars": FOLLOW_THROUGH_BARS,           # e.g. 2-4 execution bars
      "follow_through_min":  FOLLOW_THROUGH_MIN,
      "require_continuation": True,   # new extension bar in direction + volume sustained
      # (c) premium-response
      "min_premium_response": MIN_PREMIUM_RESPONSE,         # premium must advance this much in window
      "entry_premium": entry_premium,
      # rule
      "exit_on": "FIRST_OF[invalidation_hit, no_follow_through_in_window, premium_not_responding]",
      "never": ["widen_stop", "average_down", "wait_for_recovery"],
    }
```

---

## 9. CALIBRATION PARAMETERS (starting points — FIT TO THE TRADER'S OWN LOG)

Tune against historical fills, labelling each past entry as real-expansion / fakeout / late-chase. Calibrate **separately for intraday/0DTE and swing**.

| Parameter | Starting value | Meaning |
|---|---|---|
| `C_HIGH` / `C_LOW` | ~65 / ~35 | Compression primed / absent |
| `T_HIGH` | ~60 | Trigger near enough to act |
| `CATALYST_HORIZON_MIN` | ~120 min | Window over which a catalyst raises score |
| squeeze/contraction "low" band | bottom ~20–30 pctile | BB/ATR/RV/VIX thresholds |
| `VOL_DRYUP_TH` | ~0.75 | Volume < 75% of avg = drying up |
| **`MAX_CHASE_FRACTION`** | **~0.33** | **Reject if >⅓ of move already gone (Failure #1)** |
| **`MAX_CHASE_ATR`** | **~0.5–1.0 × ATR** | **ATR-based chase cap** |
| **`EXT_ATR` / `MAX_STRETCH`** | **~2.0 × ATR / tuned** | **Climax/blow-off rejection** |
| **`FOLLOW_THROUGH_BARS`** | **~2–4 bars** | **Confirm-or-exit window (Failure #2)** |
| **`FOLLOW_THROUGH_MIN`** | **~6–12 min intraday** | **Time-stop if no continuation** |
| **`MIN_PREMIUM_RESPONSE`** | **tuned (e.g. ≥ entry theta for window)** | **Premium must move in favor, else exit** |
| `BUFFER` | 1.1–1.2 | Margin remaining-move must beat breakeven+costs |
| `VIX_RICH` | top ~25 pctile | Rich-vol crush caution |
| weights `W_*`, `V_*` | start equal, then fit | Signal contributions |

---

## 10. OPERATING BOUNDARIES

1. **Compression can persist (false squeeze).** A coil can stay coiled; the volume-surge requirement on breaks and the §6 fast-exit are the guards.
2. **Catalysts can be priced in (buy-the-rumor / IV crush).** A known event can inflate IV beforehand and crush it on release even as spot moves — the long can lose to vega. Weight *post-event realized* expansion; avoid entering at peak pre-event IV.
3. **Realized-vol inputs must be current-regime.** Long-horizon HV lags; for "compressed *now*," use short-window / intraday realized vol.
4. **Timeframes must match the horizon.** Daily ATR/structure for swing; intraday for intraday/0DTE. A daily reading must not gate an intraday entry without intraday confirmation.
5. **0DTE uses remaining session time, not calendar DTE.** Compute time-to-expiry from `hours_to_close/6.25`. The final ~30–60 min is a distinct pin/settlement regime — flag it; the §6 time-stop becomes even tighter there.
6. **Freshness and exits are mandatory, not optional.** The §5 freshness gate and §6 exit package are the two guards the implementer must not drop; without them the model recreates exactly the late-entry and stall-to-stop failures it exists to prevent.
7. **Timing and side-selection scope only.** This does not pick CE vs PE; CE/PE symmetry holds only after a directional model selects the side, and the chosen contract's own IV/delta/spread/theta/liquidity must be used for the final entry and exit calculations.
8. **Validity depends on calibration.** §9 values are placeholders; fit to real fills incl. slippage and fakeouts. Backtest before live reliance.

---

## 11. ONE-LINE SUMMARY

> A move is imminent when **Compression is primed AND a Trigger is near** — but only enter if **enough move REMAINS from your actual entry price to clear `ΔS_BE = S·σ·√Δt` plus costs** (the freshness gate that prevents late entries), and every entry is opened **together with a tight structural invalidation, a 2–4 bar follow-through time-stop, and a premium-response exit** so a stalled trade is scratched immediately rather than nursed into a stop (the fast-exit that prevents stall-to-stop). Compression without a trigger is ARM, never ENTER; a fired entry without prompt follow-through is closed, never widened. Keep intraday and daily timeframes separate, handle 0DTE on the session clock, beware false squeezes and pre-event IV crush, and calibrate every threshold to the trader's own log. Timing and side-selection scope only — pair with a directional model.
