"""TSLA Wheel 策略 — 单次执行（供 GitHub Actions 调用）

每次运行：检查市场状态 → 检查 Wheel 阶段 → 执行一次 run_cycle() → 退出
由 GitHub Actions 每 5 分钟调用一次，无需本机保持开启。

NOTE: Earlier versions piggybacked email_summary.py here for OPEN/CLOSE
window backup-trigger reliability. That was reverted on 5/1 — daily_sent_state.json
is per-runner local, so wheel.yml's 12 invocations per window each saw an
empty state and would fan out into 12 duplicate emails. wheel-summary.yml
is now the sole email trigger; if its cron drops, user manually fires
workflow_dispatch.

输出变量：
- 如果下单，会写 trade.flag 文件 + 写 summary.md 供工作流发 Issue
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import WheelStrategy, WheelPhase
from execution.option_order_manager import OptionOrderManager
from config import settings
from loguru import logger

ET = ZoneInfo("America/New_York")


def main():
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    logger.info(f"=== Wheel 单次检查 @ {now_et} ===")

    # 检查市场是否开盘
    try:
        clock = AlpacaClients.trading().get_clock()
    except Exception as e:
        logger.error(f"无法获取市场状态: {e}")
        sys.exit(1)

    if not clock.is_open:
        next_open = clock.next_open.astimezone(ET).strftime("%m/%d %H:%M ET")
        logger.info(f"市场休市，跳过本次。下次开盘: {next_open}")
        sys.exit(0)

    logger.info("市场开盘，执行 Wheel 策略检查...")

    # 记录执行前的订单数，用于检测是否新下单
    # v12 修 (5/30): 必须查 ALL 标的的订单，不只是 settings.WHEEL_SYMBOL，
    # 因为并行多 wheel 会在 GM/MSFT 等非 primary 标的上开仓。
    # 旧代码只查 AVGO 订单 → 即使开了 GM 仓位 new_orders 也是空集 → 无 trade alert。
    opt_mgr = OptionOrderManager()
    def _all_open_option_orders():
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        try:
            orders = AlpacaClients.trading().get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            )
            return [o for o in orders if o.symbol and len(o.symbol) > 10]
        except Exception as e:
            logger.warning(f"查全账户 open orders 失败: {e}")
            return []
    before_orders = {o.id for o in _all_open_option_orders()}

    # ── v12 CRITICAL FIX (6/4): 必须管理所有已持仓 wheel 标的 ──
    # 之前版本只跑 primary + NEW candidate，导致 secondary 持仓（如 MSFT）
    # 成为"孤儿仓位" — 没有任何风险管理代码触发。
    # MSFT 6/3 损失 -$1,418 因为 3× stop loss 从未运行。
    primary_sym = settings.WHEEL_SYMBOL
    from strategy.wheel_strategy import _parse_symbol

    # 1. 收集所有已有持仓 wheel 标的（必须管理）
    existing_opt_symbols = set()
    try:
        for p in AlpacaClients.trading().get_all_positions():
            if p.symbol and len(p.symbol) > 10:  # OCC option
                info = _parse_symbol(p.symbol)
                if info:
                    existing_opt_symbols.add(info["underlying"])
            elif p.symbol and float(p.qty or 0) >= 100:
                # 被行权后的股票仓位 — 也要管 CC
                existing_opt_symbols.add(p.symbol)
    except Exception as e:
        logger.warning(f"收集已有持仓失败: {e}")

    # 2. wheel_symbols = primary + 全部已持仓 + (有空位时) new candidate
    wheel_symbols = [primary_sym]
    for s in sorted(existing_opt_symbols):
        if s != primary_sym and s not in wheel_symbols:
            wheel_symbols.append(s)
            logger.info(f"v12: 管理已有持仓 wheel → {s}")

    max_concurrent = getattr(settings, "MAX_CONCURRENT_WHEELS", 1)
    if max_concurrent >= 2 and len(wheel_symbols) < max_concurrent:
        try:
            from strategy.wheel_evaluator import _find_best_alternative
            secondary_sym = _find_best_alternative(exclude=existing_opt_symbols | {primary_sym})
            if secondary_sym and secondary_sym not in wheel_symbols:
                wheel_symbols.append(secondary_sym)
                logger.info(f"v12: 添加新 wheel 候选 → {secondary_sym}")
            else:
                logger.info(f"v12: 未找到合适新 wheel 候选")
        except Exception as e:
            logger.warning(f"v12 新 wheel 选择失败: {e}")

    for sym in wheel_symbols:
        try:
            logger.info(f"--- 运行 wheel: {sym} ---")
            strategy = WheelStrategy(symbol=sym)
            strategy.run_cycle()
            logger.info(f"--- {sym} run_cycle() 完成 ---")
        except Exception as e:
            logger.error(f"{sym} run_cycle() 异常: {e}")
            # 不 sys.exit — 让其余 wheel 继续

    # 检测是否有新下单（v12 修：检查全账户而非单标的）
    after_orders_list = _all_open_option_orders()
    after_orders = {o.id for o in after_orders_list}
    new_orders = after_orders - before_orders

    if new_orders:
        logger.info(f"检测到 {len(new_orders)} 个新下单 → 生成交易通知")

        # 1. Send a SIMPLE alert email immediately. Idempotency is implicit:
        #    set-difference (after - before) only fires on the SAME run that
        #    placed the order. Subsequent cycles see the order in `before`
        #    and `after`, so new_orders is empty for them. No cross-run
        #    state needed.
        try:
            from utils.emailer import send_email
            sgt = ZoneInfo("Asia/Singapore")
            now_et = datetime.now(ET).strftime("%H:%M ET")
            now_sgt = datetime.now(sgt).strftime("%H:%M SGT")

            # Pull the actual order objects to show details (side, qty, limit).
            # v12 修: 同样需要全账户查询，不限 primary 标的。
            order_lookup = {o.id: o for o in after_orders_list}
            order_lines = []
            for oid in new_orders:
                o = order_lookup.get(oid)
                if not o:
                    order_lines.append(f"  - order id {oid} (details unavailable)")
                    continue
                side = str(o.side).split(".")[-1]
                limit = float(o.limit_price) if o.limit_price else 0
                order_lines.append(
                    f"  • {side} {o.symbol} qty={o.qty} @ limit ${limit:.2f}"
                )

            subject = (f"[{settings.WHEEL_SYMBOL} Wheel] Trade alert "
                       f"— {now_et} (= {now_sgt})")
            body = (
                f"Bot placed {len(new_orders)} new option order(s):\n\n"
                + "\n".join(order_lines)
                + "\n\nFull summary will follow at next OPEN/CLOSE email."
            )
            ok = send_email(subject=subject, body_text=body)
            if ok:
                logger.info(f"📧 trade alert email sent: {subject}")
            else:
                logger.warning("trade alert email send returned False")
        except Exception as e:
            logger.warning(f"trade alert email failed: {e}")

        # 2. Existing path: write trade.flag + summary.md for the GitHub
        #    Actions workflow to create an Issue.
        try:
            from wheel_summary import build_summary
            md = build_summary("trade")
            with open("summary.md", "w", encoding="utf-8") as f:
                f.write(md)
            # 写标记文件
            with open("trade.flag", "w") as f:
                f.write("1")
            # 设置 GitHub Actions 输出
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a") as f:
                    f.write("traded=true\n")
        except Exception as e:
            logger.error(f"生成通知失败: {e}")


if __name__ == "__main__":
    main()
