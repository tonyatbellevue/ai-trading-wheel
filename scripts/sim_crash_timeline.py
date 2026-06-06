"""端到端模拟: Phase 4.1 升级止损在真实崩盘时间线下的行为。

与 test_escalating_stop.py (单次调用单元测试) 不同, 这个脚本模拟连续多个
cron tick (每 8 分钟一次) 的完整序列, 用真实的 stop_state.py 持久化, 验证
跨 tick 的状态机:
  - 持续崩盘 → 限价 → 限价 → 市价 升级
  - 中途反弹 → 重置 → 再崩 → 从限价重新开始
  - 熔断 → 等待不计数 → 恢复后继续

用真实 _try_stock_stop_loss + 真实 stop_state (跨进程持久化逻辑), 仅 mock
Alpaca trading client。结束自动清理状态。

Run: python scripts/sim_crash_timeline.py
"""
from __future__ import annotations
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from types import SimpleNamespace
from unittest.mock import patch
from strategy.wheel_strategy import WheelStrategy
from metrics import stop_state

SYM = "SIMCRASH"
COST = 441.0


class FakeTrading:
    def __init__(self):
        self.orders = []
        self.open_sells = []
        self.tradable = True
        self.live_qty = 100

    def get_asset(self, s): return SimpleNamespace(tradable=self.tradable)
    def get_open_position(self, s): return SimpleNamespace(qty=self.live_qty)
    def get_orders(self, filter=None): return list(self.open_sells)

    def cancel_order_by_id(self, oid):
        self.open_sells = [o for o in self.open_sells if o.id != oid]

    def submit_order(self, req):
        otype = "MARKET" if "Market" in type(req).__name__ else "LIMIT"
        lim = getattr(req, "limit_price", None)
        self.orders.append((otype, lim))
        # simulate the order resting open (unfilled) for next-tick re-price test
        if otype == "LIMIT":
            self.open_sells = [SimpleNamespace(id=f"ord{len(self.orders)}",
                                               symbol=SYM, side="OrderSide.SELL")]
        else:
            self.open_sells = []
        return SimpleNamespace(id=f"ord{len(self.orders)}")


def tick(w, fake, price, label):
    """One cron tick: drive the -35% decision path with a given stock price."""
    fake.orders.clear()
    drop = (price - COST) / COST
    obj = SimpleNamespace(qty=fake.live_qty, avg_entry_price=COST, current_price=price)
    with patch("strategy.wheel_strategy._safe_journal"):
        if drop <= -0.35:
            w._try_stock_stop_loss(obj, price, COST, drop)
        else:
            # mirror run_cycle recovery branch
            if stop_state.get_attempts(SYM) > 0:
                stop_state.reset(SYM)
    placed = fake.orders[0] if fake.orders else ("(无单)", None)
    att = stop_state.get_attempts(SYM)
    print(f"  {label:28s} 价=${price:6.2f} ({drop:+.0%}) → "
          f"{placed[0]:7s}{(' @$'+format(placed[1],'.2f')) if placed[1] else '':10s} "
          f"| attempts={att}")
    return placed


def main():
    w = WheelStrategy.__new__(WheelStrategy)
    w.symbol = SYM
    fake = FakeTrading()
    w._trading = fake
    stop_state.reset(SYM)

    fails = []
    print("=" * 78)
    print("端到端崩盘时间线模拟 — MSFT 100股 @ $441, -35%线 = $286.65")
    print("=" * 78)

    print("\n【场景1】持续崩盘 → 限价×2 → 市价升级")
    t1 = tick(w, fake, 285.0, "T1 跌35%")
    t2 = tick(w, fake, 270.0, "T2 续跌")
    t3 = tick(w, fake, 255.0, "T3 续跌(应升级市价)")
    if not (t1[0] == "LIMIT" and t2[0] == "LIMIT" and t3[0] == "MARKET"):
        fails.append("场景1 升级序列")

    print("\n【场景2】反弹重置 → 再崩从限价重来")
    stop_state.reset(SYM)
    fake.open_sells.clear()
    r1 = tick(w, fake, 285.0, "T1 跌35%")
    r2 = tick(w, fake, 300.0, "T2 反弹(>-35%)")   # 反弹, 应重置
    r3 = tick(w, fake, 280.0, "T3 再崩(应回到限价)")
    if not (r1[0] == "LIMIT" and r2[0] == "(无单)" and r3[0] == "LIMIT"
            and stop_state.get_attempts(SYM) == 1):
        fails.append("场景2 反弹重置")

    print("\n【场景3】崩盘中熔断 → 等待不计数 → 恢复后继续")
    stop_state.reset(SYM)
    fake.open_sells.clear()
    h1 = tick(w, fake, 285.0, "T1 跌35%(限价)")
    fake.tradable = False
    h2 = tick(w, fake, 270.0, "T2 熔断(应等待)")
    att_during_halt = stop_state.get_attempts(SYM)
    fake.tradable = True
    h3 = tick(w, fake, 260.0, "T3 恢复(应限价 attempt2)")
    if not (h1[0] == "LIMIT" and h2[0] == "(无单)" and att_during_halt == 1
            and h3[0] == "LIMIT"):
        fails.append("场景3 熔断")

    print("\n【场景4】止损成交后仓位清零 → 状态重置")
    stop_state.reset(SYM)
    fake.open_sells.clear()
    f1 = tick(w, fake, 285.0, "T1 跌35%(限价)")
    fake.live_qty = 0   # 模拟限价成交, 持仓清零
    f2 = tick(w, fake, 270.0, "T2 已成交(qty=0)")
    if not (f1[0] == "LIMIT" and f2[0] == "(无单)" and stop_state.get_attempts(SYM) == 0):
        fails.append("场景4 成交重置")

    stop_state.reset(SYM)
    print("\n" + "=" * 78)
    if fails:
        print(f"❌ 失败场景: {fails}")
        sys.exit(1)
    print("✅ 全部 4 个崩盘时间线场景通过 — Phase 4.1 升级止损按设计工作")
    sys.exit(0)


if __name__ == "__main__":
    main()
