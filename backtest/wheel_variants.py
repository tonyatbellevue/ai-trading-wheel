"""Wheel 策略变体配置 — 每个变体开关一组改进，用于回测对比"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict


@dataclass
class WheelConfig:
    """单个 Wheel 策略变体的完整配置"""
    name: str = "baseline"

    # ── 基础参数 ──
    target_delta: float = 0.25       # 目标 |Delta|
    min_dte: int = 2                 # 最短到期
    max_dte: int = 4                 # 最长到期（3-day wheel: Mon→Wed, Wed→Fri, Fri→Mon）
    contracts: int = 2               # 固定合约数
    iv_premium: float = 1.15         # IV ≈ realized_vol × 1.15（经验值）

    # ── 改进开关 ──
    use_earnings_filter: bool = False        # 跳过有财报的周
    earnings_dates: list = field(default_factory=list)  # ISO 日期列表

    use_gap_filter: bool = False             # 盘前 -3% 跳空时不卖 Put
    gap_threshold: float = -0.03

    use_itm_roll: bool = False               # Put ITM 时滚到下一周更低 strike
    roll_down_pct: float = 0.02              # 滚单时 strike 下移 2%

    use_kelly_sizing: bool = False           # 动态仓位
    kelly_fraction: float = 0.25             # Kelly 系数折扣（f*/4）
    win_rate_est: float = 0.85               # Kelly 所需的胜率估计

    use_iv_dynamic_delta: bool = False       # 高 IV 卖更远 OTM
    iv_low: float = 0.40                     # IV < 40% 用 target_delta
    iv_high: float = 0.70                    # IV > 70% 用 target_delta × 0.6
    low_iv_skip: bool = False                # IV < iv_low 时完全不开仓

    use_ma_trend: bool = False               # 股价 < 50d MA 时不卖 CSP（只卖 CC）
    ma_window: int = 50

    use_50pct_profit: bool = False           # 达到 50% 收益提前平仓
    profit_target: float = 0.50

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("earnings_dates", None)
        return d


# ── 预设变体 ──
def make_variants(earnings_dates: list) -> list[WheelConfig]:
    base_kwargs = dict(earnings_dates=earnings_dates)
    return [
        WheelConfig(name="01_baseline", **base_kwargs),

        WheelConfig(name="02_earnings_filter",
                    use_earnings_filter=True, **base_kwargs),

        WheelConfig(name="03_gap_filter",
                    use_gap_filter=True, **base_kwargs),

        WheelConfig(name="04_itm_roll",
                    use_itm_roll=True, **base_kwargs),

        WheelConfig(name="05_kelly_sizing",
                    use_kelly_sizing=True, **base_kwargs),

        WheelConfig(name="06_iv_dynamic_delta",
                    use_iv_dynamic_delta=True, **base_kwargs),

        WheelConfig(name="07_low_iv_skip",
                    use_iv_dynamic_delta=True, low_iv_skip=True, **base_kwargs),

        WheelConfig(name="08_ma_trend",
                    use_ma_trend=True, **base_kwargs),

        WheelConfig(name="09_50pct_profit",
                    use_50pct_profit=True, **base_kwargs),

        # 组合最佳候选
        WheelConfig(name="10_combo_earnings_gap_roll",
                    use_earnings_filter=True,
                    use_gap_filter=True,
                    use_itm_roll=True,
                    **base_kwargs),

        WheelConfig(name="11_combo_all_filters",
                    use_earnings_filter=True,
                    use_gap_filter=True,
                    use_itm_roll=True,
                    use_iv_dynamic_delta=True,
                    use_ma_trend=True,
                    **base_kwargs),

        # ★ 最终整合版 = 回测验证 ≥3/5 胜场次的改进
        WheelConfig(name="12_PRODUCTION",
                    use_earnings_filter=True,
                    use_ma_trend=True,
                    use_kelly_sizing=True,
                    **base_kwargs),
    ]
