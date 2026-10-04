"""状态、事件类型与纯数据载体。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 记录生命周期状态
ST_RECEIVED = "received"        # 已收件，证据已登记
ST_PENDING = "pending"          # 存在待处理冲突候选
ST_CONFIRMED = "confirmed"      # 身份/合并已确认，等待归档
ST_ARCHIVED = "archived"        # 已归档，只读，仅可经更正事件调整
ST_CANCELLED = "cancelled"      # 场次取消，记录失效（谱系保留）

# 合并候选状态
CAND_PROPOSED = "proposed"      # 系统建议
CAND_CONFIRMED = "confirmed"    # 授权人确认合并
CAND_REJECTED = "rejected"      # 授权人拒绝（视为不同记录）
CAND_SUPERSEDED = "superseded"  # 因后续拆分而作废

# 合并结果角色
ROLE_SURVIVOR = "survivor"      # 主记录，保留计时
ROLE_DUPLICATE = "duplicate"    # 重复记录，不单独计时

# 事件类型（追加式事件流，永不更新、永不删除）
EV_RECORD_RECEIVED = "record.received"
EV_BATCH_RETRANSMIT = "batch.retransmit"
EV_LATE_CHECKIN = "record.late_checkin"
EV_SESSION_CANCELLED = "session.cancelled"
EV_CANDIDATE_PROPOSED = "candidate.proposed"
EV_CANDIDATE_CONFIRMED = "candidate.confirmed"
EV_CANDIDATE_REJECTED = "candidate.rejected"
EV_MERGE_SPLIT = "merge.split"
EV_RECORD_VERIFIED = "record.verified"
EV_RECORD_ARCHIVED = "record.archived"
EV_RECORD_CORRECTED = "record.corrected"

EVENT_TYPES = frozenset(
    {
        EV_RECORD_RECEIVED,
        EV_BATCH_RETRANSMIT,
        EV_LATE_CHECKIN,
        EV_RECORD_VERIFIED,
        EV_SESSION_CANCELLED,
        EV_CANDIDATE_PROPOSED,
        EV_CANDIDATE_CONFIRMED,
        EV_CANDIDATE_REJECTED,
        EV_MERGE_SPLIT,
        EV_RECORD_ARCHIVED,
        EV_RECORD_CORRECTED,
    }
)


@dataclass(frozen=True)
class Submission:
    """一次提交的输入（学校集中补录或系统重传）。"""

    school_code: str                 # 提交学校（来源主体）
    submitter: str                   # 提交人（学校经办人账号）
    volunteer_name: str              # 姓名线索（可能不一致）
    id_tail: str                     # 证件尾号线索（可能不一致）
    session_code: str                # 场次编号
    session_name: str                # 场次名称
    service_start: str               # 服务开始时间 ISO8601
    service_end: str                 # 服务结束时间 ISO8601
    minutes: int                     # 申报时长（分钟）
    batch_no: str                    # 批次号（重传时保持一致）
    checkin_at: str | None = None    # 签到时间（迟到时与开始时间不同）
    payload: dict[str, Any] = field(default_factory=dict)  # 原始材料
    transmitted_at: str | None = None  # 重传时间戳，可空


@dataclass(frozen=True)
class ClueScore:
    record_id: str
    name: str
    id_tail: str
    session_code: str
    name_score: float
    id_score: float
    session_score: float
    total: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Proposal:
    """系统为一批疑似重复记录生成的合并建议。"""

    survivor_id: str
    duplicates: tuple[str, ...]
    scores: tuple[ClueScore, ...]
    reasons: tuple[str, ...]
