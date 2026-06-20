# MYTHOS — What This Code Does, and Exactly How It Gives a Trade

*The complete explanation, written for the trader who runs it. Every rule in
this document is the rule in the code, with current values as of 2026-06-13.*

---

## 1. The one-paragraph summary

MYTHOS watches the Nifty options battlefield through ICICI Breeze — spot,
futures (price + open interest), the full weekly option chain, India VIX,
Bank Nifty, Fin Nifty, and 14 heavyweight stocks with their option chains —
and hunts ONE kind of opportunity: **a strong zone being defended or broken
while the premium is still cheap**. It stalks the zone, counts live defense
evidence (13 separate tells), enters only when most of them agree several
seconds in a row, then manages the position with a conviction meter that
tells you in plain words to keep holding or get ready to square off. It
paper-trades ₹1,00,000 (fresh daily), never places a real order, and
explains every action in the commentary with a reason you can audit.

---

## 2. What it reads, continuously

| Input | Source | Cadence | Used for |
|---|---|---|---|
| Nifty spot | Breeze WS tick | tick | zones, ATM, kinematics |
| Nifty futures price | WS tick | tick | CVD, VWAP, AVWAP, candles, swing pivots |
| Nifty futures OI | WS tick | tick | long/short buildup/covering/unwinding quadrant |
| Option chain ATM±8 (CE+PE) | WS tick | tick | premiums, OI, volume, bid/ask + sizes |
| Full weekly chain | REST | 90 s | wide OI walls, max pain, GEX |
| India VIX | REST | 60 s | premium environment |
| **Bank Nifty, Fin Nifty** | REST | 20 s | sister-index agreement (evidence + conviction) |
| 14 heavyweights (official NSE weights) | WS ticks | tick | weighted tug-of-war, basket sentiment |
| Heavyweight option chains | REST round-robin | ~20 s/stock | per-stock PCR + OI walls → implied Nifty S/R |

Everything persists to SQLite (candles, IV samples, OI snapshots, trades) —
the system's memory grows every session and powers IV rank, warm restarts,
and future backtesting.

## 3. The analysis engines

