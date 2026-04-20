"""Wheel 标的切换管理 — 支持计划切换 + 条件触发

工作流程：
1. 调用 plan_switch("TSLA", "MSFT", trigger_date="2026-04-18") 记录计划
2. 每次 run_cycle 开头调用 maybe_switch() 检查：
   - 今天 >= trigger_date
   - 且当前 phase == IDLE（无持仓无挂单）
   - **触发时重扫 scan_wheel_alternatives**：若当天 top-1 ≠ plan.to_symbol
     则采用当天 top-1（避免用过期排名）；扫描失败则回退原计划
   - 修改 wheel_symbol.json 将 active_symbol 改为最终选出的 symbol，清空 plan
3. config.settings.WHEEL_SYMBOL 从 wheel_symbol.json 动态读取

文件位置：仓库根 wheel_symbol.json（持久化进 git 仓库，GitHub Actions 跨运行可见）
"""
from __future__ import annotations
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from loguru import logger


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "wheel_symbol.json"


def _default_state() -> dict:
    return {
        "active_symbol": "TSLA",
        "plan": None,   # 或 {"to_symbol": "MSFT", "trigger_date": "2026-04-18", "reason": "..."}
        "history": [],
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return _default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取 {STATE_FILE} 失败: {e}，使用默认")
        return _default_state()


def save_state(state: dict) -> None:
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_active_symbol() -> str:
    return load_state().get("active_symbol", "TSLA")


def plan_switch(from_symbol: str, to_symbol: str,
                trigger_date: str, reason: str = "") -> None:
    """登记一个切换计划。trigger_date: ISO 日期字符串 YYYY-MM-DD"""
    state = load_state()
    state["plan"] = {
        "from_symbol": from_symbol,
        "to_symbol": to_symbol,
        "trigger_date": trigger_date,
        "reason": reason,
        "planned_at": datetime.now().isoformat(),
    }
    save_state(state)
    logger.info(f"📅 切换计划已登记: {from_symbol} → {to_symbol} @ ≥{trigger_date}")


def cancel_plan() -> None:
    state = load_state()
    state["plan"] = None
    save_state(state)
    logger.info("切换计划已取消")


def maybe_switch(current_phase_is_idle: bool) -> Optional[tuple[str, str]]:
    """检查是否应触发切换。

    触发条件：
      1. 存在 plan
      2. 今天 >= plan.trigger_date
      3. 当前 phase == IDLE（无持仓无挂单）

    返回 (old_symbol, new_symbol) 表示已切换；否则 None。
    """
    state = load_state()
    plan = state.get("plan")
    if not plan:
        return None

    trigger = date.fromisoformat(plan["trigger_date"])
    today = date.today()
    if today < trigger:
        logger.info(f"切换计划待触发: {plan['to_symbol']} @ {trigger}（还需 {(trigger-today).days} 天）")
        return None

    if not current_phase_is_idle:
        logger.info(f"切换计划待触发: 当前仍有持仓/挂单，等待 IDLE 状态")
        return None

    old_symbol = state["active_symbol"]
    planned_symbol = plan["to_symbol"]

    # 触发时重新扫描：用当天 top-1，不盲信几天前的 plan。
    # 市场数据在 plan 登记到触发之间可能已变，排名会变。
    new_symbol = planned_symbol
    rescan_note = ""
    try:
        from wheel_scanner import scan_wheel_alternatives
        alts = scan_wheel_alternatives(exclude=old_symbol, top_n=5)
        candidates = (alts or {}).get("all", []) if isinstance(alts, dict) else []
        if candidates:
            top = candidates[0]
            top_sym = top.get("symbol")
            top_score = top.get("score", {}).get("total_score", 0)
            if top_sym and top_sym != planned_symbol:
                rescan_note = (
                    f"触发时重扫: 原计划 {planned_symbol} 已非 top-1，"
                    f"改用 {top_sym} (score={top_score:.1f})"
                )
                logger.warning(f"🔁 {rescan_note}")
                new_symbol = top_sym
            else:
                logger.info(
                    f"触发时重扫: {planned_symbol} 仍为 top-1 (score={top_score:.1f})，按原计划切换"
                )
        else:
            logger.warning("触发时重扫无候选，回退到原计划标的")
    except Exception as e:
        logger.warning(f"触发时重扫失败，回退原计划 {planned_symbol}: {e}")

    state["active_symbol"] = new_symbol
    history_entry = {
        "switched_at": datetime.now().isoformat(),
        "from": old_symbol,
        "to": new_symbol,
        "planned_to": planned_symbol,
        "reason": plan.get("reason", ""),
    }
    if rescan_note:
        history_entry["rescan_note"] = rescan_note
    state["history"].append(history_entry)
    state["plan"] = None
    save_state(state)

    logger.warning(
        f"🔄 Wheel 标的切换: {old_symbol} → {new_symbol}"
        f"（计划 {planned_symbol}{'，重扫后改选' if new_symbol != planned_symbol else ''}）"
    )
    return (old_symbol, new_symbol)
