"""离线 Demo — 不用任何 API key，展示 wheel_summary 的输出样式

用法：
    python wheel_demo.py           # 默认 open 场景
    python wheel_demo.py open      # 开盘摘要
    python wheel_demo.py close     # 收盘摘要

工作原理：
    在 import 任何业务模块之前，先把 alpaca SDK 的几个数据类替换成
    返回合成数据的 Fake 类。然后业务代码 `from alpaca.data.historical
    import XxxClient` 时会 import 到 Fake。账户 client 通过直接设置
    AlpacaClients._trading 单例绕过。

    全部数据都是合成的，用 seed=42 保证输出稳定。
"""
from __future__ import annotations

import os
import sys
import random
import zlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# ── 1. 必须在 import config 之前设好假 env，否则 settings.py 会拿到空字符串 ──
os.environ.setdefault("ALPACA_API_KEY", "DEMO_KEY")
os.environ.setdefault("ALPACA_SECRET_KEY", "DEMO_SECRET")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
os.environ.setdefault("PAPER_TRADING", "true")

ET = ZoneInfo("America/New_York")
DEMO_SYMBOL = "DEMO"  # 不在 EARNINGS_DB 里 → 自动绕过财报过滤

# ──────────────────────────────────────────────────────────────────────
#  合成数据生成器
# ──────────────────────────────────────────────────────────────────────

def _fake_account():
    return SimpleNamespace(
        equity="52345.67",
        last_equity="51820.30",
        cash="50200.00",
        buying_power="100400.00",
        portfolio_value="52345.67",
    )

def _fake_clock():
    now = datetime.now()
    return SimpleNamespace(
        is_open=True,
        next_open=now + timedelta(hours=16),
        next_close=now + timedelta(hours=6),
    )


class _FakeTradingClient:
    """模拟 TradingClient — 覆盖 wheel_summary / scanner / health_check 用到的所有方法"""

    def get_account(self):
        return _fake_account()

    def get_clock(self):
        return _fake_clock()

    def get_all_positions(self):
        return []

    def get_open_position(self, sym):
        raise Exception(f"[demo] no stock position for {sym}")

    def get_orders(self, filter=None):  # noqa: A002  (signature matches SDK)
        return []

    def submit_order(self, req):
        return SimpleNamespace(id="DEMO-ORDER-001")

    def cancel_order_by_id(self, order_id):
        return None


def _deterministic_bars(symbol: str, n: int = 60) -> list:
    """生成稳定的合成日 bar 序列（seeded by symbol，跨进程稳定）"""
    # 用 crc32 而不是 hash()：Python 的 hash() 受 PYTHONHASHSEED 影响，每次启动都变
    rng = random.Random(zlib.crc32(symbol.encode("utf-8")) ^ 42)
    price = 170.0
    bars = []
    for _ in range(n):
        price += rng.uniform(-1.3, 1.3)
        bars.append(SimpleNamespace(
            close=round(price, 2),
            open=round(price - 0.05, 2),
            high=round(price + 0.4, 2),
            low=round(price - 0.4, 2),
            volume=1_000_000,
            trade_count=5000,
            vwap=round(price, 2),
        ))
    return bars


def _make_option_chain(sym: str, dte: int) -> dict:
    """生成合成 Put option chain — 3 个合约覆盖 delta 0.15/0.25/0.35"""
    today = date.today()
    expiry = today + timedelta(days=max(dte, 1))
    exp_str = expiry.strftime("%y%m%d")
    chain = {}
    # delta -0.25 就是 Wheel 目标 — 放中间确保被选中
    for strike, delta, mid in [(175.0, -0.15, 0.80),
                                (170.0, -0.25, 1.30),
                                (165.0, -0.35, 2.10)]:
        occ = f"{sym}{exp_str}P{int(strike * 1000):08d}"
        chain[occ] = SimpleNamespace(
            greeks=SimpleNamespace(
                delta=delta, gamma=0.015, theta=-0.06, vega=0.12, rho=0.01,
            ),
            latest_quote=SimpleNamespace(
                bid_price=mid - 0.10,
                ask_price=mid + 0.10,
            ),
            latest_trade=SimpleNamespace(price=mid),
            implied_volatility=0.45,
        )
    return chain


