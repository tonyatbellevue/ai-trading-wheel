"""Black-Scholes 期权定价与希腊字母 — 回测用

因为 Alpaca 只提供近期期权 Greeks，历史回测需要自己定价。
用 BS 模型 + 历史实现波动率（加 IV premium）作为 IV 估计。
"""
from __future__ import annotations
import math
from math import log, sqrt, exp, erf
from dataclasses import dataclass


def _N(x: float) -> float:
    """标准正态 CDF"""
    return 0.5 * (1 + erf(x / sqrt(2)))


def _n(x: float) -> float:
    """标准正态 PDF"""
    return exp(-0.5 * x * x) / sqrt(2 * math.pi)


@dataclass
class OptionQuote:
    price: float          # 期权理论价
    delta: float          # Delta
    gamma: float
    theta: float          # per day
    vega: float
    iv: float
    strike: float
    dte: int
    type: str             # "P" or "C"


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes 欧式期权价格。T 单位: 年。"""
    if T <= 0 or sigma <= 0:
        # 到期或无波动率
        intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
        return intrinsic
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if is_call:
        return S * _N(d1) - K * exp(-r * T) * _N(d2)
    else:
        return K * exp(-r * T) * _N(-d2) - S * _N(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> tuple:
    """返回 (delta, gamma, theta_per_day, vega_per_1pct)"""
    if T <= 0 or sigma <= 0:
        delta = 1.0 if (is_call and S > K) else (-1.0 if (not is_call and S < K) else 0.0)
        return delta, 0.0, 0.0, 0.0
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    gamma = _n(d1) / (S * sigma * sqrt(T))
    vega = S * _n(d1) * sqrt(T) / 100  # per 1% IV move
    if is_call:
        delta = _N(d1)
        theta = (-S * _n(d1) * sigma / (2 * sqrt(T)) - r * K * exp(-r * T) * _N(d2)) / 365
    else:
        delta = _N(d1) - 1
        theta = (-S * _n(d1) * sigma / (2 * sqrt(T)) + r * K * exp(-r * T) * _N(-d2)) / 365
    return delta, gamma, theta, vega


def find_strike_by_delta(S: float, target_delta: float, T: float, r: float, sigma: float,
                         is_call: bool, step: float = 2.5) -> float:
    """搜索使 Delta 最接近 target_delta 的 strike。

    target_delta: for put 传入负值 (e.g. -0.25), for call 传正值 (e.g. 0.25)
    step: strike 搜索步长 (TSLA 常规 $2.5)
    """
    # 围绕当前价 ±30% 搜索
    strikes = []
    k = round(S * 0.7 / step) * step
    while k <= S * 1.3:
        strikes.append(k)
        k += step

    best_k = S
    best_diff = float("inf")
    for K in strikes:
        d, _, _, _ = bs_greeks(S, K, T, r, sigma, is_call)
        diff = abs(d - target_delta)
        if diff < best_diff:
            best_diff = diff
            best_k = K
    return best_k


def quote_option(S: float, strike: float, dte: int, iv: float, is_call: bool,
                 r: float = 0.045) -> OptionQuote:
    """完整定价 + Greeks"""
    T = max(dte, 0) / 365.0
    price = bs_price(S, strike, T, r, iv, is_call)
    delta, gamma, theta, vega = bs_greeks(S, strike, T, r, iv, is_call)
    return OptionQuote(
        price=max(price, 0.01),  # 最低 $0.01
        delta=delta, gamma=gamma, theta=theta, vega=vega,
        iv=iv, strike=strike, dte=dte,
        type="C" if is_call else "P",
    )
