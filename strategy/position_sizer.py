"""仓位计算 — ATR 波动率法"""
from config import settings
from loguru import logger


def calc_qty(
    equity: float,
    price: float,
    atr: float,
    risk_ratio: float = settings.RISK_PER_TRADE,
    atr_multiplier: float = settings.ATR_STOP_MULTIPLIER,
    max_position_ratio: float = settings.MAX_POSITION_RATIO,
) -> int:
    """
    ATR 波动率仓位法:
      风险金额 = equity × risk_ratio
      止损距离 = ATR × atr_multiplier
      股数     = 风险金额 ÷ 止损距离
    上限: 单标的市值 ≤ equity × max_position_ratio
    """
    if atr <= 0 or price <= 0:
        return 0

    risk_amount   = equity * risk_ratio
    stop_distance = atr * atr_multiplier
    qty           = risk_amount / stop_distance

    # 最大仓位上限约束
    max_qty = (equity * max_position_ratio) / price
    qty = min(qty, max_qty)
    qty = max(1, int(qty))

    logger.debug(
        f"仓位计算: equity={equity:.0f}, price={price:.2f}, "
        f"atr={atr:.4f}, stop={stop_distance:.4f}, qty={qty}"
    )
    return qty


def calc_stop_price(price: float, atr: float, side: str = "buy") -> float:
    """计算止损价"""
    dist = atr * settings.ATR_STOP_MULTIPLIER
    if side == "buy":
        return round(price - dist, 2)
    return round(price + dist, 2)
