"""事件投影：从只增账本派生当前状态。

投影完全由事件序列决定：删掉库重放全部事件，得到的状态逐字节一致，
因此管理命令重跑、数据库重建都不会改变结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import (
    BATCH_OPENED,
    BATCH_RETRANSMITTED,
    CHECKIN_RECORDED,
    CORRECTION_APPLIED,
    DEDUPE_RUN,
    GROUP_ARCHIVED,
    GROUP_SPLIT,
    MERGE_CONFIRMED,
    MERGE_REJECTED,
    RECORD_SUBMITTED,
    RECORD_SUPERSEDED,
    SESSION_CANCELLED,
    SUGGESTION_OBSOLETED,
    SUGGESTION_RAISED,
)
from .hashing import short_id
from . import events as event_types

STATE_DRAFT = "草拟"
STATE_PENDING = "待核验"
STATE_CONFIRMED = "已确认"
STATE_RUNNING = "执行中"
STATE_ARCHIVED = "已归档"
STATE_CANCELLED = "已取消"


@dataclass
class RecordVersion:
    fingerprint: str
    raw: dict[str, Any]
    clues: dict[str, Any]
    session: dict[str, Any]
    seq: int
    superseded_by: str | None = None


@dataclass
class RecordState:
    rid: str
    source: str
    batch_id: str
    entry_no: Any
    versions: list[RecordVersion] = field(default_factory=list)

    @property
    def current(self) -> RecordVersion:
        return self.versions[-1]

    @property
    def fingerprints(self) -> list[str]:
        return [v.fingerprint for v in self.versions]


@dataclass
class Transmission:
    seq: int
    kind: str
    content_hash: str
    record_count: int
    submitted_at: str = ""


@dataclass
class BatchState:
    source: str
    batch_id: str
    transmissions: list[Transmission] = field(default_factory=list)

    @property
    def stream(self) -> str:
        from .fingerprints import batch_stream

        return batch_stream(self.source, self.batch_id)

    @property
    def current_content_hash(self) -> str | None:
        return self.transmissions[-1].content_hash if self.transmissions else None


@dataclass
class SuggestionInstance:
    suggestion_id: str
    generation: int
    rids: tuple[str, ...]
    edges: list[dict[str, Any]]
    score: int
    confidence: str
    fingerprints: tuple[str, ...]
    status: str = "open"  # open / confirmed / rejected / obsoleted
    group_id: str | None = None
    decided_by: str | None = None
    decided_seq: int | None = None

    @property
    def key(self) -> str:
        return f"{self.suggestion_id}#g{self.generation}"


@dataclass
class GroupState:
    group_id: str
    members: list[str]
    origin: str  # singleton / merge / split
    created_seq: int
    parent_id: str | None = None
    suggestion_id: str | None = None
    suggestion_generation: int | None = None
    archived_seq: int | None = None
    excluded: set[str] = field(default_factory=set)
    attached: list[str] = field(default_factory=list)
    minutes_delta: float = 0.0
    corrections: list[dict[str, Any]] = field(default_factory=list)
    split_children: list[str] = field(default_factory=list)

    @property
    def archived(self) -> bool:
        return self.archived_seq is not None

    def counting_members(self) -> list[str]:
        members = [rid for rid in self.members if rid not in self.excluded]
        members.extend(self.attached)
        return members
class Projection:
    def __init__(self) -> None:
        self.batches: dict[str, BatchState] = {}
        self.records: dict[str, RecordState] = {}
        self.groups: dict[str, GroupState] = {}
        # 建议自然 ID -> 各代实例（按 generation 排序）
        self.suggestions: dict[str, list[SuggestionInstance]] = {}
        self.checkins: dict[str, list[dict[str, Any]]] = {}
        self.cancelled_sessions: set[tuple[str, str, str]] = set()
        self.dedupe_runs: list[dict[str, Any]] = []
        # rid -> 当前所在组
        self._membership: dict[str, str] = {}
        # 人工拆分切开的成对记录 -> 拆分时各自的指纹快照。
        # 证据未再次变化前，去重不得重新撮合这些对子。
        self.split_severances: dict[frozenset[str], dict[str, str]] = {}

    # ---- 加载 ----

    def load(self, event_log: list[event_types.Event]) -> None:
        for event in event_log:
            self.apply(event)

    def apply(self, event: event_types.Event) -> None:
        p = event.payload
        handler = {
            BATCH_OPENED: self._on_batch_opened,
            BATCH_RETRANSMITTED: self._on_batch_retransmitted,
            RECORD_SUBMITTED: self._on_record_submitted,
            RECORD_SUPERSEDED: self._on_record_superseded,
            DEDUPE_RUN: self._on_dedupe_run,
            SUGGESTION_RAISED: self._on_suggestion_raised,
            SUGGESTION_OBSOLETED: self._on_suggestion_obsoleted,
            MERGE_CONFIRMED: self._on_merge_confirmed,
            MERGE_REJECTED: self._on_merge_rejected,
            GROUP_SPLIT: self._on_group_split,
            CHECKIN_RECORDED: self._on_checkin,
            SESSION_CANCELLED: self._on_session_cancelled,
            GROUP_ARCHIVED: self._on_archived,
            CORRECTION_APPLIED: self._on_correction,
        }.get(event.etype)
        if handler is not None:
            handler(event.seq, p)

    # ---- 批次与记录 ----

    @staticmethod
    def _batch_key(source: str, batch_id: str) -> str:
        from .fingerprints import batch_key

        return batch_key(source, batch_id)

    def _on_batch_opened(self, seq: int, p: dict) -> None:
        key = self._batch_key(p["source"], p["batch_id"])
        batch = self.batches.get(key)
        if batch is None:
            batch = BatchState(source=p["source"], batch_id=p["batch_id"])
            self.batches[key] = batch
        batch.transmissions.append(
            Transmission(
                seq=seq,
                kind="opened",
                content_hash=p["content_hash"],
                record_count=p["record_count"],
                submitted_at=p.get("submitted_at", ""),
            )
        )

    def _on_batch_retransmitted(self, seq: int, p: dict) -> None:
        key = self._batch_key(p["source"], p["batch_id"])
        batch = self.batches[key]
        batch.transmissions.append(
            Transmission(
                seq=seq,
                kind="retransmitted",
                content_hash=p["content_hash"],
                record_count=p["record_count"],
                submitted_at=p.get("submitted_at", ""),
            )
        )

    def _singleton_group_id(self, rid: str) -> str:
        return short_id("grp", "singleton", rid)

    def _on_record_submitted(self, seq: int, p: dict) -> None:
        rid = p["rid"]
        record = self.records.get(rid)
        if record is None:
            record = RecordState(rid=rid, source=p["source"], batch_id=p["batch_id"], entry_no=p["entry_no"])
            self.records[rid] = record
        record.versions.append(
            RecordVersion(
                fingerprint=p["fingerprint"],
                raw=p["raw"],
                clues=p["clues"],
                session=p["session"],
                seq=seq,
            )
        )
        if rid not in self._membership:
            group_id = self._singleton_group_id(rid)
            if group_id not in self.groups:
                self.groups[group_id] = GroupState(
                    group_id=group_id, members=[rid], origin="singleton", created_seq=seq
                )
            self._membership[rid] = group_id

    def _on_record_superseded(self, seq: int, p: dict) -> None:
        record = self.records[p["rid"]]
        for version in record.versions:
            if version.fingerprint == p["old_fingerprint"] and version.superseded_by is None:
                version.superseded_by = p["new_fingerprint"]
                break

    # ---- 去重建议 ----

    def _on_dedupe_run(self, seq: int, p: dict) -> None:
        self.dedupe_runs.append({"seq": seq, **p})

    def _on_suggestion_raised(self, seq: int, p: dict) -> None:
        instances = self.suggestions.setdefault(p["suggestion_id"], [])
        generation = p["generation"]
        if any(inst.generation == generation for inst in instances):
            return
        instances.append(
            SuggestionInstance(
                suggestion_id=p["suggestion_id"],
                generation=generation,
                rids=tuple(p["rids"]),
                edges=p["edges"],
                score=p["score"],
                confidence=p["confidence"],
                fingerprints=tuple(p["fingerprints"]),
            )
        )
        instances.sort(key=lambda inst: inst.generation)

    def _current_instance(self, suggestion_id: str) -> SuggestionInstance | None:
        instances = self.suggestions.get(suggestion_id)
        return instances[-1] if instances else None

    def _on_suggestion_obsoleted(self, seq: int, p: dict) -> None:
        for inst in self.suggestions.get(p["suggestion_id"], []):
            if inst.generation == p["generation"] and inst.status == "open":
                inst.status = "obsoleted"

    def _on_merge_confirmed(self, seq: int, p: dict) -> None:
        inst = self._find_instance(p["suggestion_id"], p["generation"])
        if inst is not None:
            inst.status = "confirmed"
            inst.decided_by = p["operator"]
            inst.decided_seq = seq
            inst.group_id = p["group_id"]
        rids = list(p["rids"])
        group = GroupState(
            group_id=p["group_id"],
            members=rids,
            origin="merge",
            created_seq=seq,
            suggestion_id=p["suggestion_id"],
            suggestion_generation=p["generation"],
        )
        self.groups[p["group_id"]] = group
        for rid in rids:
            self._membership[rid] = p["group_id"]

    def _on_merge_rejected(self, seq: int, p: dict) -> None:
        inst = self._find_instance(p["suggestion_id"], p["generation"])
        if inst is not None:
            inst.status = "rejected"
            inst.decided_by = p["operator"]
            inst.decided_seq = seq

    def _find_instance(self, suggestion_id: str, generation: int) -> SuggestionInstance | None:
        for inst in self.suggestions.get(suggestion_id, []):
            if inst.generation == generation:
                return inst
        return None

    def _on_group_split(self, seq: int, p: dict) -> None:
        parent = self.groups[p["group_id"]]
        peeled = set(p["peeled_rids"])
        remaining = [rid for rid in parent.members if rid not in peeled]
        # 快照拆分时双方证据指纹，供去重判断证据是否已变化。
        for rid_a in peeled:
            for rid_b in remaining:
                self.split_severances[frozenset({rid_a, rid_b})] = {
                    rid_a: self.records[rid_a].current.fingerprint,
                    rid_b: self.records[rid_b].current.fingerprint,
                }
        new_group = GroupState(
            group_id=p["new_group_id"],
            members=sorted(peeled),
            origin="split",
            created_seq=seq,
            parent_id=p["group_id"],
        )
        self.groups[p["new_group_id"]] = new_group
        parent.split_children.append(p["new_group_id"])
        parent.members = remaining
        for rid in peeled:
            self._membership[rid] = p["new_group_id"]

    # ---- 签到 / 取消 / 归档 / 更正 ----

    def _on_checkin(self, seq: int, p: dict) -> None:
        self.checkins.setdefault(p["rid"], []).append({"seq": seq, **p})

    def _on_session_cancelled(self, seq: int, p: dict) -> None:
        self.cancelled_sessions.add((p["site"], p["service_date"], p.get("session_code") or ""))

    def _on_archived(self, seq: int, p: dict) -> None:
        self.groups[p["group_id"]].archived_seq = seq

    def _on_correction(self, seq: int, p: dict) -> None:
        group = self.groups[p["group_id"]]
        group.corrections.append({"seq": seq, **p})
        kind = p["kind"]
        if kind == "minutes_delta":
            group.minutes_delta += float(p["delta_minutes"])
        elif kind == "exclude_record":
            group.excluded.add(p["rid"])
        elif kind == "attach_record":
            group.excluded.discard(p["rid"])
            if p["rid"] not in group.attached and p["rid"] not in group.members:
                group.attached.append(p["rid"])
                self._membership[p["rid"]] = group.group_id

    # ---- 查询 ----

    def group_of(self, rid: str) -> GroupState:
        return self.groups[self._membership[rid]]

    def effective_members(self, group: GroupState) -> list[str]:
        """当前仍归属该组且未被更正剔除的记录。

        记录被后续合并吸收进新组后，其当前归属改变，旧组不再为其计时。
        """
        return [
            rid
            for rid in group.counting_members()
            if self._membership.get(rid) == group.group_id
        ]

    def next_generation(self, suggestion_id: str) -> int:
        instances = self.suggestions.get(suggestion_id, [])
        return 0 if not instances else instances[-1].generation + 1

    def open_suggestions(self) -> list[SuggestionInstance]:
        result = [instances[-1] for instances in self.suggestions.values() if instances[-1].status == "open"]
        result.sort(key=lambda inst: inst.rids)
        return result

    def open_suggestion_for(self, suggestion_id: str) -> SuggestionInstance | None:
        inst = self._current_instance(suggestion_id)
        return inst if inst is not None and inst.status == "open" else None

    def rejected_signatures(self) -> dict[str, tuple[str, ...]]:
        """被人工拒绝且证据未变化的建议指纹集合，去重不得再次提出。"""
        signatures: dict[str, tuple[str, ...]] = {}
        for instances in self.suggestions.values():
            for inst in instances:
                if inst.status == "rejected":
                    signatures[inst.suggestion_id] = inst.fingerprints
        return signatures

    def is_session_cancelled(self, site: str, service_date: str, session_code: str) -> bool:
        if (site, service_date, session_code) in self.cancelled_sessions:
            return True
        # 当天整体取消（场次码为空）覆盖所有场次。
        return (site, service_date, "") in self.cancelled_sessions

    def group_cancelled(self, group: GroupState) -> bool:
        for rid in self.effective_members(group):
            record = self.records.get(rid)
            if record is None:
                continue
            session = record.current.session
            if self.is_session_cancelled(session["site"], session["service_date"], session["session_code"]):
                return True
        return False

    def has_open_suggestion(self, group: GroupState) -> bool:
        members = set(group.members)
        for inst in self.open_suggestions():
            if members & set(inst.rids):
                return True
        return False

    def has_checkin(self, group: GroupState) -> bool:
        return any(self.checkins.get(rid) for rid in self.effective_members(group))

    def derived_state(self, group: GroupState) -> str:
        if group.archived:
            return STATE_ARCHIVED
        if self.group_cancelled(group):
            return STATE_CANCELLED
        if self.has_open_suggestion(group):
            return STATE_PENDING
        if self.has_checkin(group):
            return STATE_RUNNING
        if group.origin in ("merge", "split"):
            return STATE_CONFIRMED
        return STATE_DRAFT

    def is_severed_by_split(self, rid_a: str, rid_b: str) -> bool:
        snapshot = self.split_severances.get(frozenset({rid_a, rid_b}))
        if snapshot is None:
            return False
        # 任一方证据自拆分后被修订，则不再压制（允许重新提请）。
        return all(self.records[rid].current.fingerprint == fp for rid, fp in snapshot.items())

    def lineage(self, rid: str) -> list[dict[str, Any]]:
        """一条记录关联的全部事件（来源谱系）。"""
        result: list[dict[str, Any]] = []
        record = self.records.get(rid)
        if record is not None:
            for version in record.versions:
                result.append(
                    {
                        "seq": version.seq,
                        "kind": "record_version",
                        "fingerprint": version.fingerprint,
                        "batch_id": record.batch_id,
                        "entry_no": record.entry_no,
                        "superseded_by": version.superseded_by,
                    }
                )
        for checkin in self.checkins.get(rid, []):
            result.append({"seq": checkin["seq"], "kind": "checkin", "at": checkin["at"], "late": checkin["late"]})
        for instances in self.suggestions.values():
            for inst in instances:
                if rid in inst.rids:
                    result.append(
                        {
                            "seq": inst.decided_seq or -1,
                            "kind": f"suggestion_{inst.status}",
                            "suggestion_id": inst.suggestion_id,
                            "generation": inst.generation,
                            "confidence": inst.confidence,
                        }
                    )
        for group in self.groups.values():
            if rid in group.members or rid in group.attached:
                result.append(
                    {
                        "seq": group.created_seq,
                        "kind": f"group_{group.origin}",
                        "group_id": group.group_id,
                        "archived_seq": group.archived_seq,
                    }
                )
                for correction in group.corrections:
                    if correction.get("rid") in (None, rid):
                        result.append(
                            {
                                **correction,
                                "seq": correction["seq"],
                                "kind": "correction",
                                "correction_kind": correction["kind"],
                            }
                        )
        result.sort(key=lambda item: (item["seq"], item["kind"]))
        return result
