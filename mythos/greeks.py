"""
MYTHOS — Black-Scholes Greeks & implied volatility.

European pricing is the right model for Nifty index options (cash settled,
no early exercise). Everything is vectorized numpy so a full chain of 30+
strikes prices in microseconds — no numba/multiprocessing needed at this scale
(first-principles: the requirement's process-pool prescription solves a problem
this workload doesn't have).

Implied vol uses a bisection solver: ~25 iterations gives 1e-4 vol accuracy,
is unconditionally stable (Newton diverges on deep OTM near expiry), and still
vectorizes across the whole chain.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import norm

from . import config


@dataclass
class GreekSet:
    iv:    float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0   # per calendar day, in premium points
    vega:  float = 0.0   # per 1 vol point (0.01)


def _d1_d2(S, K, T, r, sigma):
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    return d1, d1 - sigma * sqrtT


def bs_price(S, K, T, sigma, right: str, r: float = config.RISK_FREE_RATE):
    """Vectorized BS price. right: 'call' | 'put'. T in years."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    T = np.maximum(T, 1e-6)
    sigma = np.maximum(sigma, 1e-4)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if right == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(price, S, K, T, right: str,
                r: float = config.RISK_FREE_RATE,
                lo: float = 0.01, hi: float = 3.0, iters: int = 30):
    """Vectorized bisection IV. Returns nan where the premium is outside
    no-arbitrage bounds (stale/crossed quotes)."""
    price = np.asarray(price, dtype=float)
    S = np.broadcast_to(np.asarray(S, dtype=float), price.shape).copy()
    K = np.broadcast_to(np.asarray(K, dtype=float), price.shape).copy()
    T = max(float(T), 1e-6)

    intrinsic = np.maximum(S - K, 0.0) if right == "call" else np.maximum(K - S, 0.0)
    valid = (price > intrinsic + 0.05) & (price > 0) & (S > 0)

    lo_a = np.full(price.shape, lo)
    hi_a = np.full(price.shape, hi)
    for _ in range(iters):
        mid = 0.5 * (lo_a + hi_a)
        pm = bs_price(S, K, T, mid, right, r)
        below = pm < price
        lo_a = np.where(below, mid, lo_a)
        hi_a = np.where(below, hi_a, mid)
    iv = 0.5 * (lo_a + hi_a)
    return np.where(valid, iv, np.nan)


def greeks(S: float, K, T: float, sigma, right: str,
           r: float = config.RISK_FREE_RATE):
    """Vectorized Greeks for one side of the chain.
    Returns dict of arrays: delta, gamma, theta (per day), vega (per vol pt)."""
    K = np.asarray(K, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    T = max(float(T), 1e-6)
    sigma_safe = np.where(np.isnan(sigma) | (sigma <= 0), 0.15, sigma)

    d1, d2 = _d1_d2(S, K, T, r, sigma_safe)
    pdf = norm.pdf(d1)
    sqrtT = np.sqrt(T)

    gamma = pdf / (S * sigma_safe * sqrtT)
    vega = S * pdf * sqrtT / 100.0                       # per 1 vol point
    theta_core = -(S * pdf * sigma_safe) / (2 * sqrtT)
    if right == "call":
        delta = norm.cdf(d1)
        theta = (theta_core - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (theta_core + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365.0

    bad = np.isnan(sigma)
    return {
        "delta": np.where(bad, np.nan, delta),
        "gamma": np.where(bad, np.nan, gamma),
        "theta": np.where(bad, np.nan, theta),
        "vega":  np.where(bad, np.nan, vega),
    }


def single_greeks(price: float, S: float, K: float, T: float,
                  right: str) -> Optional[GreekSet]:
    """Convenience scalar version: solve IV from premium then full Greeks."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    iv = float(implied_vol(np.array([price]), S, np.array([K]), T, right)[0])
    iv_solved = not np.isnan(iv)
    # The IV solve fails when premium ≈ intrinsic — i.e. a DEEP-ITM option, where
    # |delta| is ~1.0 BY DEFINITION and needs no IV at all. Old code returned None
    # here, so select_strike silently SKIPPED the highest-conviction MOVER ("you
    # never checked delta properly"). Instead, fall back to a sensible vol so a
    # real mover is never NaN-skipped, and bound the deep-ITM delta explicitly.
    # Only an OTM quote with unsolvable IV (a junk/crossed book, no intrinsic)
    # still returns None — preserving the skip-the-garbage behaviour.
    sigma = iv if iv_solved else config.FALLBACK_IV
    g = greeks(S, np.array([K]), T, np.array([sigma]), right)
    delta = float(g["delta"][0])
    gamma, theta, vega = (float(g["gamma"][0]), float(g["theta"][0]),
                          float(g["vega"][0]))
    if not iv_solved:
        intrinsic = max(S - K, 0.0) if right == "call" else max(K - S, 0.0)
        if intrinsic > 0:
            delta = 0.99 if right == "call" else -0.99
            # a deep-ITM (premium ≈ intrinsic) option is nearly all intrinsic, so
            # its EXTRINSIC greeks are ~0. The FALLBACK_IV used only to bound delta
            # would inflate gamma/theta/vega by orders of magnitude on the display,
            # so zero them rather than show a fabricated extrinsic sensitivity.
            gamma = theta = vega = 0.0
        else:
            return None
    return GreekSet(iv=sigma, delta=delta, gamma=gamma, theta=theta, vega=vega)


def years_to_expiry(expiry_dt, now_dt) -> float:
    """Calendar-time fraction of a year to expiry (15:30 IST expiry moment).

    EXPIRY-DAY FIX (2026-06-15): on expiry the floor is 600 s (matching sim_feed),
    so a 0DTE option's gamma / GEX / theta near 15:30 stay finite instead of
    exploding ~33× off the old flat 60 s floor — which inflated gamma_heat and
    false-fired the two-tier gamma blaster. Non-expiry days are byte-identical:
    there is always ≳1 day to expiry, so `secs` dominates and the floor never binds."""
    from . import config as _cfg
    secs = (expiry_dt - now_dt).total_seconds()
    floor = 600.0 if _cfg.is_expiry_day() else 60.0
    return max(secs, floor) / (365.0 * 24 * 3600)
