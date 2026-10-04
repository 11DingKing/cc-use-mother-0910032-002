"""应用服务：摄入、去重建议、人工裁决与归档。

所有写操作都追加事件并带幂等键，因此：

- 补录批次重传不会产生重复计时；
- 去重管理命令可反复执行，建议编号与结论保持稳定；
- 归档后只能以更正事件调整，历史永不被覆盖。
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps
from threading import RLock
from typing import Any, Iterable

from . import auth
from .auth import Principal
from .clock import Clock, SystemClock
from .errors import Conflict, InvalidState, NotFound
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
from .fingerprints import (
    IdentityClues,
    SessionEvidence,
    batch_key,
    batch_stream,
    compare_identity,
    confidence_label,
    extract_clues,
    extract_session,
    record_id,
    same_session,
    service_minutes,
    source_fingerprint,
)
from .hashing import canonical_json, digest, short_id
from .projection import Projection, SuggestionInstance
from .store import EventStore

ALGORITHM_VERSION = "dedup-v3"
DEFAULT_LATE_GRACE_MINUTES = 15


def _locked(method):
    """串行化"读投影—判断—追加事件"序列，保证并发请求结论一致。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class ServiceArchive:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self._lock = RLock()
        self._rebuild()

    # ==================================================================
    # 投影重建
    # ==================================================================

    def _rebuild(self) -> None:
        self.projection = Projection()
        self.projection.load(self.store.read_all())

    # ==================================================================
    # 批次摄入与重传
    # ==================================================================

    @_locked
    def submit_batch(
        self,
        source: str,
        batch_id: str,
        entries: list[dict[str, Any]],
        submitted_at: str | None = None,
    ) -> dict[str, Any]:
        """学校集中提交（或重传）一个补录批次。

        返回 {"retransmitted": ..., "records": [...], "changed": [...]}。
        内容完全相同的重传只记录谱系，不产生任何新计时。
        """
        if not source or not batch_id:
            raise ValueError("source 与 batch_id 不能为空")
        key = batch_key(source, batch_id)
        content_hash = "batch_" + digest(canonical_json(entries))
        stream = batch_stream(source, batch_id)
        existing = self.projection.batches.get(key)
        now = self._now()
        retransmitted = existing is not None
        delivered_at = submitted_at or now

        # 同一批次、同一内容、同一交付时刻的重试：整体折叠，不追加任何事件。
        same_delivery = (
            existing is not None
            and any(
                t.content_hash == content_hash and t.submitted_at == delivered_at
                for t in existing.transmissions
            )
        )
        if same_delivery:
            pass  # 下方逐条记录同样全部幂等命中
        elif not retransmitted:
            self._append(
                stream,
                BATCH_OPENED,
                {
                    "source": source,
                    "batch_id": batch_id,
                    "content_hash": content_hash,
                    "record_count": len(entries),
                    "submitted_at": delivered_at,
                },
                idempotency_key=f"open:{key}:{content_hash}:{delivered_at}",
            )
        elif existing.current_content_hash != content_hash:
            # 内容有修订：记录修订重传，逐条产生新版本。
            self._append(
                stream,
                BATCH_RETRANSMITTED,
                {
                    "source": source,
                    "batch_id": batch_id,
                    "content_hash": content_hash,
                    "record_count": len(entries),
                    "submitted_at": delivered_at,
                    "revision": True,
                },
                idempotency_key=f"retry:{key}:{content_hash}:{delivered_at}",
            )
        else:
            # 内容一致的重复交付：按交付时刻留谱系；同时刻的重试被幂等折叠。
            self._append(
                stream,
                BATCH_RETRANSMITTED,
                {
                    "source": source,
                    "batch_id": batch_id,
                    "content_hash": content_hash,
                    "record_count": len(entries),
                    "submitted_at": delivered_at,
                    "revision": False,
                },
                idempotency_key=f"retry:{key}:{content_hash}:{delivered_at}",
            )

        changed: list[str] = []
        rids: list[str] = []
        for index, raw in enumerate(entries):
            entry_no = raw.get("entry_no", index)
            rid = record_id(source, batch_id, entry_no)
            fp = source_fingerprint(raw)
            rids.append(rid)
            record = self.projection.records.get(rid)
            if record is not None and fp in record.fingerprints:
                continue  # 同一条号且报文一致：幂等跳过
            clues = extract_clues(raw)
            session = extract_session(raw)
            if not clues.name:
                raise ValueError(f"条目 {entry_no} 缺少志愿者姓名")
            if not session.site or not session.service_date:
                raise ValueError(f"条目 {entry_no} 缺少场馆或服务日期")
            previous_fp = record.current.fingerprint if record is not None else None
            self._append(
                stream,
                RECORD_SUBMITTED,
                {
                    "rid": rid,
                    "source": source,
                    "batch_id": batch_id,
                    "entry_no": entry_no,
                    "fingerprint": fp,
                    "raw": raw,
                    "clues": clues.as_dict(),
                    "session": session.as_dict(),
                },
                idempotency_key=f"rec:{rid}:{fp}",
            )
            if record is not None:
                self._append(
                    stream,
                    RECORD_SUPERSEDED,
                    {"rid": rid, "old_fingerprint": previous_fp, "new_fingerprint": fp},
                    idempotency_key=f"sup:{rid}:{previous_fp}:{fp}",
                )
            changed.append(rid)

        return {
            "batch_stream": stream,
            "content_hash": content_hash,
            "retransmitted": retransmitted,
            "record_count": len(entries),
            "records": rids,
            "changed": changed,
        }

    # ==================================================================
    # 去重（管理命令，可重跑）
    # ==================================================================

    @_locked
    def run_dedupe(self, run_id: str | None = None) -> dict[str, Any]:
        """扫描全部记录，生成/刷新合并建议。

        默认 run_id 由算法版本与全部当前指纹决定：相同数据重跑得到相同
        run_id，事件幂等保证零新增；数据变化后 run_id 自然变化。
        """
        candidates = self._find_candidates()
        all_fps = sorted(
            {record.current.fingerprint for record in self.projection.records.values()}
        )
        if run_id is None:
            run_id = short_id("run", ALGORITHM_VERSION, *all_fps)

        raised: list[str] = []
        obsoleted: list[str] = []
        suppressed: list[dict[str, str]] = []
        current_fingerprints: dict[str, tuple[str, ...]] = {}
        for component in candidates:
            rids = tuple(component["rids"])
            sid = short_id("sug", *rids)
            current_fingerprints[sid] = tuple(
                sorted(self.projection.records[rid].current.fingerprint for rid in rids)
            )
            outcome = self._raise_or_refresh(component)
            if outcome["action"] == "raised":
                raised.append(outcome["key"])
            elif outcome["action"] == "obsoleted":
                obsoleted.append(outcome["key"])
            elif outcome["action"] == "suppressed":
                suppressed.append({"suggestion_id": outcome["suggestion_id"], "reason": outcome["reason"]})

        # 候选组成已变化（修订导致 rids 改变）时，清扫仍然挂起的旧建议。
        for inst in list(self.projection.open_suggestions()):
            if inst.suggestion_id not in current_fingerprints:
                self._obsolete(inst, "候选组已不存在")
                obsoleted.append(inst.key)

        self._append(
            "management",
            DEDUPE_RUN,
            {
                "run_id": run_id,
                "algorithm_version": ALGORITHM_VERSION,
                "candidate_count": len(candidates),
                "raised": raised,
                "obsoleted": obsoleted,
                "suppressed": suppressed,
                "fingerprint_count": len(all_fps),
            },
            idempotency_key=f"run:{run_id}",
        )
        return {
            "run_id": run_id,
            "candidate_count": len(candidates),
            "raised": raised,
            "obsoleted": obsoleted,
            "suppressed": suppressed,
        }

    def _find_candidates(self) -> list[dict[str, Any]]:
        """按场次桶两两比对，连通分量即为候选组。"""
        # (site, date) -> 记录列表；同场次编码或时间重叠才连边。
        day_buckets: dict[tuple[str, str], list[str]] = {}
        for rid, record in self.projection.records.items():
            session = record.current.session
            if self.projection.is_session_cancelled(session["site"], session["service_date"], session["session_code"]):
                continue
            day_buckets.setdefault((session["site"], session["service_date"]), []).append(rid)

        edges_out: list[dict[str, Any]] = []
        adjacency: dict[str, set[str]] = {}
        for rids in day_buckets.values():
            for i in range(len(rids)):
                for j in range(i + 1, len(rids)):
                    edge = self._compare_pair(rids[i], rids[j])
                    if edge is None:
                        continue
                    a, b = sorted([rids[i], rids[j]])
                    edges_out.append({**edge, "a": a, "b": b})
                    adjacency.setdefault(a, set()).add(b)
                    adjacency.setdefault(b, set()).add(a)

        components = self._connected_components(adjacency)
        result: list[dict[str, Any]] = []
        for members in components:
            if len(members) < 2:
                continue
            ordered = sorted(members)
            member_edges = sorted(
                (e for e in edges_out if e["a"] in members and e["b"] in members),
                key=lambda e: (e["a"], e["b"]),
            )
            # 同一场次只允许在组内连边；跨场次连通要拆开。
            comps = self._split_by_session(ordered, member_edges)
            for comp_members in comps:
                if len(comp_members) < 2:
                    continue
                comp_edges = [
                    e for e in member_edges if e["a"] in comp_members and e["b"] in comp_members
                ]
                score = max(e["identity_score"] for e in comp_edges)
                strong = all(e["same_session"] for e in comp_edges)
                comp_members = sorted(comp_members)
                result.append(
                    {
                        "rids": tuple(comp_members),
                        "edges": [
                            {
                                "a": e["a"],
                                "b": e["b"],
                                "identity_reasons": list(e["identity_reasons"]),
                                "identity_score": e["identity_score"],
                                "session_reason": e["session_reason"],
                            }
                            for e in comp_edges
                        ],
                        "score": score,
                        "strong_session": strong,
                    }
                )
        result.sort(key=lambda c: c["rids"])
        return result

    def _compare_pair(self, rid_a: str, rid_b: str) -> dict[str, Any] | None:
        ra = self.projection.records[rid_a].current
        rb = self.projection.records[rid_b].current
        clues_a = IdentityClues(**ra.clues)
        clues_b = IdentityClues(**rb.clues)
        sess_a = SessionEvidence(**ra.session)
        sess_b = SessionEvidence(**rb.session)
        identity = compare_identity(clues_a, clues_b)
        if identity is None:
            return None
        # 仅凭手机尾号撞号不得连边：未成年人常共用监护人手机号。
        # 姓名或证件尾号至少有一项一致，才构成身份候选。
        if not ({"姓名一致", "证件尾号一致"} & set(identity.reasons)):
            return None
        same, session_reason = same_session(sess_a, sess_b)
        if not same:
            return None
        if self.projection.is_severed_by_split(rid_a, rid_b):
            return None
        return {
            "identity_reasons": identity.reasons,
            "identity_score": identity.score,
            "session_reason": session_reason,
            "same_session": True,
        }

    @staticmethod
    def _connected_components(adjacency: dict[str, set[str]]) -> list[set[str]]:
        seen: set[str] = set()
        components: list[set[str]] = []
        for node in sorted(adjacency):
            if node in seen:
                continue
            stack = [node]
            component: set[str] = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.add(current)
                stack.extend(adjacency[current] - seen)
            components.append(component)
        return components

    @staticmethod
    def _split_by_session(members: list[str], edges: list[dict[str, Any]]) -> list[set[str]]:
        adjacency: dict[str, set[str]] = {rid: set() for rid in members}
        for edge in edges:
            adjacency[edge["a"]].add(edge["b"])
            adjacency[edge["b"]].add(edge["a"])
        return ServiceArchive._connected_components(adjacency)

    def _obsolete(self, inst, reason: str) -> None:
        self._append(
            f"suggestion:{inst.suggestion_id}",
            SUGGESTION_OBSOLETED,
            {
                "suggestion_id": inst.suggestion_id,
                "generation": inst.generation,
                "reason": reason,
            },
            idempotency_key=f"obs:{inst.suggestion_id}:{inst.generation}",
        )

    def _raise_or_refresh(self, component: dict[str, Any]) -> dict[str, Any]:
        rids = tuple(component["rids"])
        suggestion_id = short_id("sug", *rids)
        fingerprints = tuple(
            sorted(self.projection.records[rid].current.fingerprint for rid in rids)
        )
        instances = self.projection.suggestions.get(suggestion_id, [])
        latest = instances[-1] if instances else None

        # 已整组在同一合并组中：无需再提。
        group_ids = {self.projection.group_of(rid).group_id for rid in rids}
        if len(group_ids) == 1:
            only_group = self.projection.group_of(next(iter(rids)))
            if only_group.origin in ("merge", "split") and set(only_group.members) >= set(rids):
                return {"action": "suppressed", "suggestion_id": suggestion_id, "reason": "已合并"}

        confidence = confidence_label(component["score"], component["strong_session"])
        edges = component["edges"]
        now = self._now()

        if latest is None:
            generation = 0
        elif latest.fingerprints == fingerprints:
            if latest.status == "open":
                return {"action": "suppressed", "suggestion_id": suggestion_id, "reason": "建议已存在"}
            if latest.status == "rejected":
                return {"action": "suppressed", "suggestion_id": suggestion_id, "reason": "已人工拒绝且证据未变"}
            if latest.status == "confirmed":
                return {"action": "suppressed", "suggestion_id": suggestion_id, "reason": "已确认合并"}
            generation = self.projection.next_generation(suggestion_id)
        else:
            # 证据发生变化（有记录被修订）：旧一代作废，新一代重新提请。
            generation = self.projection.next_generation(suggestion_id)
            if latest.status == "open":
                self._append(
                    f"suggestion:{suggestion_id}",
                    SUGGESTION_OBSOLETED,
                    {
                        "suggestion_id": suggestion_id,
                        "generation": latest.generation,
                        "reason": "来源记录修订，证据指纹变化",
                    },
                    idempotency_key=f"obs:{suggestion_id}:{latest.generation}",
                )

        self._append(
            f"suggestion:{suggestion_id}",
            SUGGESTION_RAISED,
            {
                "suggestion_id": suggestion_id,
                "generation": generation,
                "rids": list(rids),
                "edges": edges,
                "score": component["score"],
                "confidence": confidence,
                "fingerprints": list(fingerprints),
                "raised_at": now,
            },
            idempotency_key=f"sug:{suggestion_id}:g{generation}:{digest(*fingerprints)}",
        )
        return {"action": "raised", "key": f"{suggestion_id}#g{generation}"}

    # ==================================================================
    # 人工裁决
    # ==================================================================

    @_locked
    def confirm_merge(
        self,
        suggestion_id: str,
        principal: Principal,
        note: str = "",
        canonical_rid: str | None = None,
    ) -> dict[str, Any]:
        auth.require_decider(principal)
        inst = self.projection.open_suggestion_for(suggestion_id)
        if inst is None:
            raise NotFound(f"没有待确认的建议 {suggestion_id}")
        rids = list(inst.rids)
        if canonical_rid is not None and canonical_rid not in rids:
            raise ValueError("canonical_rid 必须是建议成员之一")
        group_id = short_id("grp", "merge", suggestion_id, f"g{inst.generation}")
        self._append(
            f"suggestion:{suggestion_id}",
            MERGE_CONFIRMED,
            {
                "suggestion_id": suggestion_id,
                "generation": inst.generation,
                "rids": rids,
                "group_id": group_id,
                "canonical_rid": canonical_rid or rids[0],
                "operator": principal.name,
                "note": note,
            },
            operator=principal.name,
            idempotency_key=f"confirm:{suggestion_id}:g{inst.generation}",
        )
        return {"group_id": group_id, "rids": rids}

    @_locked
    def reject_merge(self, suggestion_id: str, principal: Principal, reason: str = "") -> dict[str, Any]:
        auth.require_decider(principal)
        inst = self.projection.open_suggestion_for(suggestion_id)
        if inst is None:
            raise NotFound(f"没有待拒绝的建议 {suggestion_id}")
        self._append(
            f"suggestion:{suggestion_id}",
            MERGE_REJECTED,
            {
                "suggestion_id": suggestion_id,
                "generation": inst.generation,
                "operator": principal.name,
                "reason": reason,
            },
            operator=principal.name,
            idempotency_key=f"reject:{suggestion_id}:g{inst.generation}",
        )
        return {"suggestion_id": suggestion_id, "status": "rejected"}

    @_locked
    def split_group(
        self,
        group_id: str,
        peeled_rids: Iterable[str],
        principal: Principal,
        reason: str = "",
    ) -> dict[str, Any]:
        """拆分误合并：把指定记录剥到新组，保留父子谱系。归档后禁止。"""
        auth.require_decider(principal)
        group = self.projection.groups.get(group_id)
        if group is None:
            raise NotFound(f"分组 {group_id} 不存在")
        if group.archived:
            raise InvalidState("分组已归档，不能直接拆分；请使用更正事件")
        if group.origin != "merge":
            raise InvalidState("只有人工合并形成的分组可以拆分")
        peeled = sorted(set(peeled_rids))
        if not peeled or set(peeled) >= set(group.members):
            raise ValueError("拆分必须保留至少一条记录在原组")
        if not set(peeled) <= set(group.members):
            raise ValueError("待拆分记录不在该分组内")
        new_group_id = short_id("grp", "split", group_id, *peeled)
        self._append(
            f"group:{group_id}",
            GROUP_SPLIT,
            {
                "group_id": group_id,
                "new_group_id": new_group_id,
                "peeled_rids": peeled,
                "operator": principal.name,
                "reason": reason,
            },
            operator=principal.name,
            idempotency_key=f"split:{new_group_id}",
        )
        return {"group_id": group_id, "new_group_id": new_group_id, "peeled_rids": peeled}

    # ==================================================================
    # 签到（含迟到）、场次取消
    # ==================================================================

    @_locked
    def record_checkin(
        self,
        rid: str,
        at: str | None = None,
        late_grace_minutes: int = DEFAULT_LATE_GRACE_MINUTES,
    ) -> dict[str, Any]:
        if rid not in self.projection.records:
            raise NotFound(f"记录 {rid} 不存在")
        record = self.projection.records[rid]
        session = record.current.session
        if self.projection.is_session_cancelled(session["site"], session["service_date"], session["session_code"]):
            raise InvalidState("场次已取消，不能签到")
        moment = datetime.fromisoformat(at) if at else datetime.fromisoformat(self._now())
        idem_key = f"checkin:{rid}:{moment.isoformat()}"
        late = self._is_late(session, moment, late_grace_minutes)
        self._append(
            f"record:{rid}",
            CHECKIN_RECORDED,
            {
                "rid": rid,
                "at": moment.isoformat(),
                "late": late,
                "late_grace_minutes": late_grace_minutes,
            },
            idempotency_key=idem_key,
        )
        return {"rid": rid, "at": moment.isoformat(), "late": late}

    @staticmethod
    def _is_late(session: dict[str, Any], moment: datetime, grace: int) -> bool:
        start_min = session.get("start_min")
        if start_min is None:
            return False
        service_date = session.get("service_date")
        if service_date and moment.date().isoformat() != service_date:
            return True
        actual = moment.hour * 60 + moment.minute
        return actual > start_min + grace

    @_locked
    def cancel_session(
        self,
        site: str,
        service_date: str,
        principal: Principal,
        session_code: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        auth.require_session_canceller(principal)
        code = session_code.strip().upper()
        idem = f"cancel:{site}|{service_date}|{code}"
        self._append(
            f"session:{site}|{service_date}",
            SESSION_CANCELLED,
            {
                "site": site,
                "service_date": service_date,
                "session_code": code,
                "operator": principal.name,
                "reason": reason,
            },
            operator=principal.name,
            idempotency_key=idem,
        )
        # 取消场次后，相关待决建议失去撮合基础，自动作废（保留谱系）。
        for inst in list(self.projection.open_suggestions()):
            if self._suggestion_touches_session(inst, site, service_date, code):
                self._obsolete(inst, f"场次 {code or '全天'} 已取消")
        return {"site": site, "service_date": service_date, "session_code": code, "status": "cancelled"}

    def _suggestion_touches_session(
        self, inst: SuggestionInstance, site: str, service_date: str, code: str
    ) -> bool:
        for rid in inst.rids:
            record = self.projection.records.get(rid)
            if record is None:
                continue
            session = record.current.session
            if session["site"] != site or session["service_date"] != service_date:
                continue
            if not code or session["session_code"] == code:
                return True
        return False

    # ==================================================================
    # 归档与更正
    # ==================================================================

    @_locked
    def archive_group(self, group_id: str, principal: Principal) -> dict[str, Any]:
        auth.require_decider(principal)
        group = self.projection.groups.get(group_id)
        if group is None:
            raise NotFound(f"分组 {group_id} 不存在")
        if group.archived:
            raise Conflict("分组已归档")
        if self.projection.has_open_suggestion(group):
            raise InvalidState("仍有未处理的合并建议，不能归档")
        self._append(
            f"group:{group_id}",
            GROUP_ARCHIVED,
            {"group_id": group_id, "operator": principal.name},
            operator=principal.name,
            idempotency_key=f"archive:{group_id}",
        )
        return {"group_id": group_id, "status": "已归档"}

    @_locked
    def apply_correction(
        self,
        group_id: str,
        kind: str,
        principal: Principal,
        reason: str,
        delta_minutes: float = 0.0,
        rid: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """归档后的唯一调整通道。

        kind: minutes_delta（计时更正）/ exclude_record（剔除重复或无效记录）/
              attach_record（补挂遗漏记录）/ note（仅备注）。
        """
        auth.require_decider(principal)
        group = self.projection.groups.get(group_id)
        if group is None:
            raise NotFound(f"分组 {group_id} 不存在")
        if not group.archived:
            raise InvalidState("分组尚未归档；归档前请直接使用合并/拆分等操作")
        if kind not in {"minutes_delta", "exclude_record", "attach_record", "note"}:
            raise ValueError(f"未知更正类型 {kind}")
        if kind in {"exclude_record", "attach_record"} and not rid:
            raise ValueError(f"{kind} 必须指定 rid")
        if kind == "attach_record" and rid not in self.projection.records:
            raise NotFound(f"记录 {rid} 不存在")
        correction_id = short_id(
            "cor", group_id, kind, reason, str(delta_minutes), rid or "", note
        )
        payload = {
            "group_id": group_id,
            "correction_id": correction_id,
            "kind": kind,
            "reason": reason,
            "delta_minutes": delta_minutes,
            "rid": rid,
            "note": note,
            "operator": principal.name,
        }
        self._append(
            f"group:{group_id}",
            CORRECTION_APPLIED,
            payload,
            operator=principal.name,
            idempotency_key=f"corr:{correction_id}",
        )
        return payload

    # ==================================================================
    # 读模型
    # ==================================================================

    @_locked
    def list_suggestions(self) -> list[dict[str, Any]]:
        result = []
        for inst in self.projection.open_suggestions():
            result.append(self._suggestion_view(inst))
        return result

    @_locked
    def suggestion_detail(self, suggestion_id: str) -> dict[str, Any]:
        instances = self.projection.suggestions.get(suggestion_id)
        if not instances:
            raise NotFound(f"建议 {suggestion_id} 不存在")
        return {
            "suggestion_id": suggestion_id,
            "generations": [self._suggestion_view(inst) for inst in instances],
        }

    def _suggestion_view(self, inst: SuggestionInstance) -> dict[str, Any]:
        return {
            "suggestion_id": inst.suggestion_id,
            "generation": inst.generation,
            "status": inst.status,
            "confidence": inst.confidence,
            "score": inst.score,
            "rids": list(inst.rids),
            "edges": inst.edges,
            "fingerprints": list(inst.fingerprints),
            "decided_by": inst.decided_by,
            "group_id": inst.group_id,
        }

    @_locked
    def list_groups(self) -> list[dict[str, Any]]:
        result = []
        for group_id in sorted(self.projection.groups):
            result.append(self.group_view(group_id))
        return result

    @_locked
    def group_view(self, group_id: str) -> dict[str, Any]:
        group = self.projection.groups.get(group_id)
        if group is None:
            raise NotFound(f"分组 {group_id} 不存在")
        counting = self.projection.effective_members(group)
        canonical = counting[0] if counting else (group.members[0] if group.members else "")
        record = self.projection.records.get(canonical) if canonical else None
        minutes = service_minutes(SessionEvidence(**record.current.session)) if record else 0.0
        minutes += group.minutes_delta
        members_view = []
        for rid in group.members:
            r = self.projection.records[rid]
            members_view.append(
                {
                    "rid": rid,
                    "excluded": rid in group.excluded,
                    "source": r.source,
                    "batch_id": r.batch_id,
                    "entry_no": r.entry_no,
                    "fingerprint": r.current.fingerprint,
                    "version_count": len(r.versions),
                    "clues": r.current.clues,
                    "session": r.current.session,
                }
            )
        return {
            "group_id": group_id,
            "state": self.projection.derived_state(group),
            "origin": group.origin,
            "parent_id": group.parent_id,
            "suggestion_id": group.suggestion_id,
            "split_children": list(group.split_children),
            "members": members_view,
            "attached": list(group.attached),
            "excluded": sorted(group.excluded),
            "counted_record_count": len(counting),
            "minutes": max(0.0, minutes),
            "archived": group.archived,
            "corrections": list(group.corrections),
        }

    @_locked
    def record_lineage(self, rid: str) -> dict[str, Any]:
        if rid not in self.projection.records:
            raise NotFound(f"记录 {rid} 不存在")
        record = self.projection.records[rid]
        return {
            "rid": rid,
            "source": record.source,
            "batch_id": record.batch_id,
            "entry_no": record.entry_no,
            "current_fingerprint": record.current.fingerprint,
            "lineage": self.projection.lineage(rid),
        }

    @_locked
    def batch_view(self, source: str, batch_id: str) -> dict[str, Any]:
        key = batch_key(source, batch_id)
        batch = self.projection.batches.get(key)
        if batch is None:
            raise NotFound(f"批次 {source}/{batch_id} 不存在")
        return {
            "source": batch.source,
            "batch_id": batch.batch_id,
            "stream": batch.stream,
            "transmission_count": len(batch.transmissions),
            "transmissions": [
                {
                    "seq": t.seq,
                    "kind": t.kind,
                    "content_hash": t.content_hash,
                    "record_count": t.record_count,
                    "submitted_at": t.submitted_at,
                }
                for t in batch.transmissions
            ],
        }

    @_locked
    def event_log(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.store.read_all()]

    # ==================================================================
    # 内部
    # ==================================================================

    def _append(self, stream: str, etype: str, payload: dict, **kwargs: Any):
        event, _ = self.store.append(
            stream=stream, etype=etype, payload=payload, created_at=self._now(), **kwargs
        )
        self.projection.apply(event)
        return event

    def _now(self) -> str:
        return self.clock.now().isoformat()
