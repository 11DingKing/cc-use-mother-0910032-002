"""可注入的时钟。

生产环境使用 UTC 系统时钟；测试和管理命令重放时使用固定时钟，
保证事件时间戳与去重结果可复现。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        self._moment = moment

    def now(self) -> datetime:
        return self._moment
