# MYTHOS — When It Trades, and When It Doesn't

This is the exact, code-accurate specification of every condition that must hold
for MYTHOS to enter a trade, and every condition that makes it refuse. It is
generated from the live code (`mythos/signals.py`, `mythos/trader.py`,
`mythos/config.py`) — if the code changes, update this.

The engine re-evaluates **once per second**. An entry fires only when **every**
gate below passes **in the same second**. If any one fails, no trade.

---

## The decision pipeline (in order)

```
1. DATA      spot & ATM strike present?            else → "WAITING FOR DATA"
2. TIME      inside the trading window?            else → time veto (below)
3. DIRECTION does CE or PE reach the FIRE state?   else → no trade (still hunting)
4. REGIME    is the market NOT flat?               else → "FLAT — refusing entries"
5. QUOTE     is the option quote fresh (<15s)?     else → "STALE OPTION QUOTE"
6. TRADER    position/cooldown/risk gates clear?   else → trader veto (below)
→ ENTER
```

---

## 1. WHEN IT ENTERS — all of these must be true

### A. Time window (`_time_gate`)
- Market open: **09:15–15:30 IST**.
- **Not** the first **5 minutes** (09:15–09:20) — opening volatility.
- **Not** after **14:30** — no new positions late (only manages open ones).

### B. The market is not FLAT (`market_state`)
Needs ≥ ~60s of price history first. Blocked as **FLAT** only when *all* of:
- 15-min range < **0.10%** of spot (`FLAT_RANGE_PCT`), **and**
- ADX < **18** (`FLAT_ADX_MAX`), **and**
- |CVD slope(60s)| < **120** (`FLAT_CVD_SLOPE`).
(If narrow but OI is building → **COILING**, entries still allowed — breakout watch.)

### C. A direction reaches FIRE — the heart of it
For **CE** (mirror for **PE**), MYTHOS hunts one of two archetypes:

**BOUNCE (primary — buy the reversal at a defended zone):** ALL of —
1. **A qualifying support zone** at/below spot: an OI wall (strength ≥ `ZONE_MIN_STRENGTH` 0.50), a swing-pivot low, or a role-flipped level — on the **correct side** of spot (within `ZONE_SIDE_TOL` 5 pts; CE never fires at a level above spot).
2. **Price has touched** the zone (within `ZONE_BAND` 20 pts).
3. **≥ 6 of 14 evidences** fire (`EVIDENCE_NEED`) — "most factors favour it".
4. **Mandatory rail — Cheapness:** the option premium is within **+4 pts** (`CHEAP_CAP_PTS`) of its lowest price since the zone touch (you buy near the bottom, not after the move).
5. **Mandatory rail — Pressure stopped:** the 30-second CVD slope sign has **flipped** (selling has actually stopped, not just slowed).
6. **Mandatory rail — Confirmed turn:** price has retraced **≥ 1.5 pts** (`TURN_CONFIRM_PTS`) off the low **and held** for **≥ 2s** (`TURN_HOLD_SEC`) — a real higher-low printed, not a 1-tick blip.
7. **Mandatory rail — Exhaustion:** price actually **fell ≥ 12 pts** (`EXHAUSTION_DROP_PTS`) *into* the support before turning — a genuine reversal, not a shallow pull-back in an up-move.
8. **Sustained** for `EVIDENCE_SUSTAIN` (1s; the held pivot is itself the multi-second confirmation).

**BREAK (secondary — buy the breakout):**
- An opposing wall (e.g. a resistance for a CE) is **snapped**: spot is **10–30 pts** beyond it (`BREAK_BEYOND`..`BREAK_CHASE_MAX`), the wall was strong, and **≥ 3 of 5** thrust evidences fire (`BREAK_THRUST_NEED`), sustained 2s.

If **both** CE and PE somehow qualify, **BOUNCE fires before BREAK**, and the cheaper/closer setup wins.

### D. The option quote is fresh
The strike it would buy must have quoted within **15 s** (else "STALE OPTION QUOTE").

