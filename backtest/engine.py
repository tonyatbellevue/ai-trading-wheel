"""事件驱动回测引擎"""
from __future__ import annotations
import pandas as pd
from tqdm import tqdm
from indicators.technical import add_all_indicators
from indicators.signal_generator import generate_rule_signals
from indicators.feature_builder import build_features
from backtest.broker_simulator import SimBroker
from backtest.performance import calc_metrics
from models.rf_classifier import RFClassifier
from strategy.position_sizer import calc_qty, calc_stop_price
from config import settings
from loguru import logger


class BacktestEngine:
    def __init__(self, model: RFClassifier | None = None):
        self.model  = model
        self.broker = SimBroker()

    def run(self,
            data: dict[str, pd.DataFrame],
            warmup: int = 210) -> dict:
        """
        data: {symbol: OHLCV DataFrame}
        warmup: 前 N 根 K 线仅用于指标预热，不下单
        """
        logger.info(f"回测启动 | 标的: {list(data.keys())} | 初始资金: ${self.broker.initial_capital:,.0f}")

        # 预先计算所有指标
        processed: dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            df = add_all_indicators(df)
            df = generate_rule_signals(df)
            if self.model:
                df = build_features(df)
            processed[sym] = df.dropna()

        # 构建统一时间轴
        all_dates = sorted(set().union(*[set(df.index) for df in processed.values()]))
        logger.info(f"共 {len(all_dates)} 个时间点")

        for i, ts in enumerate(tqdm(all_dates, desc="回测进度")):
            if i < warmup:
                continue

            prices = {}
            for sym, df in processed.items():
                if ts not in df.index:
                    continue
                prices[sym] = float(df.loc[ts, "close"])

                row     = df.loc[ts]
                history = df[df.index <= ts]
                atr     = float(row["atr"]) if pd.notna(row.get("atr", float("nan"))) else 0
                price   = float(row["close"])

                # ── 检查止损 ──────────────────────────────────────────
                if sym in self.broker.positions:
                    next_idx = i + 1
                    if next_idx < len(all_dates):
                        next_ts = all_dates[next_idx]
                        if next_ts in df.index:
                            next_bar = df.loc[next_ts]
                            if float(next_bar["low"]) <= self.broker.positions[sym].stop_price:
                                self.broker.sell(sym, next_bar, reason="stop_loss")
                                continue

                # ── 已持仓则跳过信号检查 ─────────────────────────────
                if sym in self.broker.positions:
                    # 检查强制退出
                    if row.get("force_exit", False):
                        next_idx = i + 1
                        if next_idx < len(all_dates):
                            next_ts = all_dates[next_idx]
                            if next_ts in df.index:
                                self.broker.sell(sym, df.loc[next_ts], reason="force_exit")
                    continue

                # ── 信号检查 ─────────────────────────────────────────
                rule_signal = int(row.get("rule_signal", 0))
                if rule_signal != 1:
                    continue

                # ML 信号过滤
                if self.model:
                    ml_dir, ml_conf = self.model.predict(history.tail(settings.FEATURE_LOOKBACK + 50))
                    if ml_dir != 1 or ml_conf < settings.ML_CONFIDENCE_THRESHOLD:
                        continue

                # ── 计算仓位并下单 ───────────────────────────────────
                equity = self.broker.mark_to_market(prices)
                if len(self.broker.positions) >= settings.MAX_OPEN_POSITIONS:
                    continue

                qty = calc_qty(equity, price, atr)
                if qty < 1:
                    continue

                # 以下一根 K 线开盘价成交
                next_idx = i + 1
                if next_idx >= len(all_dates):
                    continue
                next_ts = all_dates[next_idx]
                if next_ts not in df.index:
                    continue
                self.broker.buy(sym, qty, df.loc[next_ts], atr)

            # 记录权益快照
            if prices:
                self.broker.mark_to_market(prices)

        # ── 收盘强制平仓 ─────────────────────────────────────────────
        last_bars = {sym: df.iloc[-1] for sym, df in processed.items()
                     if sym in self.broker.positions}
        for sym, bar in last_bars.items():
            self.broker.sell(sym, bar, reason="end_of_backtest")

        return calc_metrics(
            self.broker.equity_curve,
            self.broker.trades,
            self.broker.initial_capital,
        )
