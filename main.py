"""AI 交易机器人主入口 — Paper Trading 实盘"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signal
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from core.event_bus import bus, EventType, SignalEvent, FillEvent
from core.alpaca_client import AlpacaClients
from data.live_feed import LiveFeed
from execution.order_manager import OrderManager
from execution.portfolio_manager import PortfolioManager
from strategy.ema_rsi_strategy import EmaRsiStrategy
from strategy.risk_manager import RiskManager
from strategy.position_sizer import calc_qty, calc_stop_price
from account.account_dashboard import AccountDashboard
from models.rf_classifier import RFClassifier
from config import settings
from loguru import logger

ET = pytz.timezone("America/New_York")


class TradingBot:
    def __init__(self):
        logger.info("初始化 AI 交易机器人...")

        # 核心组件
        self.orders    = OrderManager()
        self.portfolio = PortfolioManager()
        self.dashboard = AccountDashboard()

        # 风控管理器
        equity = self.portfolio.get_equity()
        self.risk_mgr = RiskManager(initial_equity=equity)

        # ML 模型
        self.model = RFClassifier()
        try:
            self.model.load()
        except FileNotFoundError:
            logger.warning("ML 模型未找到，使用纯规则模式。请先运行 train_model.py")
            self.model = None

        # 策略
        self.strategy = EmaRsiStrategy(model=self.model, risk_manager=self.risk_mgr)

        # 实时行情
        self.live_feed = LiveFeed(symbols=settings.SYMBOLS)

        # 订阅信号事件 → 执行
        bus.subscribe(EventType.SIGNAL, self._on_signal)

        # 定时任务
        self.scheduler = BackgroundScheduler(timezone=ET)
        self._setup_scheduler()

        logger.info(f"交易机器人就绪 | 标的: {settings.SYMBOLS} | Paper Mode: {settings.PAPER}")

    def _setup_scheduler(self):
        # 每日 09:31 重置日内风控
        self.scheduler.add_job(
            self._daily_open, CronTrigger(hour=9, minute=31, day_of_week="mon-fri", timezone=ET),
        )
        # 每日 15:50 收盘前检查持仓
        self.scheduler.add_job(
            self._pre_close, CronTrigger(hour=15, minute=50, day_of_week="mon-fri", timezone=ET),
        )
        # 每分钟打印持仓状态
        self.scheduler.add_job(self._status_tick, "interval", minutes=5)

    def _daily_open(self):
        logger.info("开盘！重置日内风控...")
        equity = self.portfolio.get_equity()
        self.risk_mgr.reset_daily(equity)
        self.dashboard.show_account()

    def _pre_close(self):
        logger.info("临近收盘，检查持仓...")
        self.portfolio.print_summary()

    def _status_tick(self):
        equity = self.portfolio.get_equity()
        positions = self.portfolio.get_positions()
        logger.info(f"权益: ${equity:,.2f} | 持仓数: {len(positions)}")

    def _on_signal(self, event: SignalEvent):
        """收到策略信号 → 执行订单"""
        symbol    = event.symbol
        direction = event.direction.value
        price     = event.price

        try:
            equity     = self.portfolio.get_equity()
            self.risk_mgr.check_daily_loss(equity)
            positions  = self.portfolio.get_positions()
            open_count = len(positions)

            if direction == "BUY":
                # 已有持仓则跳过
                if symbol in positions:
                    return
                self.risk_mgr.check_position_count(open_count)

                # 计算仓位
                pos_info = self.strategy.get_position(symbol)
                atr = 0.0  # 实盘中从最新 K 线读取
                qty = calc_qty(equity, price, atr if atr > 0 else price * 0.02)

                order_id = self.orders.market_buy(symbol, qty)
                stop_px  = calc_stop_price(price, max(atr, price * 0.02), "buy")
                self.orders.stop_loss(symbol, qty, stop_px)
                self.strategy.register_fill(symbol, "buy", qty, price)

                fill = FillEvent(symbol=symbol, side="buy", qty=qty,
                                 price=price, order_id=order_id)
                bus.publish(fill)

            elif direction == "SELL":
                pos = positions.get(symbol)
                if not pos:
                    return
                qty = int(pos["qty"])
                self.orders.cancel_all_orders()   # 先撤止损单
                order_id = self.orders.market_sell(symbol, qty)
                self.strategy.register_fill(symbol, "sell", qty, price)

        except Exception as e:
            logger.error(f"[{symbol}] 执行信号失败: {e}")

    def start(self):
        logger.info("=" * 60)
        logger.info("  AI 交易机器人启动")
        logger.info("=" * 60)
        self.dashboard.show_all()
        self.scheduler.start()

        # 捕获 Ctrl+C
        def shutdown(sig, frame):
            logger.info("收到退出信号，正在关闭...")
            self.scheduler.shutdown(wait=False)
            self.live_feed.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT, shutdown)

        # 启动实时行情（阻塞）
        self.live_feed.start()


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
