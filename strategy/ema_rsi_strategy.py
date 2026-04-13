"""主策略: EMA 交叉 + RSI 过滤 + ML 信号融合"""
from __future__ import annotations
import pandas as pd
from core.event_bus import bus, SignalEvent, SignalDirection, DataEvent, EventType
from indicators.technical import add_all_indicators
from indicators.signal_generator import generate_rule_signals
from models.rf_classifier import RFClassifier
from strategy.position_sizer import calc_qty, calc_stop_price
from strategy.risk_manager import RiskManager
from config import settings
from loguru import logger


class EmaRsiStrategy:
    """
    信号融合逻辑:
      最终信号 = 规则信号 AND ML信号（置信度加权）
      两者一致 → 触发交易
      两者冲突 → 跳过（宁可不做，不冒险）
    """

    def __init__(self, model: RFClassifier, risk_manager: RiskManager):
        self.model        = model
        self.risk_manager = risk_manager
        # 每个标的的 K 线历史缓冲
        self._buffers: dict[str, pd.DataFrame] = {}
        self._positions: dict[str, dict] = {}  # {symbol: {qty, entry_price, side}}

        # 订阅数据事件
        bus.subscribe(EventType.DATA, self.on_data)

    def on_data(self, event: DataEvent):
        symbol = event.symbol
        bar    = event.bar

        # 追加到缓冲区
        buf = self._buffers.get(symbol, pd.DataFrame())
        buf = pd.concat([buf, bar.to_frame().T])
        buf = buf.tail(300)  # 最多保留 300 根 K 线
        self._buffers[symbol] = buf

        if len(buf) < 210:  # 指标需要足够长的数据才稳定
            return

        # 计算指标 + 规则信号
        df = add_all_indicators(buf)
        df = generate_rule_signals(df)
        row = df.iloc[-1]

        rule_signal = int(row["rule_signal"])
        force_exit  = bool(row["force_exit"])
        price       = float(row["close"])
        atr         = float(row["atr"]) if pd.notna(row["atr"]) else 0.0

        # 强制退出逻辑
        if force_exit and symbol in self._positions:
            logger.info(f"[{symbol}] RSI 极值，强制退出持仓")
            self._emit_exit(symbol, price)
            return

        # 无规则信号则不操作
        if rule_signal == 0:
            return

        # ML 信号融合
        ml_dir, ml_conf = self.model.predict(df)

        direction_match = (rule_signal == 1 and ml_dir == 1) or \
                          (rule_signal == -1 and ml_dir == -1)

        if not direction_match:
            logger.debug(f"[{symbol}] 规则({rule_signal}) 与 ML({ml_dir}) 信号冲突，跳过")
            return

        if ml_conf < settings.ML_CONFIDENCE_THRESHOLD:
            logger.debug(f"[{symbol}] ML 置信度 {ml_conf:.2f} < {settings.ML_CONFIDENCE_THRESHOLD}，跳过")
            return

        # 发布信号事件
        direction = SignalDirection.BUY if rule_signal == 1 else SignalDirection.SELL
        signal = SignalEvent(
            symbol=symbol,
            direction=direction,
            confidence=ml_conf,
            price=price,
        )
        logger.info(
            f"[{symbol}] 信号: {direction.value} | "
            f"price={price:.2f} | ML置信度={ml_conf:.2f} | ATR={atr:.4f}"
        )
        bus.publish(signal)

    def _emit_exit(self, symbol: str, price: float):
        pos = self._positions.get(symbol)
        if not pos:
            return
        exit_dir = SignalDirection.SELL if pos["side"] == "buy" else SignalDirection.BUY
        bus.publish(SignalEvent(
            symbol=symbol,
            direction=exit_dir,
            confidence=1.0,
            price=price,
        ))

    def register_fill(self, symbol: str, side: str, qty: float, price: float):
        """订单成交后更新本地持仓记录"""
        if side in ("buy", "long"):
            self._positions[symbol] = {"qty": qty, "entry_price": price, "side": "buy"}
        else:
            self._positions.pop(symbol, None)

    def get_position(self, symbol: str) -> dict | None:
        return self._positions.get(symbol)