class _FakeOptionData:
    """模拟 OptionHistoricalDataClient"""

    def __init__(self, **kwargs):
        pass

    def get_option_chain(self, req):
        sym = getattr(req, "underlying_symbol", DEMO_SYMBOL) or DEMO_SYMBOL
        dte = 3
        gte = getattr(req, "expiration_date_gte", None)
        if gte is not None:
            dte = max((gte - date.today()).days, 1)
        return _make_option_chain(sym, dte)

    def get_option_snapshot(self, req):
        syms = getattr(req, "symbol_or_symbols", req)
        if isinstance(syms, str):
            syms = [syms]
        out = {}
        for s in syms:
            out[s] = SimpleNamespace(
                greeks=SimpleNamespace(delta=-0.25, gamma=0.015, theta=-0.06, vega=0.12),
                latest_quote=SimpleNamespace(bid_price=1.20, ask_price=1.40),
                implied_volatility=0.45,
            )
        return out


class _FakeStockData:
    """模拟 StockHistoricalDataClient"""

    def __init__(self, **kwargs):
        pass

    def _norm_syms(self, req):
        syms = getattr(req, "symbol_or_symbols", req)
        if isinstance(syms, str):
            return [syms]
        return list(syms)

    def get_stock_bars(self, req):
        out = {}
        for sym in self._norm_syms(req):
            out[sym] = _deterministic_bars(sym)
        return out

    def get_stock_latest_quote(self, req):
        return {
            s: SimpleNamespace(bid_price=170.00, ask_price=170.10)
            for s in self._norm_syms(req)
        }

    def get_stock_latest_trade(self, req):
        return {
            s: SimpleNamespace(price=170.05)
            for s in self._norm_syms(req)
        }


# ──────────────────────────────────────────────────────────────────────
#  安装 monkey-patches（必须在 import 业务模块之前！）
# ──────────────────────────────────────────────────────────────────────

def install_mocks():
    """先 patch alpaca.data.historical 里的类，然后 patch AlpacaClients 单例"""

    # 1. Patch alpaca SDK data classes —— 业务模块 `from alpaca.data.historical
    #    import OptionHistoricalDataClient` 时会拿到 Fake
    import alpaca.data.historical as _hist
    _hist.OptionHistoricalDataClient = _FakeOptionData
    _hist.StockHistoricalDataClient = _FakeStockData

    # 2. NewsClient 也 patch 掉（market_news.py 用）
    try:
        import alpaca.data.historical.news as _news_mod
        class _FakeNewsClient:
            def __init__(self, **kwargs): pass
            def get_news(self, req):
                return SimpleNamespace(data={"news": []})
        _news_mod.NewsClient = _FakeNewsClient
    except Exception:
        pass

    # 3. Patch AlpacaClients._trading 单例 — 绕过 TradingClient 初始化
    from core.alpaca_client import AlpacaClients
    AlpacaClients._trading = _FakeTradingClient()

    # 4. 把 WHEEL_SYMBOL 改成 DEMO（不碰 wheel_symbol.json）
    from config import settings
    settings.WHEEL_SYMBOL = DEMO_SYMBOL

    # 5. Patch wheel_switch — 避免读/写真实 wheel_symbol.json
    from strategy import wheel_switch
    wheel_switch.load_state = lambda: {
        "active_symbol": DEMO_SYMBOL, "plan": None, "history": [],
    }
    wheel_switch.save_state = lambda state: None
    wheel_switch.maybe_switch = lambda current_phase_is_idle=False: None
    wheel_switch.plan_switch = lambda *a, **kw: None
    wheel_switch.cancel_plan = lambda: None
    wheel_switch.get_active_symbol = lambda: DEMO_SYMBOL

    # 6. 压住 loguru 日志（让 demo 输出干净）
    try:
        from loguru import logger
        logger.remove()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────────────────────────────

def main():
    install_mocks()

    # 延迟 import —— 保证前面 patch 都生效
    from wheel_summary import build_summary

    event = sys.argv[1] if len(sys.argv) > 1 else "open"
    md = build_summary(event)
    print(md)

    print()
    print("━" * 64)
    print(f"[DEMO MODE] 以上是用合成数据生成的 {event.upper()} 摘要。")
    print("           标的 = DEMO，所有持仓/价格/期权/收益均为假数据。")
    print("")
    print("  要看真数据：")
    print("  1. 复制 .env.example → .env，填入 Alpaca key")
    print("  2. python check_setup.py   （环境自检）")
    print("  3. python wheel_summary.py open")


if __name__ == "__main__":
    main()
