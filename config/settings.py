"""全局配置中心 — 所有可调参数集中于此"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Alpaca API ──────────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY", "").strip()
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
PAPER      = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ── 交易标的 ────────────────────────────────────────────────────────────────
SYMBOLS = ["AAPL", "TSLA", "NVDA", "MSFT", "SPY"]

# ── 时间周期 ────────────────────────────────────────────────────────────────
TIMEFRAME     = "1Min"   # 实盘数据粒度: 1Min / 5Min / 15Min / 1Day
BACKTEST_TIMEFRAME = "1Day"

# ── 策略参数 ────────────────────────────────────────────────────────────────
EMA_SHORT   = 9
EMA_LONG    = 21
SMA_FILTER  = 50
RSI_PERIOD  = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30
RSI_ENTRY_MIN  = 40
RSI_ENTRY_MAX  = 65
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
ATR_PERIOD  = 14

# ── ML 模型参数 ──────────────────────────────────────────────────────────────
ML_CONFIDENCE_THRESHOLD = 0.60   # 最低置信度才触发信号
ML_LABEL_HORIZON        = 5      # 预测未来N根K线的涨跌
ML_LABEL_THRESHOLD      = 0.005  # 涨跌超过0.5%才算方向性信号
FEATURE_LOOKBACK        = 20     # 特征回看窗口

# ── 风险控制 ────────────────────────────────────────────────────────────────
RISK_PER_TRADE        = 0.01    # 单笔风险占总权益比例 (1%)
ATR_STOP_MULTIPLIER   = 2.0     # 止损距离 = ATR × 系数
MAX_POSITION_RATIO    = 0.10    # 单标的最多占总权益 10%
MAX_OPEN_POSITIONS    = 5       # 同时最多持仓数量
MAX_DAILY_LOSS        = 0.05    # 日内最大亏损 5% 触发熔断
MAX_TRADE_LOSS        = 0.02    # 单笔最大亏损 2%

# ── 回测参数 ────────────────────────────────────────────────────────────────
BACKTEST_START    = "2023-01-01"
BACKTEST_END      = "2024-12-31"
INITIAL_CAPITAL   = 100_000.0   # 初始资金 10万美元
COMMISSION_PER_SHARE = 0.005    # 每股手续费
SLIPPAGE_FACTOR   = 0.001       # 滑点系数

# ── 路径 ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_CACHE   = os.path.join(BASE_DIR, "data_cache")
MODEL_STORE  = os.path.join(BASE_DIR, "models", "model_store")
REPORTS_DIR  = os.path.join(BASE_DIR, "reports")
LOGS_DIR     = os.path.join(BASE_DIR, "logs")

# ── Wheel 期权策略 ───────────────────────────────────────────────────────────
WHEEL_SYMBOL         = "TSLA"   # 标的股票
WHEEL_TARGET_DELTA   = 0.25     # 卖出期权目标 Delta 绝对值
WHEEL_MIN_DTE        = 1        # 最短到期天数（3-day weekly）
WHEEL_MAX_DTE        = 5        # 最长到期天数（3-day weekly）
WHEEL_CONTRACTS      = 2        # 每次卖出合约数（受购买力限制）
WHEEL_CHECK_INTERVAL = 300      # 持续监控检查间隔（秒）
