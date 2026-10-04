"""来源指纹、身份线索与场次证据。

同一条服务记录无论来自哪所学校、重传多少次，都要落到同一组稳定标识上：

- 来源定位：``(source, batch_id, entry_no)`` —— 记录在来源方名册中的位置；
- 来源指纹：原始报文内容的 SHA-256 —— 内容不变指纹不变；
- 身份线索：姓名、证件尾号、手机尾号等 —— 用于识"同一个人"；
- 场次证据：场馆、日期、场次码、起止时间 —— 用于识"同一场服务"。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

from .hashing import canonical_json, digest, short_id

FIELD_ALIASES: dict[str, set[str]] = {
    "volunteer_name": {"volunteer_name", "name", "姓名"},
    "id_number": {"id_number", "id_card", "证件号", "身份证号"},
    "id_tail": {"id_tail", "证件尾号"},
    "phone": {"phone", "mobile", "手机", "手机号"},
    "school": {"school", "学校"},
    "grade": {"grade", "年级"},
    "class_name": {"class_name", "class", "班级"},
    "site": {"site", "venue", "场馆", "服务地点"},
    "service_date": {"service_date", "date", "服务日期", "日期"},
    "session_code": {"session_code", "session", "场次", "场次码"},
    "start": {"start", "start_time", "开始时间", "签到时间"},
    "end": {"end", "end_time", "结束时间", "签退时间"},
    "role": {"role", "岗位", "角色"},
}


def _pick(raw: dict[str, Any], key: str) -> Any:
    for alias in FIELD_ALIASES[key]:
        if alias in raw and raw[alias] not in (None, ""):
            return raw[alias]
    return None


def norm_text(value: Any) -> str:
    """NFKC 规范化并去除所有空白，兼容全角字符与多余空格。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", "", text)


def digits(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value))


def tail4(value: Any) -> str:
    number = digits(value)
    return number[-4:] if len(number) >= 4 else number


@dataclass(frozen=True)
class IdentityClues:
    name: str
    id_tail: str
    phone_tail: str
    school: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "id_tail": self.id_tail,
            "phone_tail": self.phone_tail,
            "school": self.school,
        }


@dataclass(frozen=True)
class SessionEvidence:
    site: str
    service_date: str
    session_code: str
    start_min: int | None
    end_min: int | None

    @property
    def bucket(self) -> tuple[str, str, str]:
        """同场馆同日期同场次码的记录落在同一桶内比对。"""
        return (self.site, self.service_date, self.session_code)

    @property
    def day_bucket(self) -> tuple[str, str]:
        return (self.site, self.service_date)

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "service_date": self.service_date,
            "session_code": self.session_code,
            "start_min": self.start_min,
            "end_min": self.end_min,
        }


def _to_minutes(value: Any) -> int | None:
    text = norm_text(value)
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def extract_clues(raw: dict[str, Any]) -> IdentityClues:
    explicit_tail = norm_text(_pick(raw, "id_tail"))
    return IdentityClues(
        name=norm_text(_pick(raw, "volunteer_name")),
        id_tail=(tail4(_pick(raw, "id_number")) or explicit_tail),
        phone_tail=tail4(_pick(raw, "phone")),
        school=norm_text(_pick(raw, "school")),
    )


def extract_session(raw: dict[str, Any]) -> SessionEvidence:
    day = norm_text(_pick(raw, "service_date"))
    if day:
        # 校验日期合法，非法日期保持原样会让桶退化为字符串本身。
        try:
            date.fromisoformat(day)
        except ValueError:
            pass
    start = _to_minutes(_pick(raw, "start"))
    end = _to_minutes(_pick(raw, "end"))
    if start is not None and end is not None and end < start:
        # 跨午夜服务按到次日处理，保证时间重叠判断可用。
        end += 24 * 60
    return SessionEvidence(
        site=norm_text(_pick(raw, "site")),
        service_date=day,
        session_code=norm_text(_pick(raw, "session_code")).upper(),
        start_min=start,
        end_min=end,
    )


def batch_key(source: str, batch_id: str) -> str:
    return f"{norm_text(source)}|{norm_text(batch_id)}"


def batch_stream(source: str, batch_id: str) -> str:
    return short_id("bat", batch_key(source, batch_id))


def record_id(source: str, batch_id: str, entry_no: Any) -> str:
    """来源定位的稳定标识：同一所学校同一册同一条号永远映射到同一 ID。"""
    return short_id("rid", batch_key(source, batch_id), str(entry_no))


def source_fingerprint(raw: dict[str, Any]) -> str:
    """原始报文内容指纹：字节级一致的重传得到完全相同的指纹。"""
    return "fp_" + digest(canonical_json(raw))


def time_overlap(a: SessionEvidence, b: SessionEvidence) -> bool:
    if a.day_bucket != b.day_bucket:
        return False
    if a.start_min is None or a.end_min is None:
        return False
    if b.start_min is None or b.end_min is None:
        return False
    return a.start_min < b.end_min and b.start_min < a.end_min


def same_session(a: SessionEvidence, b: SessionEvidence) -> tuple[bool, str]:
    """返回（是否同一场服务，证据原因）。"""
    if (
        a.session_code
        and b.session_code
        and a.site == b.site
        and a.service_date == b.service_date
        and a.session_code.upper() == b.session_code.upper()
    ):
        return True, "同场次编码"
    if a.day_bucket == b.day_bucket and time_overlap(a, b):
        return True, "服务时间重叠"
    return False, ""


@dataclass(frozen=True)
class PairMatch:
    reasons: tuple[str, ...]
    score: int


# 身份证据权重：证件尾号 > 手机尾号 > 姓名
_IDENTITY_WEIGHTS = (
    ("id_tail", "证件尾号一致", 3),
    ("phone_tail", "手机号尾号一致", 2),
    ("name", "姓名一致", 1),
)


def compare_identity(a: IdentityClues, b: IdentityClues) -> PairMatch | None:
    reasons: list[str] = []
    score = 0
    for field, reason, weight in _IDENTITY_WEIGHTS:
        va = getattr(a, field)
        vb = getattr(b, field)
        if va and vb and va == vb:
            reasons.append(reason)
            score += weight
    if not reasons:
        return None
    return PairMatch(tuple(reasons), score)


def confidence_label(score: int, strong_session: bool) -> str:
    if score >= 3 and strong_session:
        return "高"
    if score >= 1 and strong_session:
        return "中"
    return "低"


def service_minutes(session: SessionEvidence) -> float:
    if session.start_min is None or session.end_min is None:
        return 0.0
    return float(max(0, session.end_min - session.start_min))
