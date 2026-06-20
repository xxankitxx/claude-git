# MYTHOS — Code Criticism (adversarial teardown vs requirements)

## 1. Executive Summary (blunt)

MYTHOS is not closer to making money — it is closer to having more *features that do nothing*. The shipped engine is, functionally, the pre-task-#37 system: the **6-of-14 voting gate the user explicitly HATES is still the only thing that can fire a trade** (`config.py:340`, `signals.py:1019`), and every cure for lateness, co-equality, and single-decision entry (VIS, BANK-LED, CROSS-LEAD) is behind a default-`False` flag. The two flags actually flipped ON (`SISTER_CHAIN_ON`, `CONSENSUS_GATE_ON`) are the **least validated**, ride on a stub/empty data leg, never went through the project's own calibrate-first gate, and can only ever make the late gate *later* (demote-only). The single biggest problem: **the team has converged on shipping nothing that changes entries**, while the real blocker — a negative entry edge that buys *after* the inflection — is correctly diagnosed (task #28) and left unsolved. The codebase is honest in its comments and brutal in its own memory, but its live config keeps the losing path on and dresses inert/display-only work as progress. Real money rides on a system whose "early warning" cannot make a single trade earlier, whose entries fire after the move is already done, and whose **strike/delta read silently misfires on the very contracts that move** — handing the user the wrong strike.

> NOTE: Risk management is OUT OF SCOPE by the user's mandate — the exit is fixed at **+12 / −10**, sizing is full-capital, and neither the stop, the profit-booking, nor any "risk cap" is to be criticised or changed. Those criticisms have been removed from this document. If the read is correct, the +12/−10 takes care of the rest.

---

## 2. TOP 10 CRITICAL ISSUES (ranked, deduped)

### #1 — The hated voting gate is still the ONLY live entry path
**Problem:** The user's defining requirement ("ONE sharp decision at the zone, no multi-evidence voting") is met by zero shipped code. The live fire predicate is `view.ok_count >= 6 of 14 votes AND score_ok AND cheap AND pressure_ok AND turn_ok AND exhaustion_ok`.
**Evidence:** `signals.py:1019`; `config.py:340 EVIDENCE_NEED=6`; replacements `VIS_ENTRY_ON=False` (`config.py:478`), `BANK_LED_ENTRY_ON=False` (`config.py:460`).
**Violates:** "HATES the voting/multi-evidence gate; wants ONE sharp decision."
**Fix:** Make VIS the primary path on a positive-edge tape and demote the tally to a confirmation-only fallback — or delete the tally. Stop shipping the cure dark.

### #2 — Greeks/DELTA are not checked properly → wrong, "stupid" strikes  ✅ FIXED (2026-06-20)
> **Resolved:** deep-ITM now returns a bounded delta (±0.99) instead of None (`greeks.py` single_greeks + `config.FALLBACK_IV`); the toward-money walk is bounded to the subscribed strike band (`trader.py` select_strike); delta is re-validated on the live chain at FILL and the order lapses if it drifted OTM (`trader._handle_pending`). 2 new tests; 38/38 pass. SL/profit/sizing untouched.
**Problem:** The IV→delta solve returns NaN for deep-ITM contracts (premium ≈ intrinsic), so the **most certain movers silently fail the delta check** and are SKIPPED or mis-picked; the delta of the chosen strike is never re-validated against the live chain; and a constant `strike_delta_used` is written even when the conviction branch is dead. The user has repeatedly been handed the wrong strike because the delta read is unreliable.
**Evidence:** `greeks.py:61` (IV solve → NaN at/near intrinsic); `trader.py:216-252` (select_strike delta loop emits a false SKIP); `trader.py:158-162,292` (constant `strike_delta_used` poisons learning).
**Violates:** "ATM/ITM that MOVE, never OTM"; "you never checked greeks/delta properly and gave me stupid trades."
**Fix:** Compute delta robustly for deep-ITM (BS delta directly / bounded, never NaN-skip a real mover); re-validate the chosen strike's delta against the live chain before committing; stop writing a constant delta.

### #3 — The lateness cure is structurally INERT; the rails GUARANTEE post-inflection entry
**Problem:** RAIL#1 (CVD sign-flip) + RAIL#2 (held higher-low for 2s) mechanically require the turn to be *visible and confirmed* before firing — the code's own comment admits "guarantees we never fire while price is still falling." Every "fire earlier" path is flag-OFF.
**Evidence:** `signals.py:944-952` (+ comment 949-951); `CROSS_LEAD_ON=False` (`config.py:425`), `EXHAUSTION_PRIOR_SWING_ON=False` (`config.py:386`).
**Violates:** "Entries are LATE; caught EARLY/at the inflection."
**Fix:** Enable a prior-swing exhaustion anchor and a genuine pre-higher-low inflection fire (VIS), validated on real tape — not the relaxation that the memory already proved inert.

