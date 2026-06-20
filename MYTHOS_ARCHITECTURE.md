# MYTHOS — Architecture & Behaviour Reference

*A map of what this code actually does, written so an external reviewer can audit the logic without reverse-engineering the structure. Grounded in the source as of 2026-06-20. Where a docstring overclaims, this document states the real behaviour and says so.*

---

## 0. Read this first

MYTHOS is a **paper-only, single-user, intraday options-BUYER co-pilot** for one manual trader of Indian index options. It watches the NIFTY tape through ICICI Breeze APIs, hunts entry zones, and tells the user when and what to buy. It **places no real orders** — the user executes every trade by hand. Its job is to call good CE/PE *buys* at good moments and narrate the position; it never sells options and never manages real money programmatically.

The honest state of the system: it is **operationally sound but not yet profitable**. On five days of real recorded tape the engine's realized expectancy is negative (one catastrophic day at roughly −143 premium points, profit factor ~0.15). The core open problem is the **entry edge** — entries fire after the move is largely spent. Most of the machinery below works; the part that doesn't is *what fires a trade*. A reviewer's attention is best spent there (Section 5 and Section 8), not on the items in Section 2, which are deliberate.

---

## 1. What it is and is not

It is a real-time decision-support engine: ingest tick + OI data, compute a large signal stack, run a per-direction entry state machine, and emit a single CE/PE/NEUTRAL decision plus a rich dashboard. It is **not** an execution system, a portfolio manager, an options *seller*, or a multi-instrument trader. It trades NIFTY only; BankNifty, FinNifty and ~14 heavyweight stocks are consumed as *context* (breadth, lead, sentiment), never traded.

---

## 2. Sacred constraints & deliberate scope — do NOT review these as defects

These are explicit user mandates or settled design decisions. Critiques targeting them are out of context.

- **Exit doctrine is a fixed binary: +12 / −10 premium points, no scratches.** A trade is held until it gains +12 (then a trail locks ≥ +12 once peak ≥ +14) or loses −10. There is deliberately **no stall/timeout kill** — a trade that does neither is held to EOD. This is fixed; do not propose trailing-stop redesigns, scratch exits, wider stops, or time-stops.
- **Sizing is full deployable capital:** `lots = floor(capital / (premium × 65))` (`trader.py:278`), lot size 65, capital reset to ₹100,000 each trading day. A separate app-layer breaker halts new entries and flattens at a 4% day loss (`DAILY_MAX_LOSS_FRAC`). Do not propose position-size models.
- **`LIVE_ORDERS=False` is load-bearing.** The system never transmits orders. Critiques about order management, broker reconciliation, or fill-handling miss that there are no real fills.
- **NIFTY-only trading** is a scope decision, not an omission.
- **No-faith-flip discipline:** every entry-changing experiment (VIS inflection, cross-instrument lead, bank-led entry) ships behind a default-`False` flag and must be proven on recorded tape before going live. The "dead code shipped dark" appearance is intentional risk control — an earlier over-promised flip caused a −73% real-money day.
- **`run7/` is an external reference and is never modified.**

---

## 3. Process model & data flow

### 3.1 The spine

A single orchestrator, `MythosApp` (`app.py`), holds every engine and spawns 5–7 daemon threads. The entrypoint `run_mythos.py` parses `--sim`/`--sim2`, sets the virtual clock speed, starts the app, then runs the FastAPI server (blocking) until Ctrl-C.

The central tick is the **analytics loop at 1 Hz** (`app._analytics_loop`, `app.py:367-736`):

```
futures ticks → FlowStack (CVD, VWAP/AVWAP, 1-min candles → RSI/ATR/SuperTrend/ADX,
                            swings, FuturesOIQuadrant, Kinematics)
option/chain  → OIEngine (per-strike OI EMAs, PCR, walls, S/R zones, max pain)
chain + spot  → VolEngine (per-strike IV, ATM IV, IV rank, straddle, expected move)
                GammaWatch (GEX, gamma heat → LOADING/IGNITING)
                HeavyweightBasket (stock breadth/sentiment)
              → SignalEngine.evaluate()  ──►  Decision (CE/PE/NEUTRAL, allowed?)
                            │
                            ▼
                PaperTrader.try_enter()  ──► pending Trade (limit, then market)
              → ancillary: Commentary.scan, LearningLoop.tick, FallRiskMonitor,
                BattleLines, MarketMemory, flight-recorder frame to SQLite
```

