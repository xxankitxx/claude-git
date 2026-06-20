# MYTHOS — Nifty Options Intelligence & Paper-Trading System

A browser-based ready-reckoner + rule-based paper trader for Nifty weekly
options, built on ICICI Direct Breeze live data. **Paper trading only — it
never places a real order.** You watch the cockpit, hear the alerts, and
mirror trades manually in your own account if and when the system has earned
your trust.

---

## Daily operating procedure (2 minutes, before 09:15 IST)

1. **Refresh the Breeze session key** (it expires daily):
   - Open `https://api.icicidirect.com/apiuser/login?api_key=<YOUR-URL-ENCODED-API-KEY>`
   - Log in with your ICICI Direct credentials.
   - The redirect URL contains `apisession=XXXXXXXX` — copy that number.
   - Paste it into `mythos/credentials.py` → `SESSION_KEY = "XXXXXXXX"`.
2. **Start the system:**
   ```
   python run_mythos.py
   ```
3. **Open the dashboard:** http://127.0.0.1:8765
4. Click **🔇 Sound** once to enable audio alerts (browsers require one click).

To explore the system any time without market data:
```
python run_mythos.py --sim
```
This drives the entire stack with a synthetic but realistic market.

---

## What the system does

- **Feeds** — Nifty spot + futures + ATM±8 option strikes tick-by-tick over
  websocket; 14 heavyweight stocks (HDFC Bank, ICICI, Reliance, …) live; full
  Nifty chain, heavyweight option chains and India VIX over budgeted REST
  polling (~9 calls/min vs Breeze's 100/min cap).
- **OI engine** (the core) — per-strike OI with 1/3/5-min change rates, OI
  walls, PCR per strike and aggregate, support/resistance zones, max pain,
  OI-vs-price divergence.
- **Heavyweight basket** — each stock scored from intraday move + its own
  option-chain PCR + OI-wall proximity, weight-blended into an index
  sentiment (0–100) and constituent-implied S/R levels.
- **Volatility engine** — full-chain IVs, IV rank/percentile (persisted
  across days in SQLite, improves with age), skew, expected move
  (0.8 × straddle), realized vol, variance premium.
- **Signal engine** — CE and PE scored every second from 8 weighted
  components (max 1.05, entry at ≥ 0.70):

  | Component | Weight | Fires when |
  |---|---|---|
  | CVD + VWAP | 0.25 | futures order flow rising & price above VWAP (mirror for PE) |
  | OI S/R bounce | 0.25 | price holding a strong, building OI zone |
  | PCR flip | 0.15 | aggregate PCR regime crossed (1.2↑ / 0.8↓) |
  | OI unwind | 0.10 | opposite side covering into the move |
  | IV environment | 0.10 | IV rank ≥ 30 or IV actively expanding |
  | Momentum | 0.10 | SuperTrend(10,3) + RSI(14) aligned |
  | Volume imbalance | 0.05 | same-side option volume ≥ 1.5× |
  | Greeks | 0.05 | ATM delta 0.35–0.65, gamma stable |

  Plus hard gates: first-5-min / after-14:30 lockout, chop filter (IV rank<20
  & ADX<20), 2-of-5-second persistence, premium-rising check, CE/PE premium
  divergence veto, stale-quote veto, and a rising score bar after consecutive
  stop-losses (run7's conviction ramp).
- **Paper trader** — ₹1,00,000 fresh daily; all-in lots; **SL = entry −10**
  (checked before every other exit rule — it is a promise); trailing arms at
  +6 (locks entry+1), tiered chandelier from +8: 28% of peak ≤12 pts, 22%
  ≤20 pts, 18% above — widened ×1.35 while futures order flow still agrees
  (runners are HELD, not scalped), tightened ×0.6 on thesis weakening;
  90 s re-entry cooldown so one big move = one ridden trade; stall-kill for
  dead trades (theta-aware patience by hour); EOD flatten 15:25; auto-archive
  to `mythos/archive/` at close + manual button. Stop fills are honest:
  observed market price minus slippage, never "filled at the stop level".
- **Dynamic target** — an open trade's target is re-projected continuously:
  the premium the option would carry if spot travelled to the nearest
  opposing OI wall (capped by the expected move). It ratchets up, never
  down, and never below entry+12. Exits stay owned by the trail.
- **Commentary + audio** — only extreme events: PCR spikes, CVD/price
  divergence ≥2σ, IV explosions, max-pain jumps, strike volume surges ≥5×,
  heavyweight ±1.5% moves, order-book imbalance (bids ≥3× offers on near
  strikes and the inverse), liquidity blowouts (ATM spread ≥3× its norm),
  plus a "WHY THIS TRADE" rationale on every entry. Distinct sounds for
  entry / win / loss, and a long ~2.6 s two-note chime for commentary.

## Reading the dashboard

- **Index Sentiment gauge** — 0 = max bear, 100 = max bull. Blend of engine
  evidence (60%) and heavyweight basket (40%). 42–58 means stand aside.
- **S/R Thermometer** — green bars (left) = put OI defending strikes below;
  red bars (right) = call OI capping strikes above. ▲ = wall still building.
  Cyan dashed line = spot. HW SUP/RES = constituent-implied levels.
- **Signal Score History** — green CE vs red PE vs gold entry line. A
  direction grinding upward toward 0.70 is the tell to get ready.
- **Trade Signal Cockpit** — each component bar lights when it fires; the
  banner goes green/red the second all gates pass (and audio fires).
- **Active Position** — premium lifeline: SL → entry → current → trail →
  target, live P&L, peak, live engine score of your direction.
- **PCR Heat strip** — green strikes are bull-defended, red are bear-capped.
- **Live Premiums ladder** — CE/PE LTP + IV for ATM±4 strikes (ATM row
  highlighted gold; hover any premium for its bid/ask). This is the panel to
  watch when mirroring a signal manually.
- **OI Delta Flow** — net delta of the whole OI book (dealer hedging proxy);
  steady climbs accompany sustained trends.

## Files

```
run_mythos.py            launcher (--sim for simulation)
requirements.txt         pip dependencies
mythos/
  config.py              every tunable — instrument, weights, risk, thresholds
  credentials.py         api key/secret + DAILY session key
  feed.py                Breeze websocket + REST pollers (lock-free hot path)
  sim_feed.py            synthetic market for --sim
  greeks.py vol.py       Black-Scholes/IV, IV rank, skew, expected move
  flow.py                candles, RSI/ATR/SuperTrend/ADX, VWAP, CVD
  oi_engine.py           OI walls, S/R zones, PCR, max pain
  heavyweights.py        constituent basket analyzer
  signals.py             scored decision engine + hard gates
  trader.py              paper execution, trailing, daily reset
  commentary.py audio.py event ticker + sounds
  store.py               SQLite (candles, IV history, OI snapshots, trades)
  app.py state.py server.py   orchestrator, UI state, FastAPI/websocket
  static/                dashboard (no external dependencies)
  data/ logs/ archive/   runtime artifacts
```

## Tuning

Everything lives in `mythos/config.py`. The ones that matter most:

- `SCORE_THRESHOLD` (0.70) — raise to 0.75–0.80 for fewer, higher-quality
  entries; lower with caution.
- `NUM_STRIKES`, `EXPIRY_OVERRIDE` — set the override when an NSE holiday
  shifts the Tuesday expiry.
- `HEAVYWEIGHTS` — update weights after NSE's monthly rebalance.
- `LOT_SIZE` — update when NSE revises the Nifty lot.
- `SL_POINTS / TARGET_POINTS / TRAIL_*` — the risk geometry.

## Troubleshooting

- **"WebSocket failed" at startup** → stale `SESSION_KEY` (refresh it), or
  market closed, or ICICI server issues.
- **Spot ticks but options silent** → check expiry date printed at startup;
  set `EXPIRY_OVERRIDE` if a holiday moved it.
- **Dashboard "STALE" badge** → feed lost; the system auto-reconnects and
  resubscribes within ~15 s.
- **No trades all day** → look at the cockpit's blocked reason. The engine
  is selective by design — a quiet day with no qualifying setups is the
  system working, not failing.
- Logs: `mythos/logs/mythos.log` (rotating, DEBUG level on file).
