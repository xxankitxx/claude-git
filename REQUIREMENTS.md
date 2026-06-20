# MYTHOS — Requirements (the user's, from the start)

A living checklist of everything the user (a manual Nifty options BUYER) has asked
for. Requirements, not status. Update as new ones land.

## Non-negotiables (sacred — never touch)
- [ ] **Paper-only.** No real orders ever (`LIVE_ORDERS=False`); the user executes manually.
- [ ] **Never touch risk management / sizing.** Full-capital: `lots = floor(capital / (premium × 65))`, lot size **65**, ₹100k daily reset.
- [ ] **Binary exit only: −10 or +12.** No scratches, no small-profit banking, no stall-kill/timeout.
- [ ] **A lot of the user's money depends on this — no scope of mistake or oversight.**
- [ ] **Never modify `run7/`.**

## What to trade
- [ ] **Nifty weekly options only**, CE/PE **buying** (never sell). BankNifty/FinNifty/stocks are analysis inputs.
- [ ] **Strikes must be ATM or ITM that will MOVE** — never OTM.
- [ ] **Cheap entries** — buy near the touch-low, a margin of safety below LTP.

## Entries — core philosophy
- [ ] **HATES the voting / multi-evidence gating system.** No minimum grading/thresholds — "they kill all the moves and enter too late."
- [ ] **Entries are LATE** — "when the trade comes, the move is already done." Catch the move *much in advance*, while it forms.
- [ ] **Be smarter at the ARMED stage** — one sharp decision, not waiting for many evidences to line up at once.
- [ ] Identify **strong zones** (OI walls + confirmation), stalk, enter at the **cheapest premium**; premium velocity is THE confirmation tool.
- [ ] **Nifty hunts stops right after entry** → ruthless early, relax the trail once convincingly in profit.
- [ ] Give a trade time (don't bank +12 and re-enter same side); but don't let it sit 10–15 min doing nothing.
- [ ] **Better, more frequent, earlier, right-direction trades** (not random volume).
- [ ] **Why do trades go right against me?** Cheap entry but immediately adverse — diagnose and fix.

## Cross-instrument (asked many times, with anger)
- [ ] **BankNifty + FinNifty + ALL heavyweight stocks must be CO-EQUAL participants in the decision** — check their **OI** and everything, then decide.
- [ ] **Don't trade against the broad tape** — no PE when CEs are running / banks are up.
- [ ] Index sentiment must factor in.
- [ ] Heavyweight order-flow read must not be green while the market falls.

## Battle-tested levels / strong zones
- [ ] **Big bottom panel** tracking option CE/PE (ATM/ITM) **and** the instruments.
- [ ] **Defended lows** — prices buyers keep saving (serious buying) = **strong buying zones**.
- [ ] **Resisted highs** — prices sellers keep capping (serious selling) = **strong selling zones**.
- [ ] For **Nifty, BankNifty, FinNifty, and ALL stocks** — not just Nifty.
- [ ] **Persistent market memory** in external files; remember battle-tested S/R **over sessions** ("feel the nerve of the market").
- [ ] These levels must **help decision-making elsewhere**.

## Commentary
- [ ] **Only the most significant** — very-very-bullish or very-very-bearish. Silence the rest.
- [ ] **It floods with noise and buries the important tells** — fix.
- [ ] **Multi-instrument** (BankNifty/FinNifty/stocks), not Nifty-only.
- [ ] Surface **seller exhaustion / short-covering** (sellers run away → prices rise).

## Prediction / early warning
- [ ] **Predict / catch moves in advance** — warn "the market is about to fall/rise" *while it forms*, **without noise**.

## Real-time & display
- [ ] **Prices must display on time** — no calculation may delay prices or drop ticks (BIGGEST issue).
- [ ] BankNifty/FinNifty must not be **rounded off**.
- [ ] **Trading must not stop after 2:30** (never instructed).
- [ ] CE = red, PE = green; calls left / puts right; position panel leads with **BOUGHT → NOW** large; premium ladder shows **OI + per-strike PCR**; near-ATM (±6) PCR change is the most-valued tell.
- [ ] Plain-language verdicts; no chart-heavy panels unreadable at a glance.
- [ ] Audible, long chime.
- [ ] **WHY THIS TRADE** rationale on every entry.

## Learning, memory & housekeeping
- [ ] **Close the learning loop** — adapt the system over time, not just journal.
- [ ] **Retrospective, multi-horizon learning** — re-check price minutes later vs new conditions.
- [ ] **Archive the day's trades with a command, but keep all learnings.**
- [ ] **Expiry-day caution** — theta kills a flat/slow trade; be far more selective.
- [ ] Keep the expiry date current (e.g., 23rd June).

## Analysis the user wants
- [ ] Analyze each day's trades thoroughly.
- [ ] Explain **why the market moved AND *who* drove it** (which indices/stocks).

## How to work
- [ ] **Be ambitious and invincible; don't wait for input.**
- [ ] Use **multiple agents working collectively**.
- [ ] **Inspect every aspect — no oversight.**
- [ ] **Stop ignoring requirements / no shabby work** — do it, don't defer.
- [ ] **Finish running tasks** — don't leave things spinning for hours.