A separate **exit loop at 4 Hz** (`app._exit_loop`, every `EXIT_CHECK_SEC=0.25`) calls `trader.check_exits()` so the binary stop/trail catches premium moves tick-by-tick, decoupled from the 1 Hz entry cadence.

Three REST pollers run on fixed budgets: the NIFTY chain (~90 s), heavyweight chains (round-robin ~20 s each), and VIX/sister indices (~60 s / ~10 s). A maintenance loop (10 s) reconnects the WebSocket and falls back to REST for spot if ticks stall > 10 s.

### 3.2 Threading & safety model

The design is **single-writer-per-engine**. The Breeze WebSocket callback (`feed.on_ticks`) is the only writer of `PriceStore`; the analytics thread is the only writer of the OI/Vol/Flow/Signal/Trader engines. Cross-thread *reads* (the dashboard push, the exit loop) rely on CPython GIL atomicity plus a small number of explicit guards:

- `PriceStore.freeze_core()` returns `(spot, futures, atm, ce_ltp, pe_ltp)` in one consistent read so ATM can't tear mid-derivation.
- Dict-iterating reads that race the chain poller use a 3-retry `dict(...)` snapshot that swallows `RuntimeError: dictionary changed size` (`OIEngine.recompute/oi_divergence/ladder/oi_snapshot`, `feed.merged_oi`).
- Deque-iterating reads use `flow._safe_snap` / `_copy` (retry, `[]` on timeout).
- The position-narration state (`app._heart`) is read-modify-write from two threads, serialized by a module-level `state._HEART_LOCK`.
- `PaperTrader` guards its short critical sections with `self._lock`.

The history that motivates this: on **2026-06-15** a transitive cross-thread deque race dropped ~2,200 pushes and a NaN reached `json.dumps`, freezing the dashboard. The price path was subsequently made lock-free and NaN-sanitized (Section 4.10).

### 3.3 The virtual clock

`clk.py` rebases monotonic and wall-clock time so `--sim2` can run the whole engine at 10× with every timer, cooldown and gate scaling together. At speed 1.0 all clk functions are no-ops, so live behaviour is unchanged.

---

## 4. Subsystems

### 4.1 Config (`config.py`)

The single source of truth: every threshold, cadence and feature flag. Pure constants, read-only after startup, no runtime reconfiguration. Headline values: `LIVE_ORDERS=False`, `STARTING_CAPITAL=100_000`, `LOT_SIZE=65`, `STRIKE_STEP=50`, `NUM_STRIKES=8` (subscribe ATM±8, 34 option feeds), `SL_POINTS=10`, `TARGET_POINTS=12`, `EVIDENCE_NEED=6`. Flags currently ON: `STRIKE_FORCE_ATM_OR_ITM`, `CONSENSUS_GATE_ON`, `KNIFE_GUARD_ON`, `ADAPT_ENTRY_GATE_ON`, `COMMENT_PRIORITY_ON`, `COMMENT_SIGNAL_ONLY`, `SISTER_CHAIN_ON`, `LIVE_ENTRY_CURE`. Flags OFF (experiments, prove-on-tape-first): `VIS_ENTRY_ON`, `CROSS_LEAD_ON`, `BANK_LED_ENTRY_ON`, `EXHAUSTION_PRIOR_SWING_ON`, `DROP_PREMIUM_RISING_VOTE`, `ENTRY_SCORE_CEILING`, `VETO_BOUNCE_IN_TREND`. Known wart: `EXPIRY_OVERRIDE` is a manual date that has caused expired-strike subscription if not maintained.

### 4.2 Feed & persistence (`feed.py`, `store.py`)

`PriceStore` is the lock-free state container (spot, futures, options, sister indices, heavyweights, VIX, liveness counters). `BreezeFeed` manages the WS session, token→symbol resolution, ATM-band subscription with reconnect replay (re-subscribes both the fresh band and all previously held strikes so an open position never stales), and the REST fetchers. `feed.atm_option_age()` is the option-feed liveness probe added after 06-15 (spot can tick while option quotes freeze); it now falls back to the freshest strike within ±2 when the exact ATM isn't quoted yet, to avoid a false "stale" alarm during fast drift. `Store` is a SQLite layer with a dedicated writer thread and a bounded queue that **drops rather than blocks** the analytics thread; it persists candles, IV samples, OI snapshots, trades, and the **flight-recorder frames** (~2 s cadence) that make replay possible. Limitation: frames are 1 Hz-ish snapshots, so intra-frame tick order is lost.

