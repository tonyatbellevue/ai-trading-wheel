"""内部事件总线 — 解耦数据、策略、执行各层"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List
from datetime import datetime
import pandas as pd


class EventType(Enum):
    DATA      = auto()   # 新 K 线到达
    INDICATOR = auto()   # 指标计算完成
    SIGNAL    = auto()   # 策略信号产生
    ORDER     = auto()   # 准备下单
    FILL      = auto()   # 订单成交确认


class SignalDirection(Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class DataEvent:
    type   : EventType = EventType.DATA
    symbol : str = ""
    bar    : pd.Series = field(default_factory=pd.Series)
    ts     : datetime = field(default_factory=datetime.utcnow)


@dataclass
class SignalEvent:
    type       : EventType = EventType.SIGNAL
    symbol     : str = ""
    direction  : SignalDirection = SignalDirection.HOLD
    confidence : float = 0.0
    price      : float = 0.0
    ts         : datetime = field(default_factory=datetime.utcnow)


@dataclass
class FillEvent:
    type      : EventType = EventType.FILL
    symbol    : str = ""
    side      : str = ""
    qty       : float = 0.0
    price     : float = 0.0
    order_id  : str = ""
    ts        : datetime = field(default_factory=datetime.utcnow)


class EventBus:
    """简单同步发布-订阅事件总线"""

    def __init__(self):
        self._handlers: Dict[EventType, List[Callable]] = {}

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event) -> None:
        for handler in self._handlers.get(event.type, []):
            handler(event)


# 全局单例
bus = EventBus()
