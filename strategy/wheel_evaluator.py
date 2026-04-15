"""Wheel 每轮评估器 — 每次 run_cycle 自动判断是否需要切换标的

判定规则：
  1. 若当前标的 health_check == STOP → 必须切换到健康候选
  2. 若 health_check == CAUTION 连续 3 次 → 建议切换
  3. 若扫描到明显更优候选（评分 ≥ baseline × 1.30）→ 建议切换
  4. 若 health_check == GO 且无更优候选 → 保持

为避免频繁改计划：已有 plan 时不重复登记（除非升级为更紧急的原因）
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from loguru import logger

from config import settings
from strategy.wheel_switch import load_state, plan_switch


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
        from wheel_health_check import run_health_check
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

    # ── Step 4: CAUTION → 扫描更优候选，若显著更优则登记 ──
    if decision["health_verdict"] == "CAUTION" or force:
        new_sym, improvement = _find_better_alternative(current_symbol)
        if new_sym and improvement >= 0.30:
            trigger = _next_expiry_friday_plus_one()
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

    # ── Step 5: GO → 保持 ──
    decision["action"] = "keep"
    decision["message"] = f"✅ {current_symbol} GO - 保持 wheel"
    return decision


def _next_expiry_friday_plus_one() -> date:
    """返回下一个周六（允许当前周期到期后再切换）"""
    today = date.today()
    # 下一个周五后的周六
    days_to_sat = (5 - today.weekday()) % 7
    if days_to_sat == 0:
        days_to_sat = 7
    return today + timedelta(days=days_to_sat + 1)


def _find_best_alternative(exclude: str) -> Optional[str]:
    """health STOP 时找最健康的替代品（优先 MSFT/SPY 稳定股）"""
    priority = ["MSFT", "SPY", "AAPL", "NVDA", "TSLA"]
    for sym in priority:
        if sym == exclude:
            continue
        try:
            from wheel_health_check import run_health_check
            h = run_health_check(sym)
            if h.get("verdict") == "GO":
                return sym
        except Exception:
            continue
    return None


def _find_better_alternative(current: str) -> tuple[Optional[str], float]:
    """调用 wheel_scanner 找评分显著更优的候选"""
    try:
        from wheel_scanner import scan_candidates, scan_wheel_alternatives
        from core.alpaca_client import AlpacaClients
        cash = float(AlpacaClients.trading().get_account().cash)

        # 当前标的评分
        baseline_list = scan_candidates([current], dte_min=1, dte_max=5, cash=cash)
        baseline_score = (baseline_list[0]["score"]["total_score"]
                          if baseline_list else 0)

        alts = scan_wheel_alternatives(exclude=current, top_n=3)
        if not alts:
            return None, 0.0

        top = alts[0]
        top_score = top["score"]["total_score"]
        if baseline_score <= 0:
            return top["symbol"], 1.0
        improvement = (top_score - baseline_score) / baseline_score
        return top["symbol"], improvement
    except Exception as e:
        logger.warning(f"更优候选扫描失败: {e}")
        return None, 0.0