### 4.3 Quant core (`greeks.py`, `vol.py`, `gamma.py`)

Vectorized Black-Scholes pricing, a bisection IV solver, and per-strike greeks. `single_greeks` handles the deep-ITM case where the IV solve fails (premium ≈ intrinsic) by bounding delta to ±0.99 and **zeroing the extrinsic greeks** (gamma/theta/vega) so the display isn't inflated by the fallback vol. `VolEngine` produces ATM IV, IV rank/percentile, straddle, expected move, and EWMA realized vol / variance premium. `GammaWatch` computes dealer GEX and gamma heat and runs a two-tier LOADING→IGNITING state machine for explosion fore-warning (gamma heat feeds the exit gamma-ride; it does **not** gate entries).

### 4.4 Flow & kinematics (`flow.py`)

`FlowStack` bundles NIFTY-futures indicators: 1-min candles, RSI/ATR/SuperTrend/ADX, session VWAP, CVD (tick-rule aggressor proxy with `slope()` and `accelerating()`), Anchored VWAP (re-anchored to day high/low), swing pivots (≥15 pt reversals), and `FuturesOIQuadrant` (price-vs-OI positioning over 180 s). `Kinematics` is a cascaded EMA-of-differences producing velocity/acceleration/jerk for spot, CE, PE and PCR; it differentiates only on Δt ≥ 0.25 s and folds every in-order tick into the level. These feed entry votes (turn-physics, premium acceleration, PCR shift) but gate nothing on their own. Whole stack is single-writer (analytics thread); dashboard reads via `_safe_snap`.

### 4.5 OI engine (`oi_engine.py`)

Per-strike OI tracks with 1/3/5-minute EMA rates, aggregate and near-ATM (±6) PCR with histories, OI-wall detection (strike OI > 2× neighbour), clustered support/resistance zones (top 5 per side), max pain over ATM±10, the `multiframe` wall-firming/cracking panel, `oi_divergence` (price up + call-OI falling = bullish, mirror for bearish), and `ladder`/`oi_snapshot`. NIFTY-only, weekly expiry. Concurrency: the chain poller inserts keys while the analytics thread reads; the snapshot-retry pattern is the only synchronization. Zones and divergence feed the entry brain; PCR/max-pain feed commentary and the display.

### 4.6 Signals — the entry brain (`signals.py`, `consensus_core.py`)

This is where a trade is decided, and the most important subsystem to review.

Per direction (CE and PE) a 5-stage hunt runs: SCANNING → STALKING → ARMED → CONFIRMING → FIRE (`_evaluate_direction`). It selects a zone — a defended **BOUNCE** support/resistance or a broken-wall **BREAK** — and, once price touches the zone, collects **14 "defense" evidences** (`_defense_evidence`, `signals.py:247-403`): zone holding, confirmed pivot, our/their premium velocity, CVD pressure exhaustion, defender OI building, book bias, basket sentiment, unwinding fuel (OI quadrant), AVWAP reclaim, near-ATM PCR shift + acceleration, sister-index agreement, OI divergence, and turn physics (2nd derivative).

The **live FIRE predicate** (`signals.py:1019-1027`) requires *all* of:
1. `ok_count ≥ EVIDENCE_NEED` (6 of 14; raised to 7+ for burned/whipsaw zones),
2. **four hard rails AND-ed:** RAIL#1 pressure (30 s CVD slope sign must flip, not merely decelerate), RAIL#2 turn (confirmed-pivot retracement held), RAIL#3 exhaustion (price fell ≥ `EXHAUSTION_DROP_PTS`=12 into the zone), RAIL#4 cheapness (premium ≤ zone-low + ~4 pts),
3. score within ceiling,
4. sustained ≥ `EVIDENCE_SUSTAIN` (1 s).

This majority-vote-plus-rails gate is the only thing that fires a live trade. It is also the design the user dislikes and the probe (Section 4.11) implicates in lateness — the rails mechanically require the turn to be *visible and confirmed* before firing.

