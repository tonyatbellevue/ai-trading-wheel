"""全局配置中心 — 所有可调参数集中于此

注意：本模块自动检测 git worktree 环境。如果当前在 worktree 中运行，
.env 会从主项目根目录加载，避免 worktree 与主项目凭证不同步。
"""
import os
from pathlib import Path
from dotenv import load_dotenv


def _resolve_env_path() -> Path:
    """智能定位 .env 文件

    1. 优先使用环境变量 AI_TRADING_ENV_PATH 指定的路径
    2. 如果当前在 git worktree 中，从主项目根加载
    3. 否则使用当前项目根的 .env
    """
    # 1. 环境变量覆盖
    env_override = os.environ.get("AI_TRADING_ENV_PATH")
    if env_override and Path(env_override).exists():
        return Path(env_override)

    cur_root = Path(__file__).resolve().parent.parent  # config/.. → 项目根
    git_marker = cur_root / ".git"

    # 2. worktree 检测：.git 是文件且包含 "gitdir:" 指向主项目
    if git_marker.is_file():
        try:
            content = git_marker.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir = content.split(":", 1)[1].strip()
                # gitdir 形如 ".../主项目/.git/worktrees/<name>"
                # 主项目 .git 路径 = gitdir 去掉 "/worktrees/<name>"
                main_git = Path(gitdir).parent.parent  # /worktrees/<name> 上两级
                main_root = main_git.parent  # .git 的父目录 = 主项目根
                main_env = main_root / ".env"
                if main_env.exists():
                    return main_env
        except Exception:
            pass

    # 3. 默认：当前项目根
    return cur_root / ".env"


_ENV_PATH = _resolve_env_path()
load_dotenv(dotenv_path=_ENV_PATH, override=True)
_ENV_LOADED_FROM = str(_ENV_PATH)  # 用于调试和健康检查

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

# ── 邮件通知 ────────────────────────────────────────────────────────────────
EMAIL_SMTP_SERVER = os.getenv("EMAIL_SMTP_SERVER", "smtp-mail.outlook.com")
EMAIL_SMTP_PORT   = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_SENDER      = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT   = os.getenv("EMAIL_RECIPIENT", "bellevuetony@hotmail.com")

# ── 路径 ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_CACHE   = os.path.join(BASE_DIR, "data_cache")
MODEL_STORE  = os.path.join(BASE_DIR, "models", "model_store")
REPORTS_DIR  = os.path.join(BASE_DIR, "reports")
LOGS_DIR     = os.path.join(BASE_DIR, "logs")

# ── Wheel 期权策略 ───────────────────────────────────────────────────────────
# WHEEL_SYMBOL 从 wheel_symbol.json 动态读取（支持自动切换）
def _load_wheel_symbol() -> str:
    import json
    p = os.path.join(BASE_DIR, "wheel_symbol.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f).get("active_symbol", "TSLA")
        except Exception:
            pass
    return "TSLA"

WHEEL_SYMBOL         = _load_wheel_symbol()
WHEEL_TARGET_DELTA   = 0.25     # 卖出期权目标 Delta 绝对值
WHEEL_MIN_DTE        = 1        # 最短到期天数（3-day weekly）
WHEEL_MAX_DTE        = 5        # 最长到期天数（3-day weekly）
WHEEL_CONTRACTS      = 2        # 遗留字段：仅供 run_wheel_*.py 启动日志/Web Dashboard 展示
                                # 实际合约数完全由 kelly_contracts() 动态算（4 层资金防护），
                                # Sell Put 跟资金走，Sell Call 跟持股走（持 N 股 → 卖 N/100 张）
WHEEL_CHECK_INTERVAL = 300      # 持续监控检查间隔（秒）

# ── 权利金质量门槛 ──
# 年化收益率 = (权利金 / 行权价) × (365 / DTE)
# 低于门槛说明 IV 不足以补偿风险，跳过该笔交易。
# 专家建议：保守 20%，激进 50%。默认 20% 匹配 Wheel 策略目标收益。
MIN_PREMIUM_ANNUALIZED   = 0.20     # 最低年化收益率 20%

# ── 资金安全缓冲（防止爆仓/平仓）──
# 所有新开仓前必须保证：buying_power - 最小现金保留 ≥ strike × 100 × qty
CASH_BUFFER_PCT          = 0.10     # 权益的 10% 必须保留为现金，应对黑天鹅
MAX_SINGLE_POSITION_PCT  = 0.50     # 单个新仓位不超过权益 50%（v8: 40→50, 提高
                                     # BP 利用率从 39%→~50% 以接近 25% 年化目标，
                                     # 11 笔历史交易中多数单子被旧 40% 上限限制为 1 张，
                                     # 改 50% 后多数单子可开 2 张，预期年化 18%→~22-26%）
MAX_TOTAL_EXPOSURE_PCT   = 0.90     # 所有 Put 抵押之和不超过权益 90%（防爆仓）
MAX_SECTOR_EXPOSURE_PCT  = 0.60     # 同一 sector 所有 Put 抵押不超过权益 60%（防 sector beta）
MAX_CONSECUTIVE_LOSSES   = 3        # 连亏 N 周期 → 强制换标的（stop-loss 逃生）

# ── v12: 并行多 wheel ──
# 同时运行 N 个独立 wheel（每个不同标的）。每个 wheel 自己管自己的状态机。
# 跨 wheel 风险控制通过 MAX_TOTAL_EXPOSURE_PCT (90%) + MAX_SECTOR_EXPOSURE_PCT (60%)
# 自动协调。设 1 = 保持单仓模式（原行为）。设 2 = 并行 2 个独立标的的 wheel。
# 预期：年化收益 22% → 35-40%，回撤 6% → 10-12%。
MAX_CONCURRENT_WHEELS    = 2
