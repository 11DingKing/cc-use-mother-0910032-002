"""角色授权。

与领域契约中的角色一致：合并/拆分/更正等人工裁决只允许文博中心运营员，
场次取消允许场馆负责人或运营员。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import PermissionDenied

OPERATOR = "文博中心运营员"
VENUE_MANAGER = "场馆负责人"
VOLUNTEER = "志愿者"
GUARDIAN = "监护人"

# 可做出合并/拒绝/拆分/更正裁决的角色
DECIDERS = frozenset({OPERATOR})
# 可取消场次的角色
SESSION_CANCELLERS = frozenset({OPERATOR, VENUE_MANAGER})


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


def require_decider(principal: Principal) -> None:
    if principal.role not in DECIDERS:
        raise PermissionDenied(f"角色 {principal.role} 无权进行人工裁决，需要 {OPERATOR}")


def require_session_canceller(principal: Principal) -> None:
    if principal.role not in SESSION_CANCELLERS:
        raise PermissionDenied(f"角色 {principal.role} 无权取消场次")