Three **demote-only** gates can veto a FIRE but never cause one: a FLAT-market veto, the cross-instrument LEAD veto (`_lead_vote`, BankNifty disagreement — flag OFF), and the **consensus gate** (`_consensus` + `consensus_core`, flag ON). Consensus fuses four panels — FLOW (CVD/quadrant), BREADTH (sisters + basket + sister-OI PCR), TREND (SuperTrend/VWAP/AVWAP/swings), STRUCTURE (walls/PCR/max-pain) — into a net `C` with a "contested" measure, and demotes a fire when `|C| < 0.30`, contested ≥ 0.55, or `sign(C)` opposes the direction. Note: a prove-first calibrator **does exist** (`calibrate_consensus.py`, `_run_instrumented`), contrary to an earlier claim that it was absent; however the consensus import can fail silently (returns `None`) under replay/sim without a live Breeze feed, leaving the gate inert there.

Two experimental entry paths exist behind OFF flags: **VIS_INFLECTION** (`_vis_inflection`, fire on simultaneous spot+premium 2nd-derivative up-turn, skipping the pivot rail and the sustain) and **BANK_LED_ENTRY** (reduced evidence bar when BankNifty leads early). Both are unproven on tape and default off.

### 4.7 Trader & the binary exit doctrine (`trader.py`)

`select_strike` targets ATM (delta ≈ 0.50–0.55) and, with `STRIKE_FORCE_ATM_OR_ITM` on, walks toward the money until |Δ| ≥ 0.50 or it runs out of the subscribed band, never going OTM; it scans premium ∈ [40, 350], spread ≤ 2, with an OI-wall penalty. `try_enter` sizes full-capital (`lots = floor(capital/(limit×65))`), places a **cheaper-entry resting limit** at `LTP − offset`, and creates a pending trade. `_handle_pending` waits up to `ENTRY_LIMIT_WAIT_SEC`=5 s for a fill below the limit, else takes market; it applies a **knife guard** (hold the cheap fill if our-side premium is in free-fall > 0.6 pts/s *and* the thesis turned) and a **live delta re-check** at fill (lapse the order if delta drifted < 0.50).

The exit ladder (`_check_one`, every 0.25 s) is the **binary doctrine**: hard −10 SL checked first and always; a 30 s blind hold (SL only) unless +12 is reached; then once peak ≥ +14 a trail locks ≥ +12 and a tiered chandelier follows the peak. There is **no stall kill**. `_close` instruments each exit as a normal stop/trail, a SAFETY_EXIT (stale-quote/EOD), a GAP_THROUGH (earned +14 then gapped), or a DOCTRINE_BREACH counter that must stay zero. Asymmetric cooldowns: 90 s after a win, 15 s after a loss. One position at a time.

### 4.8 Display-only awareness (`risk.py`, `levels.py`, `memory.py`, `heavyweights.py`, `radar.py`)

Five subsystems feed the dashboard and **change no trade decision** (import-guarded: `signals.py`/`trader.py` never import them). `FallRiskMonitor` fuses breadth/CVD/quadrant/structure/vol-PCR into a 0–100 fall/rip score with a rising flag and leading/coincident/lagging driver labels. `BattleLines` tracks defended floors and resisted ceilings (zigzag pivots, strength = test count) across option premiums, indices and stocks. `MarketMemory` persists cross-session S/R zones and 4-instrument "poise" to human-readable JSON via a daemon writer (decays untouched zones ~8%/day). `HeavyweightBasket` scores per-stock bias (intraday move + PCR + wall proximity) into a 0–100 sentiment and constituent-implied index S/R — this *is* read by signals/risk as the breadth/sentiment input. `OIRadar`/`BookRadar` emit ranked, deduplicated OI/volume/book-pressure events for the dashboard feed.

That much of the user's stated requirements (zones must *help decisions*, persistent memory) is currently display-only is a real open gap (Section 8).

### 4.9 Commentary & learning (`commentary.py`, `learning.py`, `audio.py`)