### #4 — The "cheaper entry" limit is a faith-flipped no-op that MANUFACTURES lateness  ⚠ PARTIAL (defensive only; lateness root-cause UNSOLVED)
> **Reframed (teardown 2026-06-20 — my earlier ✅ FIXED was an OVERCLAIM):** the knife guard added is a real, safe DEFENSIVE measure — on the cheap-dip branch it HOLDS a fill that is a genuine knife (premium free-fall `prem_vel ≤ −KNIFE_PREM_VEL` AND thesis turned). But it does NOT cure the stated root cause of #4. That root cause is WINNERS bought late: on a winner the premium is RISING, so it never dips to the limit, never enters the knife-guard branch, and still market-fills at the 5 s window end — unchanged. The knife guard only governs the falling-premium (loser) case. So #4 is addressed defensively, not as the "lateness cure" I labelled it. Replay A/B was byte-identical (no genuine knife-fills on 1 Hz tape) → benefit unproven on tape. The real winner-lateness fix is an inflection/freshness-gated entry (move-imminence spec §5), still task #28. New `KNIFE_GUARD_ON/_PREM_VEL/_MIN_SCORE` config + 1 test (proves it doesn't break existing fills). SL/profit/sizing untouched.
**Problem:** The buy-limit at `ltp − off` only fills if premium ticks DOWN within 5s; otherwise it market-buys late. On winners (premium rising) it never gets the discount and pays market 0–5s later; on losers (premium falling) it fills cheap — i.e. it buys the failures at a discount and the winners late. This is the literal mechanism of "cheap entry but immediately adverse."
**Evidence:** `trader.py:257-265`, `_handle_pending` `trader.py:345-388` (market at `372-374`); entry clock starts at FILL `trader.py:380`.
**Violates:** "CHEAP entries," "caught EARLY," "why do trades go right against me."
**Fix:** Decide and fill on the same tick (no 5s lag), or re-validate cheapness/zone/turn at fill and abort if adverse — so a "cheap" fill is a real bottom, not a dip into a continuing move.

### #5 — "Co-equal BankNifty/FinNifty/ALL stocks + their OI" is a fiction everywhere
**Problem:** The only live cross-instrument gate (CONSENSUS) is DEMOTE-ONLY — it can veto a bounce but never cause/confirm an entry, and is OFF for BREAK fires. Its BREADTH leg collapses to a single price-sentiment scalar (conf ≤ 0.5, ~0.14 effective weight) on real tapes; FinNifty is near-muted (memory weight 0.10 vs Nifty 0.40); stock OI and sister-index OI are absent from the OI engine, flow stack, battle-lines, and memory.
**Evidence:** `signals.py:736-748` (demote), `CONSENSUS_GATE_BREAK=False` (`config.py:505`); `consensus_core.py:126-187` (breadth degrade); `memory.py:41` (weights); OI engine is Nifty-only (`oi_engine.py`, `app.py:364,440`).
**Violates:** "BankNifty+FinNifty+ALL stocks (their OI too) must be CO-EQUAL."
**Fix:** Build a real per-instrument OI/flow input and a *bidirectional* consensus that can both block and confirm; weight sisters equally or justify the asymmetry with calibration.

