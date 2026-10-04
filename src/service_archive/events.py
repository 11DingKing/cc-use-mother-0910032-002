"""领域事件定义。

系统采用只增（append-only）事件账本：来源提交、合并建议、人工确认、
拆分、迟到签到、场次取消、归档与更正全部是事件。任何记录都不会被物理
删除，因此来源谱系始终可追溯。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 学校（或其他来源方）开启一个补录批次
BATCH_OPENED = "batch_opened"
# 同一 batch_id 再次提交，记录重传事实（即使内容完全一致）
BATCH_RETRANSMITTED = "batch_retransmitted"
# 单条服务记录原始提交
RECORD_SUBMITTED = "record_submitted"
# 同一来源定位键出现了新内容，旧版本被新版本替代（旧版仍保留）
RECORD_SUPERSEDED = "record_superseded"
# 管理命令完成一次去重重跑（审计标记，不改变既有结论）
DEDUPE_RUN = "dedupe_run"
# 自动生成合并建议
SUGGESTION_RAISED = "suggestion_raised"
# 建议失效（成员被修订、场次取消等）
SUGGESTION_OBSOLETED = "suggestion_obsoleted"
# 授权人员确认合并
MERGE_CONFIRMED = "merge_confirmed"
# 授权人员拒绝合并
MERGE_REJECTED = "merge_rejected"
# 误合并拆分（归档前）
GROUP_SPLIT = "group_split"
# 签到（含迟到签到）
CHECKIN_RECORDED = "checkin_recorded"
# 场次取消
SESSION_CANCELLED = "session_cancelled"
# 正式归档
GROUP_ARCHIVED = "group_archived"
# 归档后的唯一调整通道：更正事件
CORRECTION_APPLIED = "correction_applied"

ALL_EVENT_TYPES = frozenset(
    {
        BATCH_OPENED,
        BATCH_RETRANSMITTED,
        RECORD_SUBMITTED,
        RECORD_SUPERSEDED,
        DEDUPE_RUN,
        SUGGESTION_RAISED,
        SUGGESTION_OBSOLETED,
        MERGE_CONFIRMED,
        MERGE_REJECTED,
        GROUP_SPLIT,
        CHECKIN_RECORDED,
        SESSION_CANCELLED,
        GROUP_ARCHIVED,
        CORRECTION_APPLIED,
    }
)


@dataclass(frozen=True)
class Event:
    seq: int
    etype: str
    payload: dict[str, Any]
    created_at: str
    stream: str = ""
    operator: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.etype,
            "stream": self.stream,
            "operator": self.operator,
            "created_at": self.created_at,
            "payload": self.payload,
        }
