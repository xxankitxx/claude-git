#!/usr/bin/env python3
"""
MYTHOS — TRADE-LIFECYCLE VALIDATION (the round-trip test).

Drives the REAL engine AND the REAL PaperTrader (entries + the full exit ladder +
risk-based sizing + daily circuit breaker + cooldowns) over many synthetic days,
and reports the ACTUAL P&L — not entry forward-returns, the real thing.

Wiring is copied from replay.py / app._analytics_pass + _exit_loop so there is
ZERO strategy drift from the live system (the same classes the dashboard runs).
The only difference from replay is the data source: a fresh RealisticSimFeed per
day instead of recorded frames, so we sample many independent day-characters.

    python lifecycle.py [days] [minutes]

Honest limit: the sim is a calibrated proxy (regime trends + S/R causality vs
real-Nifty stylised facts), NOT real tape. This measures whether the MECHANICS
(exit ladder, risk caps, no -28 anomalies, daily stop) behave correctly and what
the strategy WOULD have produced on this synthetic tape. The real edge verdict is
Monday's live recording -> replay.py.
"""
import sys
import time as _time
import math
import random
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))
_VCLOCK = [0.0]
_real_time, _real_mono = _time.time, _time.monotonic


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(_VCLOCK[0], tz or IST)


def _patch():
    _time.time = lambda: _VCLOCK[0]
    _time.monotonic = lambda: _VCLOCK[0]
    import mythos.signals as s
    import mythos.trader as t
    import mythos.sim_feed as sf
    s.datetime = t.datetime = sf.datetime = _FakeDatetime
    return (s, t, sf)


def _restore(mods):
    _time.time, _time.monotonic = _real_time, _real_mono
    s, t, sf = mods
    s.datetime = t.datetime = sf.datetime = datetime


_START = datetime(2026, 6, 16, 9, 20, 0, tzinfo=IST).timestamp()


def _dyn_target(t, spot, T, vol, oi, gk, config):
    try:
        if t.direction == "CE":
            z = oi.nearest_resistance(spot)
            wall = z.level if z else 0.0
            if wall <= spot:
                return
            if vol.expected_move > 0:
                wall = min(wall, spot + vol.expected_move)
        else:
            z = oi.nearest_support(spot)
            wall = z.level if z else 0.0
            if wall <= 0 or wall >= spot:
                return
            if vol.expected_move > 0:
                wall = max(wall, spot - vol.expected_move)
        iv = vol.chain_iv.get((t.strike, t.right)) or vol.atm_iv or 0.13
        proj = float(gk.bs_price(wall, t.strike, T, iv, t.right))
        new_tgt = max(t.entry_price + config.TARGET_POINTS, round(proj, 2))
        if new_tgt > t.target:
            t.target = new_tgt
    except Exception:
        pass


