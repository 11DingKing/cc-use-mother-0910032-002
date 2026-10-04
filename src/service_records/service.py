"""应用服务层：用例编排、校验与授权。

所有状态变更都经过本层并写入追加式事件流；存储层本身不做业务决策。
管理命令与 HTTP 接口都只调用这里的方法，保证两条入口行为一致。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .auth import Actor
from .canonical import canonical_dumps, content_id
from .matching import build_proposals
from .models import (
    CAND_CONFIRMED,
    CAND_PROPOSED,
    CAND_REJECTED,
    ROLE_DUPLICATE,
    ROLE_SURVIVOR,
    ST_ARCHIVED,
    ST_CANCELLED,
    ST_CONFIRMED,
    ST_PENDING,
    ST_RECEIVED,
    EV_BATCH_RETRANSMIT,
    EV_CANDIDATE_CONFIRMED,
    EV_CANDIDATE_PROPOSED,
    EV_CANDIDATE_REJECTED,
    EV_LATE_CHECKIN,
    EV_MERGE_SPLIT,
    EV_RECORD_ARCHIVED,
    EV_RECORD_CORRECTED,
    EV_RECORD_RECEIVED,
    EV_RECORD_VERIFIED,
    EV_SESSION_CANCELLED,
    Submission,
)
from .store import Store


class ServiceError(Exception):
    """服务层错误基类。"""


class ValidationError(ServiceError, ValueError):
    """输入不满足领域约束。"""


class NotFoundError(ServiceError, KeyError):
    """对象不存在。"""


class ConflictError(ServiceError):
    """当前状态不允许该操作。"""


# 归档后允许通过更正事件修改的字段白名单
CORRECTABLE_FIELDS = frozenset(
    {"volunteer_name", "id_tail", "service_start", "service_end", "minutes"}
)

# 来源指纹的内容范围：同一份材料重传必然一致，身份线索不同则是另一份来源
FINGERPRINT_FIELDS = (
    "school_code",
    "submitter",
    "batch_no",
    "volunteer_name",
    "id_tail",
    "session_code",
    "service_start",
    "service_end",
    "minutes",
    "checkin_at",
    "payload",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_iso(value: str, field: str) -> None:
    if not value:
        raise ValidationError(f"{field}不能为空")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}不是合法的 ISO8601 时间：{value}") from exc


class DedupService:
    def __init__(self, store: Store, clock: Callable[[], str] = _utc_now_iso):
        self.store = store
        self.now = clock

    # ------------------------------------------------------------ 收件登记

    @staticmethod
    def fingerprint_of(sub: Submission) -> str:
        body = {key: getattr(sub, key) for key in FINGERPRINT_FIELDS}
        return content_id("fp_", body, length=32)

    def _validate_submission(self, sub: Submission) -> None:
        for field in ("school_code", "submitter", "volunteer_name",
                      "session_code", "batch_no"):
            if not getattr(sub, field):
                raise ValidationError(f"{field}不能为空")
        validate_iso(sub.service_start, "service_start")
        validate_iso(sub.service_end, "service_end")
        if sub.service_end <= sub.service_start:
            raise ValidationError("服务结束时间必须晚于开始时间")
        if not isinstance(sub.minutes, int) or sub.minutes <= 0:
            raise ValidationError("minutes 必须是正整数")
        if sub.checkin_at:
            validate_iso(sub.checkin_at, "checkin_at")

    def receive(self, actor: Actor, sub: Submission) -> dict:
        """登记一份服务证明材料；相同指纹重传为幂等操作并保留重传谱系。"""
        self._validate_submission(sub)
        fp = self.fingerprint_of(sub)
        ts = self.now()
        existing = self.store.find_by_fingerprint(fp)
        if existing is not None:
            # 批量重传：不新建记录、不重复计时，仅累加来源计数并留痕
            seq = (self.store.get_source(fp) or {}).get("transmit_count", 1)
            self.store.touch_source(fp, existing["id"], ts, sub.batch_no, seq + 1)
            self.store.update_transmit(existing["id"], seq + 1)
            self.store.add_event(
                EV_BATCH_RETRANSMIT,
                actor.account,
                sub.transmitted_at or ts,
                {
                    "fingerprint": fp,
                    "record_id": existing["id"],
                    "batch_no": sub.batch_no,
                    "transmit_seq": seq + 1,
                    "school_code": sub.school_code,
                },
                record_id=existing["id"],
            )
            self.store.commit()
            return {
                "record_id": existing["id"],
                "fingerprint": fp,
                "retransmitted": True,
                "transmit_seq": seq + 1,
            }

        record_id = content_id("rec_", fp, length=20)
        self.store.insert_record(
            {
                "id": record_id,
                "fingerprint": fp,
                "school_code": sub.school_code,
                "submitter": sub.submitter,
                "volunteer_name": sub.volunteer_name,
                "id_tail": sub.id_tail or "",
                "session_code": sub.session_code,
                "session_name": sub.session_name or "",
                "service_start": sub.service_start,
                "service_end": sub.service_end,
                "minutes": sub.minutes,
                "checkin_at": sub.checkin_at,
                "batch_no": sub.batch_no,
                "transmit_seq": 1,
                "payload_json": canonical_dumps(sub.payload),
                "status": ST_RECEIVED,
                "merged_into": None,
                "created_at": ts,
            }
        )
        self.store.touch_source(fp, record_id, ts, sub.batch_no, 1)
        self.store.add_event(
            EV_RECORD_RECEIVED,
            actor.account,
            ts,
            {
                "fingerprint": fp,
                "batch_no": sub.batch_no,
                "school_code": sub.school_code,
                "submitter": sub.submitter,
                "session_code": sub.session_code,
                "transmit_seq": 1,
            },
            record_id=record_id,
        )
        self.store.commit()
        return {
            "record_id": record_id,
            "fingerprint": fp,
            "retransmitted": False,
            "transmit_seq": 1,
        }

    def receive_batch(self, actor: Actor, submissions: Sequence[Submission]) -> dict:
        accepted, retransmitted = [], []
        for sub in submissions:
            result = self.receive(actor, sub)
            (retransmitted if result["retransmitted"] else accepted).append(result)
        return {
            "accepted": len(accepted),
            "retransmitted": len(retransmitted),
            "record_ids": [r["record_id"] for r in accepted + retransmitted],
        }

    # ------------------------------------------------------------ 去重建议

    def run_dedup(self, actor: Actor) -> dict:
        """重跑去重识别。纯函数式产出，重复执行结果稳定、不重复成案。"""
        started = self.now()
        active = self.store.active_records_for_dedup()
        blocked = self.store.all_blocked_pairs()
        proposals = build_proposals(active, blocked)

        created, skipped = [], []
        for proposal in proposals:
            members = sorted([proposal.survivor_id, *proposal.duplicates])
            existing = self.store.find_candidate_by_members(members)
            if existing is not None:
                skipped.append(existing["id"])
                continue
            cand_id = self.store.candidate_id(members)
            self.store.insert_candidate(
                {
                    "id": cand_id,
                    "status": CAND_PROPOSED,
                    "survivor_id": proposal.survivor_id,
                    "members_json": canonical_dumps(members),
                    "scores_json": canonical_dumps(
                        [
                            {
                                "record_id": s.record_id,
                                "name": s.name,
                                "id_tail": s.id_tail,
                                "session_code": s.session_code,
                                "name_score": s.name_score,
                                "id_score": s.id_score,
                                "session_score": s.session_score,
                                "total": s.total,
                                "reasons": list(s.reasons),
                            }
                            for s in proposal.scores
                        ]
                    ),
                    "reasons_json": canonical_dumps(list(proposal.reasons)),
                    "run_no": 0,
                    "created_at": started,
                    "decided_by": None,
                    "decided_at": None,
                    "decision_note": None,
                    "members_roles": [
                        (rid, ROLE_SURVIVOR if rid == proposal.survivor_id
                         else ROLE_DUPLICATE)
                        for rid in members
                    ],
                }
            )
            self.store.set_status(members, ST_PENDING)
            for rid in members:
                self.store.add_event(
                    EV_CANDIDATE_PROPOSED,
                    actor.account,
                    started,
                    {
                        "candidate_id": cand_id,
                        "survivor_id": proposal.survivor_id,
                        "members": members,
                        "role": (ROLE_SURVIVOR if rid == proposal.survivor_id
                                else ROLE_DUPLICATE),
                        "reasons": list(proposal.reasons),
                    },
                    record_id=rid,
                )
            created.append(cand_id)

        input_sig = content_id(
            "run_",
            {
                "active": sorted(r["fingerprint"] for r in active),
                "blocked": sorted(blocked),
            },
            length=20,
        )
        stats = {
            "active_records": len(active),
            "proposed_new": len(created),
            "skipped_existing": len(skipped),
            "candidate_ids": sorted(created),
            "skipped_ids": sorted(skipped),
        }
        run_no = self.store.record_run(
            "dedup", started, self.now(), stats, input_sig
        )
        # 回填候选所属运行号
        for cand_id in created:
            self.store.set_candidate_run_no(cand_id, run_no)
        self.store.commit()
        return {"run_no": run_no, "input_sig": input_sig, **stats}

    def list_proposals(self, status: str | None = CAND_PROPOSED) -> list[dict]:
        out = []
        for cand in self.store.list_candidates(status):
            out.append(self._candidate_view(cand))
        return out

    def _candidate_view(self, cand: dict) -> dict:
        import json

        return {
            "id": cand["id"],
            "status": cand["status"],
            "survivor_id": cand["survivor_id"],
            "members": json.loads(cand["members_json"]),
            "scores": json.loads(cand["scores_json"]),
            "reasons": json.loads(cand["reasons_json"]),
            "run_no": cand["run_no"],
            "created_at": cand["created_at"],
            "decided_by": cand["decided_by"],
            "decided_at": cand["decided_at"],
            "decision_note": cand["decision_note"],
        }

    # ------------------------------------------------------------ 人工裁决

    def _require_proposed(self, candidate_id: str) -> dict:
        cand = self.store.get_candidate(candidate_id)
        if cand is None:
            raise NotFoundError(f"冲突候选不存在：{candidate_id}")
        if cand["status"] != CAND_PROPOSED:
            raise ConflictError(f"候选状态为 {cand['status']}，无法再裁决")
        return cand

    def confirm_merge(self, actor: Actor, candidate_id: str,
                      note: str = "") -> dict:
        """授权人员确认合并：主记录保留计时，重复记录只保留谱系不计时。"""
        actor.require_decision_role("合并确认")
        cand = self._require_proposed(candidate_id)
        import json

        members = json.loads(cand["members_json"])
        survivor, duplicates = cand["survivor_id"], [
            m for m in members if m != cand["survivor_id"]
        ]
        ts = self.now()
        self.store.decide_candidate(candidate_id, CAND_CONFIRMED,
                                    actor.account, ts, note)
        self.store.set_merged_into(duplicates, survivor)
        self.store.mark_survivor_confirmed(survivor)
        for rid in members:
            self.store.add_event(
                EV_CANDIDATE_CONFIRMED,
                actor.account,
                ts,
                {
                    "candidate_id": candidate_id,
                    "survivor_id": survivor,
                    "members": members,
                    "role": ROLE_SURVIVOR if rid == survivor else ROLE_DUPLICATE,
                    "note": note,
                },
                record_id=rid,
            )
        self.store.commit()
        return {"candidate_id": candidate_id, "status": CAND_CONFIRMED,
                "survivor_id": survivor, "duplicates": duplicates}

    def reject_candidate(self, actor: Actor, candidate_id: str,
                         note: str = "") -> dict:
        """授权人员拒绝合并：记录对被永久标记，重跑去重不会再次成案。"""
        actor.require_decision_role("合并拒绝")
        cand = self._require_proposed(candidate_id)
        import json

        members = json.loads(cand["members_json"])
        ts = self.now()
        self.store.decide_candidate(candidate_id, CAND_REJECTED,
                                    actor.account, ts, note)
        # 组内所有记录对均标记阻断，避免传递聚类换个形态再次出现
        for i, a in enumerate(sorted(members)):
            for b in sorted(members)[i + 1 :]:
                self.store.block_pair(
                    a, b, "人工驳回合并建议", actor.account, ts, candidate_id
                )
        self.store.set_status(members, ST_RECEIVED)
        for rid in members:
            self.store.add_event(
                EV_CANDIDATE_REJECTED,
                actor.account,
                ts,
                {
                    "candidate_id": candidate_id,
                    "members": sorted(members),
                    "note": note,
                },
                record_id=rid,
            )
        self.store.commit()
        return {"candidate_id": candidate_id, "status": CAND_REJECTED}

    # ------------------------------------------------------------ 误合并拆分

    def split_merge(self, actor: Actor, survivor_id: str,
                    release_ids: Sequence[str] | None = None,
                    note: str = "") -> dict:
        """拆分误合并。被拆出的记录恢复独立计时，原记录对阻断以免再次误并。"""
        actor.require_decision_role("拆分误合并")
        group = self.store.merged_group(survivor_id)
        if len(group) == 1:
            raise ConflictError(f"记录 {survivor_id} 不是任何合并组的主记录")
        members = sorted(r["id"] for r in group)
        duplicates = [r["id"] for r in group if r["merged_into"] == survivor_id]
        releases = sorted(release_ids) if release_ids else list(duplicates)
        unknown = [r for r in releases if r not in duplicates]
        if unknown:
            raise ValidationError(f"以下记录未并入 {survivor_id}：{unknown}")

        # 找到促成当前合并的候选并作废
        related = [
            c for c in self.store.list_candidates()
            if c["survivor_id"] == survivor_id
            and c["status"] == CAND_CONFIRMED
        ]
        ts = self.now()
        self.store.unmerge(releases)
        staying = [m for m in members if m not in releases]
        # 被拆出者与组内每一条留存记录之间都不再成案
        for a in releases:
            for b in staying:
                self.store.block_pair(
                    a, b, "拆分误合并", actor.account, ts,
                    related[0]["id"] if related else None,
                )
        for cand in related:
            self.store.supersede_candidate(cand["id"])
        for rid in members:
            self.store.add_event(
                EV_MERGE_SPLIT,
                actor.account,
                ts,
                {
                    "survivor_id": survivor_id,
                    "released": releases,
                    "staying_members": staying,
                    "superseded_candidates": [c["id"] for c in related],
                    "note": note,
                },
                record_id=rid,
            )
        self.store.commit()
        return {"survivor_id": survivor_id, "released": releases,
                "staying_members": staying}

    # ------------------------------------------------------------ 迟到签到

    def late_checkin(self, actor: Actor, record_id: str,
                     checkin_at: str) -> dict:
        """补登迟到签到时间；只追加证据事件，不改动已有时长。"""
        actor.require_decision_role("迟到签到登记")
        rec = self.store.get_record(record_id)
        if rec is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        if rec["status"] == ST_ARCHIVED:
            raise ConflictError("记录已归档，签到补登需走更正事件")
        validate_iso(checkin_at, "checkin_at")
        self.store.set_checkin(record_id, checkin_at)
        self.store.add_event(
            EV_LATE_CHECKIN,
            actor.account,
            self.now(),
            {
                "record_id": record_id,
                "checkin_at": checkin_at,
                "service_start": rec["service_start"],
                "minutes_late": _minutes_between(rec["service_start"], checkin_at),
            },
            record_id=record_id,
        )
        self.store.commit()
        return {"record_id": record_id, "checkin_at": checkin_at}

    # ------------------------------------------------------------ 人工核验

    def verify_record(self, actor: Actor, record_id: str,
                      note: str = "") -> dict:
        """无冲突记录经授权人员核验确认，进入可归档状态。"""
        actor.require_decision_role("记录核验")
        rec = self.store.get_record(record_id)
        if rec is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        if rec["status"] != ST_RECEIVED:
            raise ConflictError(f"状态为 {rec['status']}，无需核验或不能核验")
        if rec["merged_into"]:
            raise ConflictError("重复记录随主记录确认，不能单独核验")
        self.store.mark_survivor_confirmed(record_id)
        self.store.add_event(
            EV_RECORD_VERIFIED,
            actor.account,
            self.now(),
            {"record_id": record_id, "note": note},
            record_id=record_id,
        )
        self.store.commit()
        return {"record_id": record_id, "status": ST_CONFIRMED}

    # ------------------------------------------------------------ 场次取消

    def cancel_session(self, actor: Actor, session_code: str,
                       reason: str = "") -> dict:
        """取消场次：未归档记录置为取消并逐笔留痕；归档记录只能走更正。"""
        actor.require_decision_role("场次取消")
        if not session_code:
            raise ValidationError("session_code 不能为空")
        records = self.store.records_by_session(session_code)
        if not records:
            raise NotFoundError(f"场次下没有记录：{session_code}")
        ts = self.now()
        affected, skipped_archived = [], []
        for rec in records:
            if rec["status"] == ST_ARCHIVED:
                skipped_archived.append(rec["id"])
                continue
            if rec["status"] == ST_CANCELLED:
                continue
            affected.append(rec["id"])
        self.store.set_status(affected, ST_CANCELLED)
        for rid in affected:
            self.store.add_event(
                EV_SESSION_CANCELLED,
                actor.account,
                ts,
                {"session_code": session_code, "reason": reason},
                record_id=rid,
            )
        self.store.commit()
        return {
            "session_code": session_code,
            "cancelled": sorted(affected),
            "skipped_archived": sorted(skipped_archived),
        }

    # ---------------------------------------------------------------- 归档

    def archive(self, actor: Actor, record_ids: Sequence[str]) -> dict:
        """归档已确认的主记录；重复记录与未确认记录不得直接归档。"""
        actor.require_decision_role("归档")
        ts = self.now()
        archived, errors = [], []
        for rid in sorted(record_ids):
            rec = self.store.get_record(rid)
            if rec is None:
                errors.append({"record_id": rid, "error": "记录不存在"})
                continue
            if rec["status"] == ST_ARCHIVED:
                errors.append({"record_id": rid, "error": "已归档，无需重复操作"})
                continue
            if rec["merged_into"]:
                errors.append(
                    {"record_id": rid,
                     "error": f"重复记录随主记录 {rec['merged_into']} 归档"}
                )
                continue
            if rec["status"] != ST_CONFIRMED:
                errors.append(
                    {"record_id": rid, "error": f"状态为 {rec['status']}，不能归档"}
                )
                continue
            lineage = self.store.merged_group(rid)
            self.store.set_status([rid], ST_ARCHIVED)
            self.store.add_event(
                EV_RECORD_ARCHIVED,
                actor.account,
                ts,
                {
                    "record_id": rid,
                    "fingerprint": rec["fingerprint"],
                    "minutes_counted": rec["minutes"],
                    "merged_members": sorted(m["id"] for m in lineage
                                             if m["id"] != rid),
                    "source_fingerprints": sorted(m["fingerprint"] for m in lineage),
                },
                record_id=rid,
            )
            archived.append(rid)
        self.store.commit()
        return {"archived": archived, "errors": errors}

    # ------------------------------------------------------------ 归档更正

    def correct_archived(self, actor: Actor, record_id: str,
                         changes: dict[str, Any], reason: str) -> dict:
        """归档后唯一的调整路径：追加更正事件，记录前后值，不覆盖谱系。"""
        actor.require_decision_role("归档更正")
        rec = self.store.get_record(record_id)
        if rec is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        if rec["status"] != ST_ARCHIVED:
            raise ConflictError("只有已归档记录需要通过更正事件调整")
        if not reason:
            raise ValidationError("更正必须填写原因")
        illegal = sorted(set(changes) - CORRECTABLE_FIELDS)
        if illegal:
            raise ValidationError(f"字段不允许更正：{illegal}")
        if not changes:
            raise ValidationError("未提供任何更正内容")

        before_after: dict[str, dict[str, Any]] = {}
        for field, new_value in sorted(changes.items()):
            old_value = rec[field]
            if field == "minutes":
                if not isinstance(new_value, int) or new_value <= 0:
                    raise ValidationError("minutes 必须是正整数")
            elif field in ("service_start", "service_end"):
                validate_iso(str(new_value), field)
            elif field in ("volunteer_name", "id_tail"):
                if not isinstance(new_value, str) or not new_value.strip():
                    raise ValidationError(f"{field} 不能为空")
                new_value = new_value.strip()
            if old_value == new_value:
                continue
            before_after[field] = {"before": old_value, "after": new_value}

        if not before_after:
            raise ValidationError("更正内容与现值一致，无需更正")
        ts = self.now()
        for field, change in before_after.items():
            self.store.correct_field(record_id, field, change["after"])
        self.store.add_event(
            EV_RECORD_CORRECTED,
            actor.account,
            ts,
            {
                "record_id": record_id,
                "reason": reason,
                "changes": before_after,
            },
            record_id=record_id,
        )
        self.store.commit()
        return {"record_id": record_id, "corrected": sorted(before_after),
                "reason": reason}

    # ---------------------------------------------------------------- 谱系

    def lineage(self, record_id: str) -> dict:
        rec = self.store.get_record(record_id)
        if rec is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        events = self.store.list_events(record_id)
        chain = self.store.verify_chain()
        source = self.store.get_source(rec["fingerprint"])
        return {
            "record": {k: rec[k] for k in (
                "id", "fingerprint", "school_code", "submitter", "volunteer_name",
                "id_tail", "session_code", "service_start", "service_end",
                "minutes", "checkin_at", "batch_no", "transmit_seq", "status",
                "merged_into", "created_at")},
            "source": source,
            "events": [
                {
                    "seq": e["seq"],
                    "event_id": e["event_id"],
                    "type": e["event_type"],
                    "actor": e["actor"],
                    "occurred_at": e["occurred_at"],
                    "payload": e["payload"],
                }
                for e in events
            ],
            "chain_ok": chain["ok"],
        }


def _minutes_between(start_iso: str, end_iso: str) -> int:
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return max(0, int((end - start).total_seconds() // 60))