**Open Interest engine** (the requirement's "core differentiator"):
- Per-strike OI tracked with **rate-of-change EMAs over 1, 3 and 5 minutes**
  — not just OI levels, but whether writers are *adding or running*.
- **OI walls**: a strike holding ≥2× its neighbours' OI.
- **Support/resistance zones**: clusters where per-strike PCR ≥ 1 with put
  OI *building* (support) or PCR ≤ 0.7 with call OI building (resistance),
  merged and ranked by strength 0–1.
- **Near-ATM PCR (±6 strikes)**: a dedicated 1-second series of the PCR where
  writers actually fight intraday; its **3-minute change** is a first-class
  signal (put writers stepping in = bulls underwriting; call writers
  pressing = bears capping).
- **Max pain**, **OI-vs-price divergence**, **Σdelta×OI flow** (dealer
  hedging pressure), and **GEX** (dealer gamma: negative = amplified tape
  where moves extend; positive = dampened tape where moves fade).

**Futures positioning quadrant** (from futures OI + price, 3-min windows):
LONG BUILDUP / SHORT COVERING / SHORT BUILDUP / LONG UNWINDING. The two
unwinding quadrants are reversal fuel — trapped traders being forced out
accelerate moves off a zone.

**Price structure**: 1-minute candles, RSI/ATR/ADX/SuperTrend, session VWAP,
**anchored VWAPs from the day's high and low** (the trapped-trader averages),
and **swing pivots** (every ≥15-pt reversal extreme) — these pivots are
zones too, because genuine legs launch from price structure, not only from
OI walls. Broken zones **flip roles** (broken support becomes resistance).

**Calculus engine**: smoothed 1st/2nd/3rd derivatives (velocity /
acceleration / jerk) of spot, CE premium, PE premium and near-ATM PCR.
The second derivative reads turns before they print: *falling but
decelerating* = a bottom forming (cheapest entry moment); *rising but
decelerating* = a move dying (exit warning).

**Heavyweight basket**: each stock scored (price move 50%, own-chain PCR
30%, OI-wall proximity 20%), blended by official index weights into the
tug-of-war (bull force vs bear force with named top pullers) and into
constituent-implied Nifty support/resistance levels.

## 4. How a trade is born — the Zone Hunter

Each direction (CE and PE) runs its own hunt, every second:

```
SCANNING ──► STALKING ──► ARMED ──► CONFIRMING ──► FIRE
 no zone     zone <60pts   zone      majority of    enter at
 in range    away, named   touched   evidence held  market
             on cockpit    (±20pts)  N seconds      (cheap only)
```

**The 14 defense evidences** counted while ARMED (bounce at support shown;
PE mirrors everything):

| # | Evidence | Fires when |
|---|---|---|
| 1 | Zone holding | no fresh low in 20 s |
| 2 | Price turned | ≥4 pts off the touch extreme |
| 3 | Our premium rising | CE velocity > +0.05/s *(the user's confirmation tool)* |
| 4 | Their premium stalling | PE velocity ≤ +0.05/s |
| 5 | Pressure exhausting | CVD selling decelerating/flipping — **REQUIRED** (strongest single discriminator in the sim study; a prior, not a proven live stat) |
| 6 | Defenders adding OI | put-OI rate positive on BOTH the 1-min and 3-min windows at zone strikes (building *now*) |
| 7 | Book favours us | CE bids ≥1.5× asks, or PE offers being dumped |
| 8 | Heavyweights agree | weighted basket ≥55 (CE) / ≤45 (PE) |
| 9 | Unwinding fuel | futures quadrant = short covering / long buildup |
| 10 | AVWAP reclaim | futures above the day-high sellers' average (bears trapped) |
| 11 | ATM±6 PCR shift | near-PCR 3-min change ≥ +0.05, or PCR velocity positive AND accelerating |
| 12 | BankNifty/FinNifty agree | sisters' 3-min change in our direction (stale feeds excluded) |
| 13 | OI divergence | price rising while near-money call OI falls = bears covering (§4.3) |
| 14 | Turn physics (d²/dt²) | falling-but-decelerating, or rising-and-accelerating |

**Entry requires, simultaneously:**
- evidence ≥ **5 of 14** ("most factors"), held **3 consecutive seconds**
  — or the fast path: ≥**7 of 14** fires in **1 second** (a genuine
  ignition must not be missed waiting);
- **"Pressure exhausting" present** (hard requirement);
- **cheapness**: premium within **+10** of its zone-touch low (+15 on the
  fast path) — *the cheapest-possible-price law*;
- escalations: a zone that stopped you today ("burned"), or a whipsaw
  period (≥2 stops in 10 min), demands the overwhelming 7/13 with full
  sustain;
- vetoes: market hours (no entries first 5 min or after 14:30), premium
  40–350, spread ≤2, fresh quotes, one position at a time, and the
  asymmetric pause — **15 s after a losing exit, 90 s after a winning one**
  (audited: chasing a trail exit's dead momentum ran 25% WR).

**BREAK entries** (trend days): when spot snaps 10–30 pts through a strong
opposing wall with thrust — flow accelerating, premium velocity AND
acceleration positive, wall OI capitulating, buyers queuing, fuel quadrant —
≥3 of 5 held 2 s. The broken wall then flips to support/resistance behind
the trade.

**Sizing**: all-in — `lots = floor(capital ÷ (premium × 65))`.

At the moment of entry the commentary prints **WHY THIS TRADE** — the zone,
the archetype, every evidence that fired with its live reading, and the
risk numbers. The long "armed" chime sounds when a hunt first arms (get
ready); a short ting accompanies ordinary commentary.

## 5. How the trade is managed — the exit ladder

| Condition | Action |
|---|---|
| First 30 s | blind hold — only the hard SL can act |
| Always | **hard SL = entry − 10** (checked before all other logic; fills at stop − 1 slippage; gaps >5 pts fill honestly at market) |
| Peak ≥ **+10** | **Breakeven guard**: stop ratchets to entry+1 — a trade that showed +10 may never become a loser (audited: 0 of 7 such losers ever recovered; every real winner pushed straight to +20) |
| Peak < +20 | no profit-taking exists — the trade rides trend breathing |
| Peak ≥ **+20** | insurance floor entry+13 (≥+12 net) + chandelier: ~30%-of-peak offset, widened ×1.35 while futures flow agrees, ×1.15 in gamma regimes, ×1.2 once locked, ×1.2 while conviction strong; tightened ×0.6 on sustained weakness; **always protects ≥70% of peak** |
| Stall | dead trade (peak <2.5 after 180 s; 100 s after 13:00, 75 s after 14:00) → exit (theta-aware time-stop) |
| Safety | stale quote 120 s → exit at last price; 1-hour max hold; EOD flatten 15:25 |

**The live conviction meter** re-reads 12 factors every second for the open
position (zone intact, both premium velocities, premium *force* (2nd
derivative), flow, fuel, AVWAP, near-PCR shift, book, heavyweights, sisters,
opposite-hunter quiet) — smoothed over ~12 s with hysteresis so it can't
flip-flop — and prints the instruction on the position panel:
**KEEP HOLDING / HOLD / CAUTION — BE READY / BE PREPARED TO SQUARE OFF**
(zone break snaps to the alert instantly, with a chime). Every exit resets
all hunts' confirmation counters — no pre-armed instant flips.

## 6. The dashboard, panel by panel

Row 1 — **Zone Hunter Cockpit** (both hunts: state badge, zone, evidence
checklist, confirm countdown, premium vs zone-low) · **Active Position**
(ATM CE/PE strip, big strike, BOUGHT→NOW prices, SL/entry/trail/target
lifeline, conviction meter) · **Live Premiums** (CE OI/LTP | strike |
PE LTP/OI | PCR, CE green / PE red everywhere).
Row 2 — **Index Sentiment** (gauge + PCR/MaxPain/quadrant + BN/FN chips) ·
**Market Commentary** (color-coded BULLISH green / BEARISH red / rationale
cyan / warnings gold) · **Today's Trades** (with In/Out times and prices).
Row 3 — **Nifty 1-min candlesticks** (VWAP, MaxPain, AVWAP-H/L overlays;
velocity + acceleration chips) · **Heavyweights** (weighted tug-of-war).
Row 4 — **OI Thermometer** (strikes ascending; CE walls left red, PE right
green; ▲ = building; SPOT line; HW-implied levels) · **OI Pulse** (PCR heat
strip + plain-words OI flow) · **Premium Environment** (buyer's verdict:
FAVOUR/NEUTRAL/AGAINST; tape regime from dealer gamma) + **Greeks**.
Row 5 — **OI & Volume Radar** (significant OI builds/unwinds and volume
spikes in ANY strike of ANY instrument — Nifty chain, Nifty futures, all 14
heavyweight chains and stocks — each measured against that contract's own
norm) · **Buyer/Seller Radar** (order-book pressure shifts — bid/ask size
ratios vs each contract's own baseline — across Nifty options, futures, and
all heavyweight stocks).
Row 6 — **Performance** (capital, P&L, win rate, expectancy, max DD, equity
curve, archive button).

**The regime badge** (header): the market-state classifier narrates
continuously — **TRENDING** (prime tape) / **ACTIVE** (normal two-way) /
**COILING** (narrow range but OI building: breakout loading) / **FLAT**
(narrow + weak ADX + no flow). In FLAT the system keeps hunting and keeps
talking but **refuses to fire entries** — the banner says exactly why. The
system is never silent, and never trades dead tape.

## 7. Daily operation

```
08:40  get the day's session key → paste into mythos/credentials.py
08:55  python preflight.py        → must print GO (8 live checks)
09:00  python run_mythos.py       → open http://127.0.0.1:8765
09:15  market opens (first 5 minutes deliberately blocked)
15:25  auto-flatten · 15:30 auto-archive to mythos/archive/
any time: python run_mythos.py --sim  (full system on a synthetic
swing-structured tape; separate data files, never touches live records)
```

## 8. Honest limitations (read before Monday)

- All thresholds are **sim-calibrated**; the first live sessions are for
  measurement, not for trust. Judge on the Performance panel's expectancy
  after 2–3 weeks.
- BN/FN are **sentiment inputs** (price direction agreement) — their option
  chains are *not* analyzed (user-scoped: Nifty-only trading).
- No economic calendar — you must respect RBI/Fed/budget days yourself.
- Money-path test suite at `tests/test_money_path.py` (12 invariants — BS
  math, sizing, P&L, the exit ladder) runnable with bare
  `python tests/test_money_path.py`. Engine-level coverage beyond the money
  path is still partial.

## 10. Proving it — the replay harness

Every session is now recorded frame-by-frame (1 Hz) to SQLite by the flight
recorder. `replay.py` re-runs a recorded day through the **real** decision
engine and reports the trades + expectancy that policy produced — and, with
`--set KEY=VAL`, replays the SAME tape under a changed config and prints the
delta. This is how thresholds get calibrated honestly (on recorded tape)
instead of from memory of the last bad session.

```
python replay.py                      list recorded days
python replay.py 2026-06-16           replay live day, print expectancy
python replay.py 2026-06-16 --sim     replay a sim recording
python replay.py 2026-06-16 --set BREAKEVEN_GUARD_PEAK=8   A/B vs baseline
```

Honest limit: frames are 1 Hz snapshots, so replay CVD/premium-velocity is
coarser than live, and like every backtest it replays the recorded tape (no
market-impact model). It measures policy behaviour faithfully; it is not a
crystal ball.
- Paper fills model slippage (1 pt on stops) but not partial fills or
  freak-quote spikes.

## 9. The rule-evolution ledger (your orders, in sequence)

1. Start: requirement's 0.70 weighted score, SL −10, target +12, trail at +6.
2. *"No grading system — zones, conviction with price, cheapest price"* →
   zone-hunter rewrite.
3. *"No 1–3 point scraps — SL or ≥6"* → +6 floor → **instant-lock bug class
   discovered** (floors must arm ≥3 pts above their lock).
4. *"Hold 12–15 at least, 30 s at least"* → floor +12, hold 30 s.
5. *"Give the trade time — don't bank +12 and re-enter"* → hold-first: no
   profit exit below peak 20.
6. Forensic audit (61 trades, verified counterfactuals) → **breakeven guard
   at +10** (rescued the worst failure mode in the sim study), **asymmetric cooldown 15/90 s**,
   **whipsaw escalation**, **Pressure-exhausting required**.

That ledger is the system's real character: every rule traces to either
your explicit order or a verified measurement — never to a guess.