`Commentary` fires market tells in three priority tiers — CRITICAL (seller exhaustion, fall/rip, gamma ignition, cross-index) bypass the budget; ROUTINE chatter is rate-limited; under `COMMENT_SIGNAL_ONLY` only the very-bullish/very-bearish tells show. `LearningLoop` closes a post-exit loop: it watches the option for 5 minutes, grades each trade (BOOKED_EARLY / HELD_LOSER / CLEAN_WIN / CLEAN_LOSS / GREY), journals verdicts, and runs a per-(direction, zone) EMA "trust" model plus a global "book brake" that **raises the entry evidence bar** on failing contexts (asymmetric — it can only tighten, never loosen). It is wired into the entry gate (`app.py:594-606`). Honest limitation: an honest −10 with intact thesis and any SAFETY_EXIT teach nothing, so a large fraction of trades don't train the model, and it only ever adapts the *entry bar*, never the exit. Audio is non-blocking pygame playback.

### 4.10 State / server / dashboard (`state.py`, `server.py`, `static/app.js`)

`build_state` assembles the full ~30-key JSON tree (read-only, never raises, caches `_last_good_state`). The real-time path is **two-tier and decoupled**: the WS coroutine (`server.py`) pushes a tiny lock-free `kind:"price"` frame every `PRICE_PUSH_MS`=200 ms and harvests the heavy `build_state` from a background thread-task, pushing the full `kind:"full"` tree when ready — so a slow analytics build can never delay prices. `_f` coerces NaN/Inf to 0.0 and `_safe_dumps(allow_nan=False)` refuses any poison frame, both closing the 06-15 freeze. The position **HEART** is a 3-slot hysteresis narrator (mood / why / event-flash) guarded by `_HEART_LOCK`. The browser keeps the last full tree, patches price frames onto it, renders 18 panels under one try/catch, guards against stacked sockets, and runs a 6 s watchdog that forces a reconnect on a silently half-open socket. Dashboard state gates no trading logic — it is purely outbound.

### 4.11 Sim / replay / evidence tooling (`sim_feed.py`, `replay.py`, `entry_edge_probe.py`, `conditional_edge.py`, `calibrate_consensus.py`, `forensic_entry.py`, `archive.py`)

`RealisticSimFeed` generates a synthetic tape with Markov regimes, OI walls, S/R causality and leverage-driven vol — useful for verifying *mechanics*, not edge. `replay.py` drives the **real** engines over recorded flight-recorder frames with the virtual clock; `replay._run(frames, overrides)` reproduces a day's trades and supports config A/B via `--set KEY=VAL` (it reproduces the −143.2/19-trade 06-16 baseline, which is the faithfulness gate). The **evidence probes** (`entry_edge_probe.py`, `conditional_edge.py`) label the forward +12/−10 outcome of a hypothetical ATM entry per bar and bucket by signal — but they currently carry a **CONFOUNDS header and their conclusions are RETRACTED**: direction-contamination in leaning-side selection, exhaustion zone-survivor bias, LTP-not-ASK entry cost, and a trend two-pointer without staleness tolerance. They are engine-drive-faithful but their per-signal *edge* numbers are not trustworthy until rebuilt. `calibrate_consensus.py` exists and snapshots panel votes vs fused consensus at entry to report winner/loser separation. These are analysis tools, not engine code.

---

## 5. The entry decision pipeline, in one place

```
1 Hz: freeze prices → update premium/kinematics → mark wall breaks
      → evaluate CE hunt + PE hunt (zone select, evidence collect, rails)
      → time gate (no entries outside window)
      → FLAT veto (dead tape)
      → CROSS_LEAD veto         [flag OFF]
      → CONSENSUS veto          [flag ON, demote-only, inert under replay]
      → choose FIRE: bounce first, break fallback, quote-freshness gate
LIVE FIRE iff:  ok_count ≥ 6/14  AND  pressure-flip  AND  pivot-held
                AND  fell ≥12 into zone  AND  premium ≤ low+~4  AND  sustained ≥1s
ALT fire paths: VIS inflection [OFF], bank-led [OFF]
→ trader.try_enter: ATM/ITM strike, cheaper-limit, full-capital size
→ _handle_pending: fill <limit within 5s (knife-guard + delta re-check) else market
```

The thing to scrutinize: every live fire requires the turn to be confirmed and held, which structurally enters *after* the inflection. The probe (confounds notwithstanding) and the realized −143 day both point at this lateness as the root cause of negative expectancy. The move-imminence spec's "freshness gate" (don't enter when the remaining move can't clear cost) is the intended cure and is not yet implemented.