def _run_day(day_seed: int, minutes: int):
    from mythos.feed import PriceStore
    from mythos.oi_engine import OIEngine
    from mythos.flow import FlowStack
    from mythos.vol import VolEngine
    from mythos.heavyweights import HeavyweightBasket
    from mythos.signals import SignalEngine
    from mythos.trader import PaperTrader
    from mythos.sim_feed import RealisticSimFeed
    from mythos import config, greeks as gk
    import numpy as np

    random.seed(day_seed)
    prices = PriceStore()
    oi = OIEngine()
    flow = FlowStack()
    vol = VolEngine()
    basket = HeavyweightBasket()
    sig = SignalEngine(oi, flow, vol, basket, prices)
    sig.bypass_time_gates = True
    trader = PaperTrader(prices, store=None)
    trader.bypass_time = True
    trader._save_today = lambda: None

    feed = RealisticSimFeed(prices, basket)
    feed._seed_oi()
    feed._seed_basket()
    prices.vix = round(feed.vix, 2)
    flow.seed_candles(feed.warmup_candles(30))

    spots = []
    last_opt = last_hw = last_chain = last_vol = -1e9
    gamma_heat = 0.0
    for sec in range(minutes * 60):
        vt = _START + sec
        _VCLOCK[0] = vt
        feed._step_vix()
        feed._step_spot_and_futures(vt)
        if vt - last_opt >= 1.0:
            feed._step_options()
            last_opt = vt
        if vt - last_hw >= 1.0:
            feed._step_heavyweights()
            last_hw = vt
        if vt - last_chain >= 30.0:
            prices.chain_oi = dict(feed.oi)
            last_chain = vt

        spot, futp, atm, ce, pe = prices.freeze_core()
        if spot <= 0 or atm <= 0:
            continue
        spots.append(spot)

        while prices.fut_ticks:
            pr, qty, bid, ask, foi = prices.fut_ticks.popleft()
            flow.vwap.update(pr, qty)
            flow.avwap.update(pr, qty)
            flow.swings.update(pr)
            flow.cvd.on_tick(pr, qty, bid, ask)
            if foi > 0:
                flow.fut_oi.update(pr, foi)
            closed = flow.candles_1m.update(pr, qty)
            if closed:
                flow.rsi.on_candle(closed)
                flow.atr.on_candle(closed)
                flow.supertrend.on_candle(closed)
                flow.adx.on_candle(closed)

        strikes = prices.snapshot_strikes(atm_override=atm)
        for (k, right), d in strikes.items():
            if d["oi"] > 0:
                oi.update_strike(k, right, d["oi"], vt)
            if d["vol"] > 0:
                oi.update_volume_baseline(k, right, d["vol"])
        oi.note_spot(spot, vt)
        oi.recompute(atm, spot)
        T = gk.years_to_expiry(config.expiry_dt_ist(), _FakeDatetime.now(IST))
        # vol every 5s — expected_move/IV move slowly; the trader uses them for
        # strike selection / dynamic target / gamma, not for the FIRE decision.
        if vt - last_vol >= 5.0:
            vol.update_chain(spot, strikes, T, atm)
            vol.update_spot(spot)
            try:
                iv = vol.chain_iv.get((atm, "call")) or vol.atm_iv
                if iv and spot > 0:
                    g = gk.greeks(spot, np.array([atm]), T, np.array([iv]), "call")
                    gamma_heat = float(np.nan_to_num(g["gamma"][0])) * config.STRIKE_STEP
            except Exception:
                pass
            last_vol = vt
        for sym, ltp in list(prices.hw_ltp.items()):
            basket.on_tick(sym, ltp)
        basket.recompute(spot)

        # decide + trade (the REAL engine + REAL trader)
        decision = sig.evaluate()
        if decision.allowed and not trader.open:
            t = trader.try_enter(decision, vol.expected_move, oi)
            if t:
                sig.note_entry(t.direction)
        for t in trader.snapshot_open():
            _dyn_target(t, spot, T, vol, oi, gk, config)
        if trader.open:
            d = trader.open[0].direction
            slope = flow.cvd.slope(60)
            trend_ok = slope > 0 if d == "CE" else slope < 0
            gamma_ride = gamma_heat >= 0.18 and trend_ok
            closed_trades = trader.check_exits(
                sig.live_score(d), trend_ok, gamma_ride)
            for ct in closed_trades:
                if ct.pnl_pts < 0:
                    sig.note_stop(ct.direction)
                sig.note_exit()

    # force-close any trade still open at session end (mark-to-market)
    for t in list(trader.open):
        px = prices.option_price(t.strike, t.right)
        if px > 0:
            trader._close(t, px, "EOD")
            trader.open.remove(t)
            trader.closed.append(t)

    rng = (max(spots) - min(spots)) if spots else 0.0
    net_move = (spots[-1] - spots[0]) if spots else 0.0
    return trader.closed, rng, net_move, trader.capital


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    mods = _patch()
    from mythos import config
    try:
        all_trades = []
        day_rows = []
        for d in range(days):
            closed, rng, net_move, end_cap = _run_day(3000 + d, minutes)
            day_pnl = sum(t.pnl_pts for t in closed)
            day_cash = sum(t.pnl_cash for t in closed)
            wins = sum(1 for t in closed if t.pnl_pts >= 0)
            all_trades.extend(closed)
            day_rows.append((d + 1, rng, net_move, len(closed), wins, day_pnl, day_cash))
            print(f"  day {d+1:2d}: range {rng:4.0f} move {net_move:+5.0f}  "
                  f"trades {len(closed):2d}  W {wins:2d}  "
                  f"pnl {day_pnl:+7.1f}pt  ₹{day_cash:+8.0f}")
    finally:
        _restore(mods)

    n = len(all_trades)
    print("\n" + "=" * 70)
    print(f"  TRADE-LIFECYCLE — {days} days × {minutes} min  ({n} trades)")
    print("=" * 70)
    if n == 0:
        print("  no trades taken.")
        return 0

    wins = [t for t in all_trades if t.pnl_pts >= 0]
    losses = [t for t in all_trades if t.pnl_pts < 0]
    pts = sum(t.pnl_pts for t in all_trades)
    cash = sum(t.pnl_cash for t in all_trades)
    wr = len(wins) / n * 100
    aw = sum(t.pnl_pts for t in wins) / len(wins) if wins else 0.0
    al = sum(t.pnl_pts for t in losses) / len(losses) if losses else 0.0
    gp = sum(t.pnl_cash for t in wins)
    gl = -sum(t.pnl_cash for t in losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    expectancy_pts = pts / n
    profit_days = sum(1 for r in day_rows if r[5] > 0)

    print(f"\n  net           {pts:+.1f} pts   ₹{cash:+,.0f}   "
          f"({pts/days:+.1f} pt/day avg)")
    print(f"  win-rate      {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  avg win       {aw:+.1f} pt    avg loss {al:+.1f} pt")
    print(f"  expectancy    {expectancy_pts:+.2f} pt/trade   profit factor "
          f"{pf:.2f}" + ("" if pf != float('inf') else " (no losses)"))
    print(f"  profitable    {profit_days}/{days} days")

    # exit-reason breakdown
    reasons = {}
    for t in all_trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, [0, 0.0])
        reasons[t.exit_reason][0] += 1
        reasons[t.exit_reason][1] += t.pnl_pts
    print(f"\n  EXIT REASONS")
    for r, (cnt, rp) in sorted(reasons.items(), key=lambda x: -x[1][0]):
        print(f"    {r:14} {cnt:3d}  ({rp:+8.1f} pt total, {rp/cnt:+5.1f} avg)")

    # hold time + peaks
    holds = [t.exit_epoch - t.entry_epoch for t in all_trades
             if getattr(t, "exit_epoch", 0) and t.entry_epoch]
    if holds:
        holds.sort()
        print(f"\n  HOLD TIME     median {holds[len(holds)//2]:.0f}s   "
              f"max {max(holds):.0f}s")

    # equity curve / max drawdown across all trades (sequential)
    eq = config.STARTING_CAPITAL
    peak = eq
    mdd = 0.0
    for t in all_trades:
        eq += t.pnl_cash
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    print(f"  max drawdown  ₹{mdd:,.0f} ({mdd/config.STARTING_CAPITAL*100:.1f}% of "
          f"starting capital)")

    # SAFETY INVARIANTS — the contract that makes Monday survivable
    print(f"\n  SAFETY CHECKS")
    worst = min(all_trades, key=lambda t: t.pnl_cash)
    worst_frac = -worst.pnl_cash / config.STARTING_CAPITAL * 100
    risk_cap = config.RISK_PER_TRADE_FRAC * 100
    print(f"    worst single trade: {worst.pnl_pts:+.1f}pt  ₹{worst.pnl_cash:+,.0f}  "
          f"({worst_frac:.1f}% of capital)  [cap {risk_cap:.0f}%]  "
          f"{'OK' if worst_frac <= risk_cap + 0.5 else 'OVER!'}")
    big_losers = [t for t in all_trades if t.pnl_pts < -12.0]
    print(f"    trades worse than -12pt (gap-throughs): {len(big_losers)}"
          + (f"  -> {[round(t.pnl_pts,1) for t in big_losers][:8]}" if big_losers else ""))
    worst_day = min(day_rows, key=lambda r: r[6])
    print(f"    worst day: {worst_day[6]:+,.0f} ₹ "
          f"({worst_day[6]/config.STARTING_CAPITAL*100:+.1f}%)  "
          f"[daily stop at -{config.DAILY_MAX_LOSS_FRAC*100:.0f}%]")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