### #6 — The shipped consensus gate was flipped ON without RUNNING its calibrator
> **Correction (2026-06-20 doc pass):** the earlier claim "calibrator DOES NOT EXIST" was FACTUALLY WRONG — `calibrate_consensus.py` exists (208 lines) with `_run_instrumented` (`:82-144`) that snapshots panel votes vs fused consensus at entry and reports winner/loser separation. The valid substance remains: there is no evidence it has been RUN on real tape to prove the weights separate winners from losers, the thresholds are still self-described "PRIORS," and `_consensus` swallows an import/compute failure to `None` (`signals.py:530`), so under replay/sim without a live Breeze feed the gate is INERT (demote path dead). So: the tool exists; the proof and the live-path robustness do not.
**Problem:** `CONSENSUS_GATE_ON=True` ships a co-equal veto whose calibration has not been run/documented and which can silently no-op off-live. The BREADTH leg was a constant-50 stub on old replay tapes (`prev_close`/`change_pct` not revived) — `idx_prev` revival (task #37) addresses this on new tapes only.
**Evidence:** `consensus_core.py` (priors); `calibrate_consensus.py:82-144` (exists, run-status unknown); `signals.py:518-551,530` (demote + silent-None); `CONSENSUS_GATE_ON=True`.
**Violates:** "real money depends on this," the no-faith-flip discipline.
**Fix:** RUN `calibrate_consensus.py` on real tape, prove separation, document it; make the import-failure observable instead of a silent `None` — or turn the gate OFF until proven.

### #7 — SuperTrend casing bug silently kills it in BOTH decision-path consumers  ✅ FIXED (2026-06-20)
> **Resolved:** both consumers now normalise the read — `(supertrend.direction or "").lower()` at `app.py:703` (memory bias) and `risk.py:160` (fall/rip early-warning) — so the `"UP"/"DOWN"` emit matches again and the trend leg is live. 39/39 pass.
**Problem:** SuperTrend emits `"UP"`/`"DOWN"`; the memory `nifty_bias` and the risk early-warning compare against lowercase `"up"`/`"down"` and NEVER match. The dashboard uses the raw string correctly, so trend *displays* fine while the *decision* code reading it is permanently dead (a third of `nifty_bias` is always 0).
**Evidence:** `flow.py:191-198` (emits upper); `app.py:704` (`== "up"`); `risk.py:160-163` (`== "down"`).
**Violates:** "co-equal broad tape," "close the learning loop." A one-line bug with real-money consequences.
**Fix:** Normalize casing at the comparison; add a test pinning the three values.

### #8 — Early-warning, battle-lines, and persistent memory are all 100% DISPLAY-ONLY
**Problem:** `risk.py` (fall/rip), `levels.py` (battle-lines), and `memory.py` (zones) each *admit in their own docstrings* they change no trade decision; `trader.py`/`signals.py` never import them. The "strong zones must help decisions" and "persistent memory over sessions" requirements are met by marquee strings. Battle-lines also promises cross-session persistence on-screen while implementing zero save/load.
**Evidence:** `risk.py:14-18`; `levels.py:26` + `config.py:730`; `memory.py:2,13-17`; on-screen "over sessions" `app.js:939`, `index.html:94` with no persistence in `levels.py`.
**Violates:** "ZONES must help decisions; persistent memory over sessions."
**Fix:** Wire at least one zone-memory signal into the entry gate (e.g. discount a FALL warning into a defended multi-session floor) and actually persist battle-lines to disk.

### #9 — Real-time: price overlay is trapped behind a slow `build_state`, and a frozen OPTION feed is invisible  ✅ FIXED (2026-06-20)
> **Resolved (4 pieces, none touch SL/profit/sizing):**
> 1. **Price decoupled from the build.** The WS loop now runs at the FAST price cadence (`PRICE_PUSH_MS=200`) and sends a tiny `kind:"price"` frame (market+health only, lock-free `freeze_core`) EVERY tick. The heavy `build_state` runs in a background thread-task that is never awaited inline; its `kind:"full"` frame goes out when it finishes. A slow tree can no longer delay a price. All sends stay in ONE coroutine → no concurrent-send hazard, no lock (`server.py` ws + `state.price_frame`).
> 2. **Option-feed liveness.** `feed.atm_option_age()` / `option_feed_alive()` report the WORSE of the two ATM legs; `opt_age` is in the health dict (full + price frames). The client shows an **OPT FEED STALE** badge and trips the dead-dot when the option feed stalls while spot still ticks (`feed.py`, `state.py`, `app.js renderHeader`).
> 3. **NaN can't reach the wire.** `_f` coerces non-finite → 0.0; `_safe_dumps(allow_nan=False)` skips a single poison frame instead of emitting invalid JSON that wedges `JSON.parse` and freezes the dashboard (`state.py:_f`, `server.py:_safe_dumps`).
> 4. **Client hardened.** `onmessage` is wrapped (a bad frame skips, never kills the stream); `connect()` has a single-socket guard (no parallel sockets → price can't go backwards); a 6 s staleness watchdog forces a reconnect on a silently half-open socket (`app.js`).
>
> 3 new tests (option liveness, NaN-never-on-wire, price-frame shape); **42/42 pass**. Note: the shared-loop multi-tab `build_state` fan-out (Per-Subsystem Feed item 2) is intentionally NOT addressed — the user runs a single tab.
**Problem:** The "prices never delayed" overlay runs *inside* the same coroutine that just `await`ed a heavy `to_thread(build_state)`, so a slow tree delays the price too — drops from exceptions are covered, latency is not. Separately, `feed_alive` only watches spot/futures; a dead *option* feed (the price actually traded) shows fully live. NaN from `_f` → strict `json.dumps` can silently end all pushes.
**Evidence:** `server.py:46-68` (single loop, per-socket `to_thread`); `feed.py:468-470` (liveness ignores options); `state.py:16-21` (`_f` passes NaN), `server.py:71` (`except: log.debug; return`).
**Violates:** "prices NEVER delayed/dropped (biggest issue)."
**Fix:** Push a tiny price-only frame on a separate fixed-cadence task that never awaits `build_state`; add option-feed liveness + a client staleness watchdog; sanitize NaN before serialization.

---

## 3. Per-Subsystem Findings

### Entry / Voting (`signals.py`)
- **[CRITICAL]** Live gate is the banned 6-of-14 tally (`:1019`); VIS/BANK-LED single-decision paths flag-OFF.
- **[CRITICAL]** RAIL#1+RAIL#2 guarantee post-inflection entry (`:944-952`).
- **[HIGH]** `EVIDENCE_SUSTAIN=1` + touch-seeded `extreme_ts` ⇒ effective confirmation ~2 ticks at 1 Hz (`config.py:344`, `signals.py:885-888`).
- **[HIGH]** `score_ok` ceiling OFF while comments document WR collapses to 0-18% at high evidence — the live gate buys the documented losing cohort (`config.py:379`, `signals.py:989-991`).
- **[HIGH]** Rising-premium vote still counts ("the move already left") — `DROP_PREMIUM_RISING_VOTE=False` (`config.py:376`).
- **[MEDIUM]** BREAK path is least-gated/most-chasing, checked before BOUNCE and `return`s, pre-empting cheap bounces (`:794-830`).
- **[MEDIUM]** Swing/flipped zones get hardcoded 0.55/0.6 "strength" that always passes `ZONE_MIN_STRENGTH=0.50` (`:218-236`).
- **[LOW]** Module docstring claims "no gating system… the 0.70 gate is gone" — false vs `:1019`.

### Cheap-entry (`trader.py` try_enter, `signals.py` cheap/premium_low)
- **[CRITICAL]** Cheaper-limit is a faith-flipped no-op manufacturing lateness (see TOP #4).
- **[CRITICAL]** `premium_low` anchor resets on every zone change ⇒ "cheapest since touch" fabricated from seconds of data (`signals.py:137-151,889,898-899`).
- **[HIGH]** `cheap` vs rising-premium vote are contradictory; FIRE window is a razor 1-2s then thrown away by the 5s wait.
- **[HIGH]** VIS/BANK-LED validate cheapness at decide-time, then market-fill 0-5s later with no re-check (`trader.py:372-374`).
- **[MEDIUM]** `STRIKE_ITM_PUSH_MIN_PREMIUM=80` disables toward-money push on the 40-80 cheap band (`config.py:254`).
- **[MEDIUM]** Cheapness cap RELAXED to +5 on the high-ok_count cohort the code itself flags as losing (`signals.py:908-909`).
- **[MEDIUM]** `bid <= limit` cheap-fill books a price a BUY could not transact at (fills at ask).

### Exit / Sizing / SL / profit-booking — OUT OF SCOPE (user mandate)
- The exit is fixed at **+12 / −10**, sizing is full-capital. Per the user, this is not to be criticised or changed. Removed.

### Strikes & Greeks (`trader.py:select_strike`, `greeks.py`)
- **[CRITICAL]** IV→delta solve returns NaN on deep-ITM (premium ≈ intrinsic) — the most certain movers silently fail and emit false SKIP (`greeks.py:61`, `trader.py:216-252`).
- **[CRITICAL]** 12-step toward-money walk overruns the 8-strike feed subscription → queries never-subscribed strikes → false SKIP blocks entry (`trader.py:210`, `config.py:47`).
- **[HIGH]** "EARLY at the inflection" and cross-instrument zones are absent from strike selection; only a wall-penalty term is zone-aware.
- **[HIGH]** OTM-conviction branch is dead under `STRIKE_FORCE_ATM_OR_ITM=True` but still writes a constant `strike_delta_used`, poisoning the learning loop (`trader.py:158-162,292`).
- **[MEDIUM]** Spread gate falls back to `MAX_SPREAD` (PASS) when bid/ask missing — unquoted book treated as tradeable.
- **[LOW]** `expected_move` threaded but never used.

### Cross-instrument consensus (`consensus_core.py`, `signals.py`)
- **[CRITICAL]** DEMOTE-ONLY; can only delay, never cause/confirm — net-harmful vs the early/co-equal goal (`signals.py:734-743`).
- **[CRITICAL]** BREADTH degrades to a thin price-sentiment scalar; co-equal is fiction (see TOP #6).
- **[HIGH]** Named calibrator does not exist; thresholds unvalidated (see TOP #7).
- **[HIGH]** `idx_hist` mutated from a "pure read" with bare `deque.append`, racing the poller — same deque-race class that already froze prices (`consensus_core.py:146-148`).
- **[HIGH]** OFF for BREAK fires — disabled for exactly the late-chase entries that most need a tape veto (`config.py:505`).
- **[MEDIUM]** Contested-abstain hands ambiguous tapes back to the late Nifty engine; vetoes when least needed.
- **[MEDIUM]** `_consensus` swallows all exceptions to `None` ⇒ silent total bypass, no observability.
- **[MEDIUM]** `gate_pass`/`want_sign` dead in live path; fire rule reimplemented inline — two sources of truth.

### Commentary (`commentary.py`)
- **[CRITICAL]** ✅ FIXED (2026-06-20) — the UI color classifier (`app.js renderTicker.colorOf`) now recognizes the directional CRITICAL tells it previously rendered colorless: `DISTRIBUTION…`→bearish, `ACCUMULATION…`→bullish, `BROAD TAPE TURNING UP/DOWN`→bullish/bearish, `GATE …`/expiry→warn (matched against the actual emitted strings in `risk.py:195-198` and `commentary.py:116-117,374-381`).
- **[CRITICAL]** WHY-THIS-TRADE rationale is the hated voting list surfaced verbatim (`app.py:227-231`).
- **[CRITICAL]** Every tell is lagging/coincident (PCR/CVD/volume/book/cross-mom already-printed) — cannot predict in advance.
- **[HIGH]** Stocks/sister OI and per-strike PCR absent; equity complex collapsed to one 0-100 scalar.
- **[HIGH]** Under `COMMENT_SIGNAL_ONLY=True`, ~250 lines of `scan()` analytics compute then drop every pass; the "valued tells protected" comment is false (PCR/CVD suppressed).
- **[MEDIUM]** `cross_index_oi` kind-collision: veto and "tape turning" share one cooldown bucket and silence each other.
- **[MEDIUM]** `expiry_warn` never fires under signal-only — expiry caution silenced.

### Early-warning (`risk.py`)
- **[CRITICAL]** Entirely display-only — drives no decision (see TOP #9).
- **[CRITICAL]** "RISING" forward flag suppressed during the first ~60s of a move (the inflection) — `_fall_at` returns None (`:98-101,252-259`).
- **[CRITICAL]** `confidence>=0.4` LOUD gate re-implements the hated quorum — a lone overwhelming leading signal stays silent (`:104-105,193`).
- **[HIGH]** CVD-accel test uses raw slopes, not magnitudes; declines to reuse the correct `flow.cvd.accelerating()` (`:136-137`).
- **[HIGH]** `divergence_sigma` is a mean-reverting z-score mislabeled LEADING — fades as the move matures (`:138,262`).
- **[MEDIUM]** `iv_expanding` boosts fall only, never rip — biases a CE/PE buyer bearish.
- **[MEDIUM]** Broad `except → 0.0` makes a blind axis read as "no risk" (blind-as-calm), the dangerous direction.

### Battle-lines (`levels.py`)
- **[CRITICAL]** 100% display-only; never read by trader/signals (`levels.py:26`).
- **[CRITICAL]** Zero cross-session persistence despite on-screen "over sessions" promise (`app.js:939`).
- **[HIGH]** BankNifty/FinNifty get flow-blind bare-touch "defending" badges (`has_flow=False`, `:207-216`) — structurally second-class.
- **[HIGH]** OI entirely absent; built on spoofable top-of-book.
- **[HIGH]** Live `'OTM'` label path against "never OTM" (`:183-185`).
- **[MEDIUM]** Lagging by construction — a level prints only after a full rebound; level price is a drifting centroid, not the edge.

### Memory (`memory.py`)
- **[CRITICAL]** Inert toward decisions; no predictiveness-measurement code exists to ever earn a role (`:2,14-17`).
- **[HIGH]** False load-bearing docstring ("imported ONLY by state.build_state" — actually `app.py:37,142`).
- **[HIGH]** Not co-equal (Nifty 0.40 vs FinNifty 0.10); sister %-change /0.5 vs structural ±1 — incomparable scales (`:41,325-326`).
- **[HIGH]** No per-strike PCR, no option-premium zones; PCR/max_pain observed but never folded (`:234-251`).
- **[MEDIUM]** Day-roll decay races the daemon writer on `_last_day` ⇒ inflated "held N× sessions" counts.
- **[MEDIUM]** "Battle-tested" confidence inflated by mere touches; "launched/stalled theta-trap" built on a sampling artifact.

### Feed / Real-time (`feed.py`, `app.py`, `server.py`, `state.py`)
- **[CRITICAL]** ✅ FIXED — Price overlay no longer trapped behind `build_state`: fast `kind:"price"` frame every 200 ms, heavy build harvested from a background task (see TOP #9).
- **[CRITICAL]** Shared WS loop serializes all tabs; N tabs ⇒ N concurrent `build_state` self-inflicting tick loss. *(Not fixed — single-tab use; the build now runs in a background task so it no longer blocks that tab's price path.)*
- **[HIGH]** `_write_hw` omits `NEW_TICK_EVENT.set()`; event is never `wait()`-ed anywhere — dead freshness primitive, stock ticks second-class.
- **[HIGH]** Heavyweight/sister data is REST round-robined (minutes stale), sister chains default OFF — co-equal in name only.
- **[HIGH]** Futures tick drain + full-chain greeks/GEX run inside the 1 Hz pass — self-inflicts the tick loss the watchdog reports.
- **[MEDIUM]** `merged_oi` retry-then-drop silently discards freshest WS OI; reconnect leaves option quotes stale with no flag.
- **[MEDIUM]** ✅ FIXED — `feed_alive` ignored options; added `atm_option_age()`/`option_feed_alive()` + an OPT-FEED-STALE badge (see TOP #9).

### OI (`oi_engine.py`)
- **[CRITICAL]** ✅ FIXED (2026-06-20) — `oi_divergence()` (entry path) now reads a one-shot `dict(self._tracks)` snapshot under the same 3-retry `RuntimeError` guard `recompute()` uses, and reads EMAs with `.get(mid_w, 0.0)`. The five strikes sum from one consistent view and a future iterating change can't crash a trade evaluation. New `test_oi_divergence_survives_concurrent_poller_mutation` hammers the reader against a key-churning poller thread (`oi_engine.py:371`, was `:388`).
- **[CRITICAL]** BankNifty/FinNifty/stock OI absent from the OI engine entirely — Nifty-only.
- **[HIGH]** ✅ FIXED (2026-06-20) — `multiframe()` (push path) now snapshots each `_strike_hist` deque (`list(hq)` under the retry guard) before iterating, so a concurrent `recompute()` append can't raise "deque mutated during iteration" (`oi_engine.py:172`).
- **[HIGH]** Zone "building" gate uses only the slow 3-min EMA `>0` — a wall bleeding for 2 min still reads "defended" (laxer than its own consumer) (`:264,276`).
- **[HIGH]** Max pain computed over ATM±10 levels only → pins to boundary on trend days, feeds false gravitation.
- **[MEDIUM]** `_strike_pcr` fabricates PCR=2.0 on zero call OI and that manufactures support zones; OI ratchets and never expires on unwind ⇒ stale "defended" floors.
- **[MEDIUM]** `self._snap` and `pcr_flip()` inert with comments asserting wiring that doesn't exist.

### Flow / Kinematics (`flow.py`)
- **[CRITICAL]** SuperTrend casing bug — dead in memory + risk (see TOP #8).
- **[CRITICAL]** ✅ FIXED (2026-06-20) — Kinematics `dt<0.25` early-return dropped sub-0.25s ticks *before* folding them into the smoother, starving the v→a→j lead chain during fast bursts. Now EVERY in-order observation folds into the level and differentiation still happens only on ≥0.25s steps (`_s_mark`/`_ts` track the last derivative step). Pinned **byte-identical to the old formula at the engine's 1 Hz cadence** (where no sub-0.25s ticks occur, so zero change to the live entry votes it feeds) and corruption-free for any future fast path. NOTE: latent, not active — at 1 Hz the old branch was never taken; the value is future-proofing the only caller that could feed it faster. New `test_kinematics_identical_at_1hz_and_no_longer_drops_fast_ticks` (`flow.py:386`).
- **[HIGH]** `accelerating()` collapses to a sign-permissive bool (`s3==0` always passes) on slow 60/180s windows.
- **[HIGH]** Entire flow stack is Nifty-futures-ONLY — no CVD/kinematics/swings/quadrant for sisters or stocks.
- **[HIGH]** FuturesOIQuadrant fires off a single 3-min-ago sample with a 0.1% OI threshold inside tick noise.
- **[MEDIUM]** VWAP/AVWAP fall back to tick-count weighting then are sold as volume-weighted lenses.

### Learning (`learning.py`)
- **[CRITICAL]** "Closed loop" keyed on a 50-pt spot bucket that rarely accrues 4 clean outcomes before decay ⇒ effectively inert (`:89-90,168`, `config.py:826`).
- **[CRITICAL]** Reward ignores the #1 complaint: immediate-adverse −10 with thesis intact returns None (no learning) (`:200-211`).
- **[CRITICAL]** `book_brake` cap is always clearable (`ADAPT_MIN_HEADROOM=1`) — a soft nudge sold as a circuit breaker (`app.py:602-603`).
- **[HIGH]** EMA-relative gating produces zero bumps under uniform negative edge — blind to the user's actual regime.
- **[HIGH]** BOOKED_EARLY (the stated core leak) is a +12 win ⇒ *raises* trust on the very behavior it claims to fix.
- **[MEDIUM]** `time_to_mfe`/`entry_score`/`strike_delta_used` captured, zero consumers.

### Sim / Replay (`replay.py`, `sim_feed.py`)
- **[CRITICAL]** Virtual clock split-brain: replay patches `time.*` and signals/trader `datetime` but not `oi_engine`/`vol`/`flow`/`greeks` — and sim reads via `clk.*`. Expiry/theta/staleness logic untestable (`replay.py:45-51,148-149`).
- **[CRITICAL]** Replays the SAME late voting engine on a sim tuned to be kind to it — cannot surface lateness.
- **[HIGH]** Replay runs `store=None` ⇒ persistent zone memory absent; backtests blind to the feature.
- **[HIGH]** "Old tapes replay identically" is faith-flipped — missing fields default to 0 ⇒ dead CVD/flow, A/B across old/new tapes is apples-to-oranges.
- **[HIGH]** `_dyn_target` swallows all exceptions ⇒ replay scores a degraded policy silently.
- **[MEDIUM]** Sim price pinned to max-pain magnet computed from its own OI ⇒ zone strategies tautologically rewarded; sisters/stocks are beta-shadows ⇒ breadth never diverges, co-equal veto untestable.

### Config (`config.py`)
- **[CRITICAL]** Hated vote is default-live; all earliness flags OFF (see TOP #1).
- **[CRITICAL]** `EXPIRY_OVERRIDE` hardcoded/stale (`:50`) — manual time-bomb that pins time-to-expiry and strike subscription.
- **[HIGH]** "never OTM" is one boolean (`STRIKE_FORCE_ATM_OR_ITM`) away from a fully-wired OTM machine.
- **[MEDIUM]** `LIVE_ENTRY_CURE=True` secretly mutates `ZONE_BAND`/`TURN_CONFIRM_PTS` 50 lines after their literals — hidden control flow, shipped admittedly-unproven.
- **[MEDIUM]** `MAX_ENTRIES_PER_DAY=8` — unrequested hard cap contradicting "enter every valid zone."

### State / Server (`state.py`, `server.py`)
- **[CRITICAL]** ✅ FIXED (2026-06-20) — `app._heart` read-modify-write (slot A/B/C hysteresis) and its flat-reset are now serialized by a module-level `_HEART_LOCK`, so the WS `to_thread` build and the `/api/state` route can no longer tear the dict / double-promote / KeyError (`state.py:_position_heart`, flat-reset). 44/44 pass.
- **[HIGH]** Live overlay refreshes only 4 numbers; the entire co-equal tape/OI/PCR surface silently freezes on a served last-good frame (`:81-112`). *(Mitigated: the full build now runs continuously in a background task, so last-good is served only on a genuine build failure, not on every slow pass; the analytics surface still isn't independently overlaid.)*
- **[HIGH]** ✅ FIXED — NaN/`json.dumps`/blanket-except silently ends all pushes; `_f` sanitizes non-finite and `_safe_dumps(allow_nan=False)` skips a single poison frame (see TOP #9).
- **[MEDIUM]** `recent_trades`/learning/journal read on the push path with no tear guards while memory/risk/battle get `_safe` wrappers — inconsistent defense.
- **[MEDIUM]** `bull_score` blends `ce.ok_count − pe.ok_count` — the hated vote — straight into the headline gauge.

### Dashboard (`app.js`, `index.html`, `style.css`)
- **[CRITICAL]** ✅ MITIGATED — `onmessage` now wraps parse+render in try/catch (a bad frame skips, doesn't wedge the socket); the per-panel try/catch remains, but price rides the separate `kind:"price"` frame so a broken analytics panel can't freeze the price (see TOP #9).
- **[CRITICAL]** ✅ FIXED — added a 6 s client staleness watchdog that forces a reconnect on a silently half-open socket (see TOP #9).
- **[CRITICAL]** ✅ FIXED — `connect()` has a single-socket guard; no parallel sockets ⇒ price can't go backwards (see TOP #9).
- **[HIGH]** Cockpit literally renders the voting tally (`evidence ok/needed`, `confirm sustain/need`); broad tape is a self-labeled "coincident, not leading" sidebar — not co-equal.
- **[HIGH]** Nothing answers "why do trades go right against me?" — no entry-edge/MAE display.
- **[LOW]** CE=red/PE=green inconsistent (ladder red, tape green); PCR thresholds differ across four panels.

### Tests (`tests/test_money_path.py`)
- **[CRITICAL]** ✅ PARTIALLY ADDRESSED — `FakePrices.option_age` is still `1.0`, but freshness is now tested against the REAL `PriceStore` (`test_option_feed_liveness_flags_a_stale_leg` drives `atm_option_age()`/`option_feed_alive()` with a stalled leg), plus NaN-never-on-wire and price-frame-shape tests.
- **[CRITICAL]** No assertion that BankNifty/FinNifty are un-rounded; FinNifty/all-stock-OI co-equality untested.
- **[HIGH]** "Early inflection" and "zone" tested only as unwired predicates; the hated voting gate is pinned as an invariant.
- **[HIGH]** Doctrine-breach counters are self-certifying/tautological; sizing test reuses the code's formula on both sides.
- **[MEDIUM]** Non-isolated fixed-path temp files + global `config` mutation ⇒ contamination can produce false GREEN.
- **[LOW]** `binary_exit_invariant` asserts `[-9,+10]` — looser than the −10/+12 doctrine on both ends; never asserts `LOT_SIZE==65`.

---

## 4. PRETENDS-TO-WORK (inert / display-only / unproven / faith-flipped)

| "Fix" | Reality | Evidence |
|---|---|---|
| VIS / BANK-LED single-decision entry | Shipped DARK — flags `False` | `config.py:478,460` |
| CROSS-LEAD lateness cure | All sub-flags OFF; `_lead_vote` returns 0 unconditionally | `config.py:425,448,460`; `signals.py:449` |
| CONSENSUS co-equal gate | DEMOTE-ONLY; can never cause/confirm; OFF for BREAK | `signals.py:734-743`; `config.py:505` |
| `calibrate_consensus.py` "prove-first" | Calibrator does not exist; thresholds are unvalidated priors | `consensus_core.py:22-23` |
| Sister option-OI leg | Wrong NFO code / unverified probe ⇒ `idx_pcr` empty, OI leg abstains | `feed.py:643-656,702-709` |
| Early-warning fall/rip | Drives no decision; "RISING" dark for first 60s | `risk.py:14-18,98-101` |
| Battle-lines "over sessions" | No save/load; never read by trader | `levels.py:26`; `app.js:939` |
| Persistent zone memory | Display-only; no predictiveness code; not co-equal | `memory.py:2,14-17,41` |
| Learning "closed loop" | Rarely bumps; rewards BOOKED_EARLY; brake always clearable | `learning.py:89-90,207,235` |
| Cheaper-entry limit | Faith-flipped: market-late on winners, cheap on losers | `trader.py:257-265,372-374` |
| `NEW_TICK_EVENT` freshness primitive | Set 4 places, never waited — dead code | `feed.py:34` |
| Doctrine-breach / gap-through counters | Write-only; feed no decision | `trader.py:91-94,569-582` |
| `self._snap` / `pcr_flip` (OI) | Inert; comments assert wiring that doesn't exist | `oi_engine.py:132,339-351` |
| Sentiment / breadth on replay | Constant-50 stub (no `prev_close`) | `heavyweights.py:61-62,140` |
| Commentary "valued tells protected" | All suppressed by `COMMENT_SIGNAL_ONLY=True` | `commentary.py:71-74`; `config.py:711` |
| MYTHOS_TRADING_RULES.md (system of record) | Stale — no mention of the live consensus gate | doc vs `config.py:502` |

---

## 5. REQUIREMENTS NOT MET (honest mapping)

| Requirement | Genuinely delivered? | Honest verdict |
|---|---|---|
| ONE sharp decision; HATES voting gate | **NO** | 6-of-14 tally is the only live fire path; rendered/celebrated in the cockpit |
| Caught EARLY / at the inflection; predict in advance | **NO** | Rails guarantee post-inflection; all early paths OFF; every tell is lagging/coincident |
| CHEAP entries | **NO** | Cheaper-limit faith-flipped; `premium_low` anchor fabricated; market-fill late on winners |
| Why do trades go right against me? | **NO** | Diagnosed (task #28) but unfixed; no entry-edge/MAE; reward ignores immediate-adverse −10 |
| BankNifty+FinNifty+ALL stocks (+OI) CO-EQUAL | **NO** | Demote-only veto; breadth = thin scalar; FinNifty muted; stock/sister OI absent from engine |
| Don't trade against the broad tape | **PARTIAL** | Only the defensive half (a sometimes-abstaining veto), and OFF for BREAKs |
| Strong zones (defended) help decisions | **NO** | Zones are display-only (levels/memory/risk never imported by trader) |
| Persistent memory over sessions | **NO** | Battle-lines: no persistence; memory: wiped daily / not decision-grade |
| Strikes ATM/ITM that MOVE, never OTM; delta checked | **PARTIAL** | Honored only by one flag; IV-NaN / walk-overrun cause false SKIPs on real movers; delta read unreliable |
| Prices NEVER delayed/dropped (biggest issue) | **YES (2026-06-20)** | Fast price frame decoupled from the build (200 ms); option-feed liveness + badge; NaN sanitized; client watchdog + single-socket guard (see TOP #9) |
| No rounding of BankNifty/FinNifty | **PARTIAL/UNTESTED** | No dedicated full-precision readout; no test pins it |
| Commentary ONLY very-bullish/bearish; no noise | **PARTIAL** | Two headline tells render colorless; rationale = the hated vote list |
| Expiry-day theta caution | **NO** | Cosmetic string suffix; suppressed under signal-only; stale hardcoded expiry |
| No 2:30 stop | **YES (mostly)** | Cutoff removed; but 15:15→15:25 window structurally scratches |
| Close the learning loop (adapt, not journal) | **NO** | Inert bumps; rewards early-booking; multi-horizon data captured with no consumer |
| OI + per-strike PCR display | **PARTIAL** | Per-strike PCR shown but absent from memory/commentary; four inconsistent thresholds |
| Display: CE=red/PE=green, WHY-THIS-TRADE | **PARTIAL** | CE coloring inconsistent across panels; WHY = fired-vote enumeration |

---

## 6. The ONE Highest-Leverage Thing To Fix Next

**Fix the entry edge first — replace the post-inflection voting monolith with a single, EARLY, positive-edge decision, and PROVE it on real tape before flipping anything else on.**

Everything else is motion around this. The system already knows (in comments, in memory, in task #28) that its entries are late and the edge is negative; it has built the replacement (VIS) and the cross-tape inputs, then refused to ship them because shipping them honestly requires a calibration that was skipped. The correct next move is narrow and sequenced:

1. Build the missing `calibrate_consensus.py` and an entry-edge backtest that runs on **real recorded tape** (not the max-pain-magnet sim), with a working breadth leg (revive `prev_close`/`change_pct` on replay).
2. Prove that an EARLY single-decision path (VIS + prior-swing exhaustion anchor) separates winners from losers and reduces adverse-after-cheap-entry.
3. Only then flip it ON as the *primary* path and demote the 6-of-14 tally to a fallback.

Until that loop closes with evidence, every other fix — colors, consensus vetoes, learning bumps, sister OI — is decorating a system that buys after the move is already done. Fix the buy; the rest becomes worth doing.