---

## 6. The exit doctrine, in one place

Hard −10 (checked first, never ratchets) · 30 s blind hold (SL only) · escape early if +12 reached · once peak ≥ +14 lock ≥ +12 then tiered chandelier trail · **no stall kill** · EOD flatten 15:25 · SAFETY_EXIT only for stale-quote/EOD · DOCTRINE_BREACH counter must stay 0. This is fixed and out of scope for redesign.

---

## 7. Concurrency & failure-safety summary

Single-writer-per-engine, GIL-atomic reads, snapshot-retry on the two dict/deque races that can throw, one lock for the shared heart state, one lock for the trader. Ancillary subsystems (commentary, learning, memory, risk, battle-lines, radar) are wrapped so a failure there degrades silently and **never aborts the decision loop or drops a trade**. The dashboard price path is lock-free and NaN-proofed. The known residual risks are catalogued honestly in each subsystem's limitations and in CRITICISM.md.

---

## 8. Known limitations & open issues (the honest part)

The authoritative, continuously-updated list is **`CRITICISM.md`** (a prior 20-agent adversarial teardown, with per-item fix status). The big-ticket open items a reviewer will and should land on:

- **The negative entry edge / the voting gate is the only live entry path.** This is the central unsolved problem. The single-decision replacements are built but flag-OFF pending tape proof.
- **The rails guarantee post-inflection entry** — earliness is structurally absent in the live path.
- **Cross-instrument "co-equality" is partial:** the consensus gate is demote-only and can be inert under replay; sister/stock OI is thin; FinNifty is under-weighted.
- **Zones, persistent memory, and the fall-risk monitor are display-only** — they inform the user but gate no decision.
- **The learning loop adapts only the entry bar, asymmetrically, on sparse data** — its real-world effect is small.
- **Operational warts:** the manual `EXPIRY_OVERRIDE`, the ~20 s sister-chain staleness, the SQLite drop-on-overflow, and the lack of HTTPS/wss handling for non-localhost deploys (the last now fixed).
- **The evidence probe's per-signal conclusions are retracted** pending a rebuild that removes four confounds (Section 4.11).

What is *not* a defect: everything in Section 2.

---

## 9. How to run, test, replay

- **Live/paper:** `python run_mythos.py` (needs Breeze credentials; `LIVE_ORDERS` stays False). Dashboard on the configured host/port.
- **Accelerated sim:** `python run_mythos.py --sim2` (10× virtual clock, synthetic tape).
- **Preflight:** `python preflight.py` (Monday connectivity GO/NO-GO).
- **Replay a recorded day:** `python replay.py <YYYY-MM-DD>` (lists days with no arg); A/B a config with `python replay.py <day> --set EVIDENCE_NEED=4`.
- **Tests:** `python -m pytest -q` (money-path invariants — exit doctrine, sizing, sacred constants `LOT_SIZE==65`/`SL==10`/`TARGET==12`, the race-safety and kinematics regressions; currently 47 passing).

---

## 10. File map

Engine (`mythos/`): `app.py` orchestrator · `config.py` constants · `feed.py`+`store.py` data/persistence · `greeks.py`/`vol.py`/`gamma.py` quant core · `flow.py` indicators+kinematics · `oi_engine.py` OI · `signals.py`+`consensus_core.py` entry brain · `trader.py` paper trader + exit doctrine · `risk.py`/`levels.py`/`memory.py`/`heavyweights.py`/`radar.py` display-only awareness · `commentary.py`/`learning.py`/`audio.py` narration+adaptation · `state.py`/`server.py`/`static/*` dashboard · `sim_feed.py` synthetic tape · `clk.py` virtual clock · `archive.py` daily export.

Root tooling: `run_mythos.py` launcher · `preflight.py`/`lifecycle.py` ops · `replay.py` backtest/A-B · `entry_edge_probe.py`/`conditional_edge.py`/`calibrate_consensus.py`/`forensic_entry.py`/`sim_stats.py` evidence (analysis only).

Docs: `CRITICISM.md` (open-issue ledger), `REQUIREMENTS.md`, `MOVE_IMMINENCE_DETECTION_SPEC.md` (the intended entry-timing cure), `MYTHOS_TRADING_RULES.md`.
