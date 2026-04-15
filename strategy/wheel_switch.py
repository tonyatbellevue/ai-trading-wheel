"""Wheel 标的切换管理 — 支持计划切换 + 条件触发

工作流程：
1. 调用 plan_switch("TSLA", "MSFT", trigger_date="2026-04-18") 记录计划
2. 每次 run_cycle 开头调用 maybe_switch() 检查：
   - 今天 >= trigger_date
   - 且当前 phase == IDLE（无持仓无挂单）
   - 则修改 wheel_symbol.json 将 active_symbol 改为 to_symbol，清空 plan
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

    # 执行切换
    old_symbol = state["active_symbol"]
    new_symbol = plan["to_symbol"]
    state["active_symbol"] = new_symbol
    state["history"].append({
        "switched_at": datetime.now().isoformat(),
        "from": old_symbol,
        "to": new_symbol,
        "reason": plan.get("reason", ""),
    })
    state["plan"] = None
    save_state(state)

    logger.warning(f"🔄 Wheel 标的切换: {old_symbol} → {new_symbol}（原因: {plan.get('reason', 'manual plan')}）")
    return (old_symbol, new_symbol)
