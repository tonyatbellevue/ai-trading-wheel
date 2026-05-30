"""Wheel 每轮评估器 — 每次 run_cycle 自动判断是否需要切换标的

判定规则（数据驱动，不硬编码优先标的）：
  1. STOP    → 必须切换到 wheel_scanner 评分最高的健康候选
  2. CAUTION → 若评分高于 baseline × 1.10 即切换
  3. GO + IDLE → option 周期结束时主动比较 Top1 vs baseline，
                若 Top1 高于 baseline × 1.15 也切换（保持最优配置）
  4. GO + 持仓中 → 保持当前周期，待 IDLE 时再评估

阈值（可调）：
  - SWITCH_ON_CAUTION = 1.10  (10% 即换)
  - SWITCH_ON_IDLE_GO = 1.15  (15% 即换，比 CAUTION 略高，避免 GO 时频繁换)

为避免频繁改计划：已有 plan 时不重复登记（除非升级为更紧急的原因）
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from loguru import logger

from config import settings
from strategy.wheel_switch import load_state, plan_switch

# Hoisted from per-call lazy imports — 5-min cron × 288 runs/day makes
# import machinery overhead worth shaving. These modules are safe to
# import at startup (no side effects beyond Alpaca client construction
# which already happens module-wide).
from wheel_health_check import run_health_check
from wheel_scanner import scan_wheel_alternatives


# Rotation thresholds (higher during healthy idle to avoid churn)
SWITCH_ON_CAUTION = getattr(settings, "SWITCH_ON_CAUTION", 0.10)
SWITCH_ON_IDLE_GO = getattr(settings, "SWITCH_ON_IDLE_GO", 0.15)


def evaluate_and_maybe_plan(
    current_symbol: str,
    current_phase_is_idle: bool,
    force: bool = False,
) -> dict:
    """运行一次评估。返回决策字典。

    仅在下面情况真正登记切换计划（plan_switch）：
      - current health == STOP
      - 或 force=True

    返回：
      {
        "symbol": str,
        "health_verdict": "GO"|"CAUTION"|"STOP",
        "action": "keep"|"plan_switch"|"suggest_switch",
        "new_symbol": str|None,
        "reason": str,
        "message": str   # 给 summary 用的一行说明
      }
    """
    decision = {
        "symbol": current_symbol,
        "health_verdict": "UNKNOWN",
        "action": "keep",
        "new_symbol": None,
        "reason": "",
        "message": "",
    }

    # ── Step 1: 健康检查当前标的 ──
    try:
        health = run_health_check(current_symbol)
        decision["health_verdict"] = health.get("verdict", "UNKNOWN")
        decision["health_warnings"] = health.get("warnings", [])
    except Exception as e:
        logger.warning(f"健康检查失败: {e}")
        decision["message"] = f"⚠️ 评估器：健康检查失败 {e}"
        return decision

    # ── Step 2: 若已有待执行切换计划，只更新 message 不重复登记 ──
    state = load_state()
    existing_plan = state.get("plan")

    # ── Step 3: STOP 判定 → 必须切换 ──
    if decision["health_verdict"] == "STOP":
        # 扫描最佳候选
        new_sym = _find_best_alternative(exclude=current_symbol)
        if not new_sym:
            decision["action"] = "keep"
            decision["message"] = f"🛑 {current_symbol} STOP 但无可切换候选"
            return decision

        # 若已有同目标 plan 则不重复
        if existing_plan and existing_plan.get("to_symbol") == new_sym:
            decision["action"] = "plan_exists"
            decision["new_symbol"] = new_sym
            decision["message"] = f"🛑 {current_symbol} STOP | 切换计划已存在 → {new_sym}"
            return decision

        # 登记切换：若 IDLE 立即触发日期=今天，否则周五到期后
        trigger = date.today() if current_phase_is_idle else _next_expiry_friday_plus_one()
        reason = f"health STOP: {'; '.join(decision['health_warnings'][:2])}"
        plan_switch(current_symbol, new_sym, trigger.isoformat(), reason)
        decision["action"] = "plan_switch"
        decision["new_symbol"] = new_sym
        decision["reason"] = reason
        decision["message"] = f"🛑 {current_symbol} STOP → 已登记切换至 {new_sym} @ {trigger}"
        return decision

    # ── Step 4: CAUTION → 扫描更优候选，10% 即可切换 ──
    if decision["health_verdict"] == "CAUTION" or force:
        new_sym, improvement = _find_better_alternative(current_symbol)
        if new_sym and improvement >= SWITCH_ON_CAUTION:
            trigger = _next_expiry_friday_plus_one() if not current_phase_is_idle else date.today()
            if not existing_plan or existing_plan.get("to_symbol") != new_sym:
                reason = f"CAUTION + better alternative ({new_sym} score +{improvement:.0%})"
                plan_switch(current_symbol, new_sym, trigger.isoformat(), reason)
                decision["action"] = "plan_switch"
                decision["new_symbol"] = new_sym
                decision["reason"] = reason
                decision["message"] = f"⚠️ {current_symbol} CAUTION → 登记切换至 {new_sym} (+{improvement:.0%}) @ {trigger}"
                return decision
        decision["action"] = "keep"
        decision["message"] = f"⚠️ {current_symbol} CAUTION 但无显著更优候选，保持"
        return decision

    # ── Step 5: GO + IDLE（option 周期结束）→ 主动比较 Top1 vs baseline ──
    # 持仓中（非 IDLE）时不主动换，等本周期到期再评估
    if current_phase_is_idle:
        # ★ 连亏 stop-loss：防止被某一标的永久套牢
        consecutive_losses = _count_consecutive_losses(current_symbol)
        max_consecutive = getattr(settings, "MAX_CONSECUTIVE_LOSSES", 3)
        if consecutive_losses >= max_consecutive:
            escape_sym = _find_best_alternative(exclude=current_symbol)
            if escape_sym:
                trigger = date.today()
                reason = (f"stop-loss: {consecutive_losses} consecutive losing cycles "
                          f"on {current_symbol}, escape to {escape_sym}")
                plan_switch(current_symbol, escape_sym, trigger.isoformat(), reason)
                decision["action"] = "plan_switch"
                decision["new_symbol"] = escape_sym
                decision["reason"] = reason
                decision["message"] = (
                    f"🚨 {current_symbol} 连亏 {consecutive_losses} 次 → 强制止损换到 {escape_sym}"
                )
                return decision
            # else: no healthy alternative, fall through to the "stay" path below

        # ★ 关键：只在"正常赚钱结束"时才换，被行权/亏损周期不换
        profitable, reason_why = _last_cycle_was_profitable(current_symbol)
        if not profitable:
            decision["action"] = "keep"
            decision["message"] = (
                f"⏸️ {current_symbol} IDLE 但上一周期 {reason_why} — 不切换，"
                f"先在原标的恢复盈利 (连亏 {consecutive_losses}/{max_consecutive})"
            )
            return decision

        new_sym, improvement = _find_better_alternative(current_symbol)
        if new_sym and improvement >= SWITCH_ON_IDLE_GO:
            # 必须是 GO 健康标的才切换
            try:
                target_health = run_health_check(new_sym).get("verdict", "UNKNOWN")
            except Exception:
                target_health = "UNKNOWN"

            if target_health == "GO":
                trigger = date.today()  # IDLE 立即切换
                if not existing_plan or existing_plan.get("to_symbol") != new_sym:
                    reason = (f"IDLE+profit rotation: {new_sym} scores "
                              f"+{improvement:.0%} above {current_symbol} ({reason_why})")
                    plan_switch(current_symbol, new_sym, trigger.isoformat(), reason)
                    decision["action"] = "plan_switch"
                    decision["new_symbol"] = new_sym
                    decision["reason"] = reason
                    decision["message"] = (
                        f"🔄 IDLE+赚钱结束: {current_symbol} → {new_sym} "
                        f"(+{improvement:.0%}, {reason_why}) @ {trigger}"
                    )
                    return decision
            else:
                decision["message"] = (
                    f"✅ {current_symbol} GO - Top1 {new_sym} 评分 +{improvement:.0%} "
                    f"但 {new_sym} 健康判定 {target_health}，保持"
                )
                return decision

        # ── v8 IV-blocked fallback (5/7 added) ──
        # If we got here, normal rotation didn't fire — the active stock
        # is "fine" by score but the alternative isn't decisively better
        # (< 15%). BUT: if the active itself can't pass the IV-rank gate
        # (rank < 30), the bot will be stuck IDLE forever — no open on
        # active, no rotation triggered. Real case: 5/2-5/6 TSLA was
        # active with IV rank 21-25, alternatives all +0%/-1%/-2%, bot
        # idled 5 trading days while QCOM (rank 100) sat unused.
        #
        # Fallback: if active is IV-blocked, switch to the universe's
        # highest-IV-rank healthy alternative regardless of score. This
        # bypasses the +15% threshold but ONLY when stuck — preserves
        # anti-thrashing in normal cases.
        try:
            from strategy.wheel_filters import compute_iv_rank
            active_rank = compute_iv_rank(current_symbol)
            if active_rank is not None and active_rank < 30:
                from wheel_scanner import WHEEL_UNIVERSE
                cands = []
                for s in WHEEL_UNIVERSE:
                    if s == current_symbol:
                        continue
                    r = compute_iv_rank(s)
                    if r is not None and r >= 30:
                        cands.append((s, r))
                cands.sort(key=lambda x: -x[1])  # highest IV rank first
                for cand_sym, cand_rank in cands:
                    try:
                        cand_health = run_health_check(cand_sym).get("verdict", "UNKNOWN")
                    except Exception:
                        cand_health = "UNKNOWN"
                    if cand_health == "GO":
                        trigger = date.today()
                        reason = (f"IV-blocked fallback: {current_symbol} IV rank "
                                  f"{active_rank:.0f} < 30, rotate to {cand_sym} "
                                  f"(IV rank {cand_rank:.0f})")
                        if not existing_plan or existing_plan.get("to_symbol") != cand_sym:
                            plan_switch(current_symbol, cand_sym, trigger.isoformat(), reason)
                            decision["action"] = "plan_switch"
                            decision["new_symbol"] = cand_sym
                            decision["reason"] = reason
                            decision["message"] = (
                                f"🔓 {current_symbol} IV rank {active_rank:.0f} 失格 → "
                                f"切到 {cand_sym} (IV rank {cand_rank:.0f}, 最高)"
                            )
                            return decision
                # No GO-healthy candidate found — fall through to keep
        except Exception as e:
            logger.warning(f"IV-blocked fallback check failed: {e}")

        decision["action"] = "keep"
        if new_sym:
            decision["message"] = (
                f"✅ {current_symbol} GO+profit({reason_why}) - Top1 {new_sym} 仅 +{improvement:.0%} "
                f"(< {SWITCH_ON_IDLE_GO:.0%} 阈值)，保持"
            )
        else:
            decision["message"] = f"✅ {current_symbol} GO+profit({reason_why}) - 无更优候选，保持"
        return decision

    # ── Step 6: GO + 持仓中 → 不动，等到 IDLE 再评估 ──
    decision["action"] = "keep"
    decision["message"] = f"✅ {current_symbol} GO - 持仓中，等本周期到期再评估"
    return decision


def _count_consecutive_losses(symbol: str, max_lookback: int = 20) -> int:
    """Count how many of the most-recent exit events on this symbol were
    losses, stopping at the first win. Used for stop-loss escape logic.

    "Loss" = pnl ≤ 0  OR  exit_reason == "assigned" + stock never closed
    profitably since. "Win" = any exit with pnl > 0.
    """
    try:
        from metrics.trade_journal import read_journal
        rows = read_journal()
    except Exception:
        return 0

    # Filter to this symbol's exits, newest first
    exits = [r for r in rows
             if r.get("event_type") == "exit" and r.get("symbol") == symbol]
    exits.sort(key=lambda r: r.get("event_at", ""), reverse=True)

    streak = 0
    for r in exits[:max_lookback]:
        try:
            pnl = float(r.get("pnl") or 0)
        except (ValueError, TypeError):
            pnl = 0
        if pnl > 0:
            break    # hit a win, streak resets
        streak += 1
    return streak


def _last_cycle_was_profitable(symbol: str, lookback_days: int = 14) -> tuple[bool, str]:
    """判断上一个 wheel 周期是否"正常赚钱"结束

    严格定义"正常赚钱"：
      - 期权 OTM 到期作废（赚 100% 权利金）
      - 或 Call 被行权但总账盈利（股票 + 权利金 > 入场成本）

    "非赚钱"包括：
      - Put 被行权后股票还在亏损
      - 当前权益低于近期最高（被套中）

    检查方式（多重证据，逐级 fallback）：
      1. 优先：trade_journal.csv 最近一次 exit 事件 (pnl > 0)
      2. 次选：Alpaca 最近 7 天已平仓订单的总盈亏
      3. 兜底：当前账户权益 vs lookback 最高点（无回撤即视为赚钱）

    返回：(是否赚钱, 原因说明)
    """
    # ── 证据 1: trade journal 最近一次 exit ──
    try:
        from metrics.trade_journal import read_journal
        rows = read_journal()
        # 找最近一次 exit
        exits = [r for r in rows if r.get("event_type") == "exit"
                 and r.get("symbol") == symbol]
        if exits:
            latest = exits[-1]
            pnl = float(latest.get("pnl") or 0)
            reason = latest.get("exit_reason", "")
            if reason == "expired_otm" and pnl > 0:
                return True, f"上次 expired_otm +${pnl:.0f}"
            if reason == "assigned" and pnl > 0:
                return True, f"上次 assigned 但总账盈利 +${pnl:.0f}"
            if pnl <= 0:
                return False, f"上次 {reason} 亏损 ${pnl:.0f}"
    except Exception as e:
        logger.debug(f"trade_journal 检查失败: {e}")

    # ── 证据 2: Alpaca 最近 7 天已平仓订单 ──
    try:
        from core.alpaca_client import AlpacaClients
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        trading = AlpacaClients.trading()
        closed = trading.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=50,
        ))
        sto, btc = 0.0, 0.0
        recent_count = 0
        cutoff = date.today() - timedelta(days=lookback_days)
        for o in closed:
            if not o.symbol.startswith(symbol) or len(o.symbol) < 10:
                continue
            if not o.filled_at or o.filled_at.date() < cutoff:
                continue
            if not o.filled_qty or float(o.filled_qty) <= 0:
                continue
            price = float(o.filled_avg_price or 0)
            qty = float(o.filled_qty)
            notional = price * qty * 100
            if str(o.side).endswith("SELL"):
                sto += notional
            else:
                btc += notional
            recent_count += 1

        if recent_count > 0:
            net = sto - btc
            if net > 0:
                return True, f"近 {lookback_days} 天 {recent_count} 笔净盈利 +${net:.0f}"
            else:
                return False, f"近 {lookback_days} 天 {recent_count} 笔净亏损 ${net:.0f}"
    except Exception as e:
        logger.debug(f"Alpaca 历史订单检查失败: {e}")

    # ── 证据 3: 当前权益 vs 历史峰值（兜底，无数据时视为可换） ──
    try:
        from core.alpaca_client import AlpacaClients
        acct = AlpacaClients.trading().get_account()
        eq = float(acct.equity)
        le = float(acct.last_equity)
        if eq >= le:
            return True, f"账户权益 ${eq:,.0f} ≥ 上日"
        else:
            return False, f"账户权益 ${eq:,.0f} < 上日"
    except Exception:
        pass

    # 完全无数据 → 默认允许（避免永久卡住）
    return True, "无历史数据可查（默认允许）"


def _next_expiry_friday_plus_one() -> date:
    """返回下一个周六（允许当前周期到期后再切换）"""
    today = date.today()
    # 下一个周五后的周六
    days_to_sat = (5 - today.weekday()) % 7
    if days_to_sat == 0:
        days_to_sat = 7
    return today + timedelta(days=days_to_sat + 1)


def _find_best_alternative(exclude) -> Optional[str]:
    """STOP 时找最佳替代 — 数据驱动（不再硬编码 MSFT 优先）

    流程：
      1. 调用 wheel_scanner.scan_wheel_alternatives 按评分排序所有候选
      2. 对 Top 5 逐个跑 health_check
      3. 返回第一个 GO 的标的（评分最高 + 健康）

    若 Top 5 都不 GO，再 fallback 到防御标的（QQQ/COST/UNH）作为兜底。

    `exclude` 可以是单个 str 或 set/list（v12: 用于多 wheel 排除多个已持标的）。
    """
    # v12: normalize exclude to set
    if isinstance(exclude, str):
        exclude_set = {exclude}
    else:
        exclude_set = set(exclude) if exclude else set()
    primary_exclude = next(iter(exclude_set)) if exclude_set else "TSLA"
    try:

        # 按评分扫描全部候选
        alts = scan_wheel_alternatives(exclude=primary_exclude, top_n=20)

        # 取每个候选的 symbol（按评分排序），排除整个 exclude_set
        ranked = []
        for r in (alts.get("all", []) if isinstance(alts, dict) else alts):
            sym = r.get("symbol")
            score = r.get("score", {}).get("total_score", 0)
            if sym and sym not in exclude_set:
                ranked.append((sym, score))

        # 按评分降序，逐个查健康
        for sym, score in ranked:
            try:
                h = run_health_check(sym)
                if h.get("verdict") == "GO":
                    logger.info(f"STOP→ 数据驱动选择 {sym} (评分 {score})")
                    return sym
            except Exception as e:
                logger.debug(f"health check {sym} failed: {e}")
                continue
    except Exception as e:
        logger.warning(f"_find_best_alternative scanner failed: {e}")

    # ── Fallback：扫描失败时用稳健防御标的 ──
    fallback = ["QQQ", "MSFT", "AAPL", "GOOGL", "COST", "UNH", "SPY"]
    for sym in fallback:
        if sym in exclude_set:
            continue
        try:
            h = run_health_check(sym)
            if h.get("verdict") == "GO":
                logger.info(f"STOP→ fallback 选择 {sym}")
                return sym
        except Exception:
            continue
    return None


def _find_better_alternative(current: str) -> tuple[Optional[str], float]:
    """调用 wheel_scanner 找评分显著更优的候选

    scan_wheel_alternatives 返回 dict: {baseline, better, all}
    - better: 比 current 评分高的 Top N
    - all: 全部 Top 5（万一没 better 也能对比）
    """
    try:

        alts = scan_wheel_alternatives(exclude=current, top_n=5)
        if not alts or not isinstance(alts, dict):
            return None, 0.0

        baseline_score = alts.get("baseline", {}).get("score", 0) or 0

        # 优先从 "better" 列表取 Top1；没 better 再从 "all" 取
        better_list = alts.get("better", [])
        all_list = alts.get("all", [])
        top = None
        if better_list:
            top = better_list[0]
        elif all_list:
            top = all_list[0]

        if not top:
            return None, 0.0

        top_score = top.get("score", {}).get("total_score", 0)
        top_sym = top.get("symbol")

        if baseline_score <= 0:
            return top_sym, 1.0 if top_score > 0 else 0.0

        improvement = (top_score - baseline_score) / baseline_score
        return top_sym, improvement
    except Exception as e:
        logger.warning(f"更优候选扫描失败: {e}")
        return None, 0.0