### E. Trader gates (`try_enter`) — all clear
- **No position already open** (`MAX_OPEN` = 1).
- **Not in cooldown:** 15s after a loss, **90s after a win** (don't chase a spent move).
- **Daily loss < 4%** (`DAILY_MAX_LOSS_FRAC`) — circuit breaker not tripped.
- **Fewer than 8 trades today** (`MAX_ENTRIES_PER_DAY`).
- **Premium in ₹40–₹350** (`MIN_PREMIUM`..`MAX_PREMIUM`) — no lottery tickets, no capital hogs.
- **Risk sizing yields ≥ 1 lot** (≤ 3% of capital at risk).
- The zone isn't **burned** (a zone that stopped you out today demands overwhelming 7/14 evidence), and you're not in a **whipsaw** cool-off (2 stops in 10 min).

---

## 2. WHEN IT REFUSES — every block reason

| Block reason | Meaning |
|---|---|
| `WAITING FOR DATA` | No spot / ATM yet (feed warming up). |
| `PRE-MARKET` / `MARKET CLOSED` | Outside 09:15–15:30. |
| `OPENING VOLATILITY (first 5 min)` | Too noisy at the open. |
| `NO NEW ENTRIES AFTER 14:30` | Late-day; manage only. |
| `FLAT — refusing entries` | Dead tape: narrow range + weak ADX + no flow. |
| (silent, still hunting) | No zone qualified, or evidence < 6, or a mandatory rail failed (not cheap / pressure still falling / no confirmed turn / no exhaustion drop). |
| `STALE OPTION QUOTE` | The strike to buy hasn't quoted in 15s. |
| (trader silent) | Position open, in cooldown, daily-loss cap hit, 8-trade cap hit, premium out of band, or can't size 1 lot. |

**The most common reason for "no trade" is condition C** — a zone is present and
price is near it, but one of the four mandatory rails (cheap / pressure-stopped /
confirmed-turn / exhaustion) isn't satisfied. That is **by design**: MYTHOS only
buys a reversal that has actually fallen into a defended level, stopped selling,
turned, and is still cheap.

---

## 3. The 14 evidences (the "most factors" count, need ≥ 6)

1. Zone holding (no fresh extreme for 15s)
2. Price turning (confirmed pivot — *also a mandatory rail*)
3. Our premium rising
4. Their (opposite) premium stalling
5. Pressure exhausting (CVD) — *the sign-flip is a mandatory rail*
6. Defenders adding OI at the zone
7. Order book favours us (bid/ask stack)
8. Heavyweights agree (Nifty basket sentiment)
9. Unwinding fuel (futures OI quadrant)
10. AVWAP reclaim (trapped-trader lens)
11. ATM±6 PCR shift (put writers stepping in)
12. Bank Nifty / Fin Nifty agree
13. OI-vs-price divergence
14. Turn physics (2nd-derivative deceleration)

---

## 4. Once in a trade — the exit (Profit Rule v4)

**Binary outcome: −10 or ≥ +12.** No small-profit scraps.
- **Hard stop −10** is checked first, always (sized so even a gap stays ≤ 3% of capital).
- **Below +14:** the trade is **held** — only the −10 stop can act. It is given time to run.
- **At peak ≥ +14:** a floor **secures +12** and a chandelier trail lets it **run higher** (protecting ~70% of the peak as it climbs).
- A trade that **neither** reaches +12 **nor** hits −10 and just sits is a **non-runner** — flagged (`STALL KILL`) as a signal the *entry* or the *sim* is wrong, not patched with a scrap exit.
- Hard caps: **15-min** max hold; **daily −4%** force-flatten.

---

## 5. One-line summary

> MYTHOS trades only when, inside market hours and a non-flat tape, price has
> **fallen into a defended support (or broken a wall), stopped selling, printed a
> confirmed turn, and is still cheap** — with ≥6 of 14 factors agreeing — and the
> risk/cooldown gates are clear. Then it holds for **−10 or +12**.
