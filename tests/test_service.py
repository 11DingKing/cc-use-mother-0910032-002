"""服务端用例测试：收件、去重、裁决、拆分、签到、取消、归档、更正、重跑稳定性。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_records.auth import ROLE_GUARDIAN, ROLE_OPERATOR, ROLE_VENUE_MANAGER, Actor, AuthzError
from service_records.models import Submission
from service_records.service import (
    ConflictError,
    DedupService,
    NotFoundError,
    ValidationError,
)
from service_records.store import Store

OP = Actor("op_zhang", ROLE_OPERATOR, "张运营")
MANAGER = Actor("mgr_li", ROLE_VENUE_MANAGER, "李馆长")
GUARDIAN = Actor("g_1", ROLE_GUARDIAN)


def sub(name: str, tail: str, *, school="S1", submitter="wang",
        session="SESS-01", start="2026-09-01T09:00:00+08:00",
        end="2026-09-01T11:00:00+08:00", minutes=120, batch="B20260901",
        checkin_at=None, payload=None, transmitted_at=None) -> Submission:
    return Submission(
        school_code=school, submitter=submitter, volunteer_name=name, id_tail=tail,
        session_code=session, session_name="展厅引导", service_start=start,
        service_end=end, minutes=minutes, batch_no=batch, checkin_at=checkin_at,
        payload=payload or {}, transmitted_at=transmitted_at,
    )


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.init()
        self.svc = DedupService(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def receive(self, s: Submission, actor=OP) -> dict:
        return self.svc.receive(actor, s)


class ReceiveTests(ServiceTestBase):
    def test_fingerprint_stable_for_same_source(self) -> None:
        fp1 = DedupService.fingerprint_of(sub("王晓明", "1234"))
        fp2 = DedupService.fingerprint_of(sub("王晓明", "1234"))
        self.assertEqual(fp1, fp2)

    def test_different_identity_clue_is_different_source(self) -> None:
        # 同一场次同一人，但姓名线索不同 → 不同来源指纹，需要去重流程处理
        a = self.receive(sub("王晓明", "1234"))
        b = self.receive(sub("王小明", "1234"))
        self.assertNotEqual(a["fingerprint"], b["fingerprint"])
        self.assertFalse(a["retransmitted"])
        self.assertFalse(b["retransmitted"])

    def test_batch_retransmit_is_idempotent(self) -> None:
        first = self.receive(sub("王晓明", "1234"))
        again = self.receive(
            sub("王晓明", "1234", transmitted_at="2026-09-05T10:00:00+08:00")
        )
        self.assertTrue(again["retransmitted"])
        self.assertEqual(first["record_id"], again["record_id"])
        self.assertEqual(again["transmit_seq"], 2)
        records = self.store.list_records()
        self.assertEqual(len(records), 1, "重传不得产生第二条记录")
        events = self.store.list_events(first["record_id"])
        kinds = [e["event_type"] for e in events]
        self.assertEqual(kinds.count("batch.retransmit"), 1)
        self.assertEqual(events[-1]["payload"]["transmit_seq"], 2)

    def test_validation_rejects_bad_input(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.receive(OP, sub("", "1234"))
        with self.assertRaises(ValidationError):
            self.svc.receive(
                OP, sub("王晓明", "1", start="2026-09-01T11:00:00+08:00",
                        end="2026-09-01T09:00:00+08:00")
            )
        with self.assertRaises(ValidationError):
            self.svc.receive(OP, sub("王晓明", "1", minutes=0))


class DedupTests(ServiceTestBase):
    def seed_conflict_trio(self) -> tuple[str, str, str]:
        a = self.receive(sub("王晓明", "1234"))["record_id"]
        b = self.receive(sub("王小明", "1234"))["record_id"]   # 名字不同尾号同
        c = self.receive(sub("王晓明", "9234"))["record_id"]   # 尾号不同名字同
        return a, b, c

    def test_proposals_group_same_volunteer_same_session(self) -> None:
        a, b, c = self.seed_conflict_trio()
        result = self.svc.run_dedup(OP)
        self.assertEqual(result["proposed_new"], 1)
        proposals = self.svc.list_proposals()
        self.assertEqual(len(proposals), 1)
        p = proposals[0]
        self.assertEqual(set(p["members"]), {a, b, c})
        self.assertIn(p["survivor_id"], {a, b, c})
        self.assertEqual(len(p["members"]), 3)
        statuses = {r["id"]: r["status"] for r in self.store.list_records()}
        self.assertTrue(all(s == "pending" for s in statuses.values()))

    def test_different_sessions_are_not_duplicates(self) -> None:
        self.receive(sub("王晓明", "1234", session="SESS-01"))
        self.receive(sub("王晓明", "1234", session="SESS-02"))
        result = self.svc.run_dedup(OP)
        self.assertEqual(result["proposed_new"], 0)

    def test_tail_suffix_overlap_and_fuzzy_name(self) -> None:
        # 尾号后缀相容 + 姓名完全一致 → 成案
        self.receive(sub("王晓明", "X1234"))
        self.receive(sub("王晓明", "1234"))
        result = self.svc.run_dedup(OP)
        self.assertEqual(result["proposed_new"], 1)

    def test_weak_clues_alone_do_not_propose(self) -> None:
        # 仅模糊姓名、尾号不一致：不成案
        self.receive(sub("王晓明", "1111"))
        self.receive(sub("王小小", "2222"))
        result = self.svc.run_dedup(OP)
        self.assertEqual(result["proposed_new"], 0)

    def test_rerun_is_stable(self) -> None:
        self.seed_conflict_trio()
        r1 = self.svc.run_dedup(OP)
        p1 = self.svc.list_proposals()
        r2 = self.svc.run_dedup(OP)
        r3 = self.svc.run_dedup(MANAGER)
        self.assertEqual(r1["proposed_new"], 1)
        self.assertEqual(r2["proposed_new"], 0)
        self.assertEqual(r2["skipped_existing"], 1)
        self.assertEqual(r3["proposed_new"], 0)
        self.assertEqual(r1["input_sig"], r2["input_sig"])
        p2 = self.svc.list_proposals()
        self.assertEqual([p["id"] for p in p1], [p["id"] for p in p2])
        # 同一输入签名，运行记录逐条可追溯
        sigs = [r["input_sig"] for r in self.store.list_runs()]
        self.assertEqual(sigs, [sigs[0]] * 3)

    def test_survivor_prefers_checkin_evidence(self) -> None:
        a = self.receive(sub("王晓明", "1234"))["record_id"]
        b = self.receive(
            sub("王小明", "1234", checkin_at="2026-09-01T09:05:00+08:00")
        )["record_id"]
        self.svc.run_dedup(OP)
        p = self.svc.list_proposals()[0]
        self.assertEqual(p["survivor_id"], b, "有签到证据者应为主记录")


class DecisionTests(ServiceTestBase):
    def prepare_candidate(self):
        a = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        b = self.svc.receive(OP, sub("王小明", "1234"))["record_id"]
        self.svc.run_dedup(OP)
        cand = self.svc.list_proposals()[0]
        return a, b, cand

    def test_confirm_merge_counts_time_once(self) -> None:
        a, b, cand = self.prepare_candidate()
        result = self.svc.confirm_merge(MANAGER, cand["id"], note="确认为同一人")
        survivor = result["survivor_id"]
        dup = result["duplicates"][0]
        rec_s = self.store.get_record_required(survivor)
        rec_d = self.store.get_record_required(dup)
        self.assertEqual(rec_s["status"], "confirmed")
        self.assertEqual(rec_d["merged_into"], survivor)
        self.assertEqual(rec_d["status"], "confirmed")
        # 全库可计时记录只有主记录一笔
        timed = [r for r in self.store.list_records() if r["merged_into"] is None]
        self.assertEqual(len(timed), 1)
        self.assertEqual(timed[0]["minutes"], 120)

    def test_guardian_cannot_decide(self) -> None:
        _, _, cand = self.prepare_candidate()
        with self.assertRaises(AuthzError):
            self.svc.confirm_merge(GUARDIAN, cand["id"])

    def test_double_decision_conflicts(self) -> None:
        _, _, cand = self.prepare_candidate()
        self.svc.confirm_merge(OP, cand["id"])
        with self.assertRaises(ConflictError):
            self.svc.reject_candidate(OP, cand["id"])

    def test_reject_blocks_pair_and_rerun_stays_clean(self) -> None:
        a, b, cand = self.prepare_candidate()
        self.svc.reject_candidate(OP, cand["id"], note="确为两名志愿者")
        self.assertTrue(self.store.is_blocked(a, b))
        # 记录回到 received，重跑不再成案
        self.assertEqual(self.store.get_record_required(a)["status"], "received")
        again = self.svc.run_dedup(OP)
        self.assertEqual(again["proposed_new"], 0)
        self.assertEqual(self.svc.list_proposals(), [])
        # 第三条记录与 a 姓名、尾号双一致（仅提交人不同，属另一来源）：
        # c 仍可与 a 成案，但阻断的 a-b 不会经 c 传递复合
        c = self.svc.receive(OP, sub("王晓明", "1234", submitter="zhao"))[
            "record_id"
        ]
        self.svc.run_dedup(OP)
        proposed = self.svc.list_proposals()
        self.assertEqual(len(proposed), 1, "阻断对 a-b 不得经传递再次成案")
        self.assertEqual(set(proposed[0]["members"]), {a, c})
        self.assertNotIn(b, proposed[0]["members"])

    def test_reject_then_new_evidence_still_works_for_other_pairs(self) -> None:
        # 驳回 a-b 后，新到的 d 与 b 构成新对，仍应正常成案
        a = self.svc.receive(OP, sub("王晓明", "1111"))["record_id"]
        b = self.svc.receive(OP, sub("陈晓明", "1111"))["record_id"]
        self.svc.run_dedup(OP)
        cand = self.svc.list_proposals()[0]
        self.svc.reject_candidate(OP, cand["id"])
        d = self.svc.receive(OP, sub("陈晓明", "1111", school="S2"))["record_id"]
        self.svc.run_dedup(OP)
        props = self.svc.list_proposals()
        self.assertEqual(len(props), 1)
        self.assertEqual(set(props[0]["members"]), {b, d})


class SplitTests(ServiceTestBase):
    def test_split_releases_record_and_keeps_lineage(self) -> None:
        a = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        b = self.svc.receive(OP, sub("王小明", "1234"))["record_id"]
        self.svc.run_dedup(OP)
        cand_id = self.svc.list_proposals()[0]["id"]
        merged = self.svc.confirm_merge(OP, cand_id)
        survivor, dup = merged["survivor_id"], merged["duplicates"][0]

        split = self.svc.split_merge(MANAGER, survivor, note="发现并非同一人")
        self.assertEqual(split["released"], [dup])
        self.assertIsNone(self.store.get_record_required(dup)["merged_into"])
        self.assertEqual(self.store.get_record_required(dup)["status"], "received")
        self.assertTrue(self.store.is_blocked(survivor, dup))
        # 原候选作废
        self.assertEqual(self.store.get_candidate(cand_id)["status"], "superseded")
        # 拆分事件对组内两条记录都留痕
        for rid in (survivor, dup):
            types = [e["event_type"] for e in self.store.list_events(rid)]
            self.assertIn("merge.split", types)
            self.assertIn("candidate.confirmed", types)
        # 重跑稳定：不再成案
        self.assertEqual(self.svc.run_dedup(OP)["proposed_new"], 0)


class CheckinCancelArchiveTests(ServiceTestBase):
    def confirmed_record(self) -> str:
        rid = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        return rid

    def test_late_checkin_records_minutes(self) -> None:
        rid = self.confirmed_record()
        out = self.svc.late_checkin(
            OP, rid, "2026-09-01T09:20:00+08:00"
        )
        self.assertEqual(out["checkin_at"], "2026-09-01T09:20:00+08:00")
        ev = self.store.list_events(rid)[-1]
        self.assertEqual(ev["event_type"], "record.late_checkin")
        self.assertEqual(ev["payload"]["minutes_late"], 20)

    def test_cancel_session_skips_archived(self) -> None:
        rid = self.confirmed_record()
        self.svc.verify_record(OP, rid)
        self.svc.archive(OP, [rid])
        other = self.svc.receive(OP, sub("李四", "5555"))["record_id"]
        # 归档记录不受场次取消影响，仅取消未归档记录
        out = self.svc.cancel_session(OP, "SESS-01", reason="台风")
        self.assertEqual(out["cancelled"], [other])
        self.assertEqual(out["skipped_archived"], [rid])
        self.assertEqual(self.store.get_record_required(rid)["status"], "archived")
        self.assertEqual(self.store.get_record_required(other)["status"], "cancelled")
        # 已取消的记录再次取消是幂等无操作
        again = self.svc.cancel_session(OP, "SESS-01")
        self.assertEqual(again["cancelled"], [])

    def test_cancel_unknown_session(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.cancel_session(OP, "NOPE")

    def _archive_ready(self) -> str:
        rid = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        self.svc.verify_record(OP, rid)
        return rid

    def test_archive_flow_and_immutability(self) -> None:
        rid = self._archive_ready()
        out = self.svc.archive(OP, [rid])
        self.assertEqual(out["archived"], [rid])
        # 归档后常规修改路径关闭：迟到签到被拒
        with self.assertRaises(ConflictError):
            self.svc.late_checkin(OP, rid, "2026-09-01T09:30:00+08:00")
        # 场次取消跳过归档记录
        other = self.svc.receive(OP, sub("李四", "5555"))["record_id"]
        self.svc.cancel_session(OP, "SESS-01")
        self.assertEqual(self.store.get_record_required(rid)["status"], "archived")
        self.assertEqual(self.store.get_record_required(other)["status"], "cancelled")

    def test_duplicate_record_cannot_archive_directly(self) -> None:
        a = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        b = self.svc.receive(OP, sub("王小明", "1234"))["record_id"]
        self.svc.run_dedup(OP)
        cand = self.svc.list_proposals()[0]
        merged = self.svc.confirm_merge(OP, cand["id"])
        out = self.svc.archive(OP, [merged["survivor_id"], merged["duplicates"][0]])
        self.assertEqual(out["archived"], [merged["survivor_id"]])
        self.assertTrue(out["errors"])

    def test_correction_is_the_only_post_archive_change(self) -> None:
        rid = self._archive_ready()
        self.svc.archive(MANAGER, [rid])
        with self.assertRaises(ValidationError):
            self.svc.correct_archived(OP, rid, {"minutes": 100}, reason="")
        with self.assertRaises(ValidationError):
            self.svc.correct_archived(OP, rid, {"status": "received"}, "改错字段")
        out = self.svc.correct_archived(
            MANAGER, rid, {"minutes": 100, "volunteer_name": "王晓明 "},
            reason="签到表核实实际时长",
        )
        self.assertEqual(out["corrected"], ["minutes"])
        rec = self.store.get_record_required(rid)
        self.assertEqual(rec["minutes"], 100)
        ev = self.store.list_events(rid)[-1]
        self.assertEqual(ev["event_type"], "record.corrected")
        self.assertEqual(ev["payload"]["changes"]["minutes"]["before"], 120)
        self.assertEqual(ev["payload"]["changes"]["minutes"]["after"], 100)
        # 无变化的更正被拒绝
        with self.assertRaises(ValidationError):
            self.svc.correct_archived(MANAGER, rid, {"minutes": 100}, reason="重复")
        # 非归档记录不能走更正
        fresh = self.svc.receive(OP, sub("赵六", "7777"))["record_id"]
        with self.assertRaises(ConflictError):
            self.svc.correct_archived(MANAGER, fresh, {"minutes": 60}, reason="x")


class LineageChainTests(ServiceTestBase):
    def test_full_lineage_and_chain_integrity(self) -> None:
        rid = self.svc.receive(
            OP, sub("王晓明", "1234", payload={"form_no": "F-001"})
        )["record_id"]
        self.svc.receive(  # 重传
            OP, sub("王晓明", "1234", payload={"form_no": "F-001"},
                    transmitted_at="2026-09-05T10:00:00+08:00")
        )
        self.svc.late_checkin(OP, rid, "2026-09-01T09:10:00+08:00")
        lin = self.svc.lineage(rid)
        self.assertTrue(lin["chain_ok"])
        self.assertEqual(lin["source"]["transmit_count"], 2)
        self.assertEqual([e["type"] for e in lin["events"]],
                         ["record.received", "batch.retransmit",
                          "record.late_checkin"])
        self.assertTrue(self.store.verify_chain()["ok"])

    def test_tamper_breaks_chain(self) -> None:
        rid = self.svc.receive(OP, sub("王晓明", "1234"))["record_id"]
        self.svc.run_dedup(OP)
        self.store._conn.execute(  # noqa: SLF001 - 模拟外部篡改
            "UPDATE events SET actor = 'fake' WHERE record_id = ?", (rid,)
        )
        result = self.store.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 1)


if __name__ == "__main__":
    unittest.main()
