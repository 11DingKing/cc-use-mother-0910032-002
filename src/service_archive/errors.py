"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则冲突的基类。"""


class PermissionDenied(DomainError):
    """非授权人员尝试执行需要确认的操作。"""


class InvalidState(DomainError):
    """记录当前状态不允许该操作（如归档后直接修改）。"""


class NotFound(DomainError):
    """目标建议、分组或记录不存在。"""


class Conflict(DomainError):
    """请求与已有人工决定冲突。"""
