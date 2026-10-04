"""服务记录防重复归档服务端。

只增事件账本 + 确定性投影：
来源指纹、身份线索、场次证据、冲突候选与人工裁决全部可追溯。
"""
from .auth import Principal, DECIDERS, SESSION_CANCELLERS
from .clock import Clock, FixedClock, SystemClock
from .errors import Conflict, DomainError, InvalidState, NotFound, PermissionDenied
from .events import Event
from .projection import Projection
from .service import ALGORITHM_VERSION, ServiceArchive
from .store import EventStore

__all__ = [
    "ALGORITHM_VERSION",
    "Clock",
    "Conflict",
    "DECIDERS",
    "DomainError",
    "Event",
    "EventStore",
    "FixedClock",
    "InvalidState",
    "NotFound",
    "PermissionDenied",
    "Principal",
    "Projection",
    "SESSION_CANCELLERS",
    "ServiceArchive",
    "SystemClock",
]
