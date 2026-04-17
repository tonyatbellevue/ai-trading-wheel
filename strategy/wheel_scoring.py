"""Shared v3 scoring for wheel-candidate evaluation.

Used by both the offline rotation backtest (`backtest/rotation_backtest.py`)
and the live shadow-rotation tracker (`metrics/shadow_rotation.py`) so the
score formula, feature extraction, and RV calculation stay in one place.

The v3 philosophy: reward stability (low RV, small drawdown, IV sweet zone)
over raw premium. Backtest-validated +11.7% annualized vs -2.3% Fixed-TSLA
over 24 months (MaxDD 9% vs 46%).
"""
from __future__ import annotations
from math import log, sqrt
from statistics import mean, stdev
from typing import Optional

import pandas as pd

from backtest.wheel_pricing import quote_option, find_strike_by_delta


# ── Component weights (must sum to ~1.0 for interpretability) ────────
PREMIUM_WEIGHT     = 0.25
STABILITY_WEIGHT   = 0.30
DRAWDOWN_WEIGHT    = 0.15
IV_WEIGHT          = 0.15
TREND_WEIGHT       = 0.10
MOMENTUM_WEIGHT    = 0.05

# Catastrophe penalty: any single-day drop > 10% in past 30d → -30 flat
CATASTROPHE_DROP   = -0.10
CATASTROPHE_POINTS = -30

# IV sweet zone
IV_PREMIUM         = 1.15       # IV ≈ realized_vol × 1.15


# ── Primitives ────────────────────────────────────────────────────────

def realized_vol(closes: list[float], window: int = 20) -> float:
    """Annualized realized volatility from close series (std of log returns × √252)."""
    if len(closes) < window + 1:
        return 0.30
    rets = [log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))]
    return stdev(rets) * sqrt(252) if len(rets) > 1 else 0.30


def extract_features(closes: list[float]) -> Optional[dict]:
    """Turn a close-price series into the feature dict scorer expects.

    Expects at least 50 points; returns None if too short to compute MA50.
    """
    if len(closes) < 50:
        return None
    price = closes[-1]
    ma50 = mean(closes[-50:])
    ma10 = mean(closes[-10:])
    rv = realized_vol(closes, 20)
    iv_est = rv * IV_PREMIUM

    # Momentum at several horizons
    momentum_5d = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
    momentum_20d = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0
    momentum_126d = ((closes[-1] - closes[0]) / closes[0]
                     if len(closes) >= 126 else momentum_20d)

    # Worst single-day drop in past 30 sessions
    window_30 = closes[-30:] if len(closes) >= 30 else closes
    max_drop_30d = min((window_30[i] - window_30[i - 1]) / window_30[i - 1]
                       for i in range(1, len(window_30))) if len(window_30) > 1 else 0

    # Peak-to-trough drawdown in past 30 sessions
    max_dd_30d = 0.0
    peak = window_30[0] if window_30 else 0
    for c in window_30:
        peak = max(peak, c)
        dd = (peak - c) / peak if peak > 0 else 0
        max_dd_30d = max(max_dd_30d, dd)

    # Trend consistency: fraction of last 20 closes above MA50
    trend_consistency = (sum(1 for c in closes[-20:] if c > ma50)
                         / min(20, len(closes)))

    return {
        "price": price, "ma50": ma50, "ma10": ma10,
        "rv": rv, "iv": iv_est,
        "momentum_5d": momentum_5d,
        "momentum_20d": momentum_20d,
        "momentum_126d": momentum_126d,
        "trend_consistency": trend_consistency,
        "max_drop_30d": max_drop_30d,
        "max_dd_30d": max_dd_30d,
        "above_ma50": price > ma50,
    }


def extract_features_from_df(df: pd.DataFrame, idx: int) -> Optional[dict]:
    """Backtest helper: pull last 126 closes from DataFrame ending at idx."""
    if idx < 50:
        return None
    lookback_start = max(0, idx - 126)
    closes = [float(df.iloc[i]["close"]) for i in range(lookback_start, idx + 1)]
    return extract_features(closes)


# ── Score ─────────────────────────────────────────────────────────────

def score_symbol(features: Optional[dict],
                  dte: int = 3,
                  target_delta: float = -0.25,
                  r: float = 0.045) -> float:
    """v3 score: stability-weighted suitability for selling 25-delta puts.

    Returns a real number (typically 0-60); higher = better candidate.
    Negative is possible if catastrophe penalty applies.
    """
    if not features:
        return 0
    S = features["price"]
    iv = features["iv"]
    rv = features["rv"]
    if iv <= 0 or S <= 0:
        return 0

    T = dte / 365
    try:
        strike = find_strike_by_delta(S, target_delta, T, r, iv, is_call=False)
        q = quote_option(S, strike, dte, iv, is_call=False, r=r)
        premium = q.price
    except Exception:
        return 0
    if premium <= 0 or strike <= 0:
        return 0

    # 1. Annualized premium yield, capped at 150% to avoid outlier dominance
    annualized = (premium / strike) * (365 / dte) * 100
    premium_score = min(annualized, 150) * PREMIUM_WEIGHT

    # 2. Low RV is rewarded (RV 20% → +100, 40% → +50, 60% → 0, 80% → -50)
    stability_score = max(-50, 100 - rv * 100 * 1.5) * STABILITY_WEIGHT

    # 3. Small recent drawdown is rewarded (5% DD → +50, 15% → 0, 25% → -50)
    dd_pct = features["max_dd_30d"] * 100
    dd_score = max(-50, 50 - (dd_pct - 5) * 5) * DRAWDOWN_WEIGHT

    # 4. IV sweet zone 30-60% preferred
    if 0.30 <= iv <= 0.60:
        iv_score = 50 * IV_WEIGHT
    elif 0.20 <= iv <= 0.75:
        iv_score = 25 * IV_WEIGHT
    else:
        iv_score = -25 * IV_WEIGHT

    # 5. Trend consistency (fraction of 20d above MA50)
    trend_score = (features["trend_consistency"] - 0.5) * 100 * TREND_WEIGHT

    # 6. Short-term momentum: small positive preferred, extremes penalized
    m5 = features["momentum_5d"]
    if -0.02 <= m5 <= 0.04:
        mom_score = 30 * MOMENTUM_WEIGHT
    elif abs(m5) > 0.08:
        mom_score = -20 * MOMENTUM_WEIGHT
    else:
        mom_score = 0

    # 7. Hard catastrophe penalty
    catastrophe = (CATASTROPHE_POINTS
                   if features["max_drop_30d"] < CATASTROPHE_DROP
                   else 0)

    total = (premium_score + stability_score + dd_score + iv_score
             + trend_score + mom_score + catastrophe)
    return round(total, 2)
