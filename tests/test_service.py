"""端到端领域流程测试：重复提交、去重稳定、人工确认、谱系与归档更正。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive import (
    EventStore,
    FixedClock,
    InvalidState,
    NotFound,
    PermissionDenied,
    Principal,
    ServiceArchive,
)

OPERATOR = Principal(name="周运营", role="文博中心运营员")
VENUE = Principal(name="钱馆长", role="场馆负责人")
GUARDIAN = Principal(name="孙某", role="监护人")


def entry(name, tail, start="09:00", end="11:00", code="A01", school="一中", phone_tail="8888", **extra):
    data = {
        "姓名": name,
        "证件尾号": tail,
        "手机号": f"1380000{phone_tail}",
        "学校": school,
        "场馆": "文博中心",
        "服务日期": "2026-09-10",
        "场次码": code,
        "开始时间": start,
        "结束时间": end,
    }
    data.update(extra)
    return data


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ServiceArchive(EventStore(":memory:"), FixedClock("2026-09-12T08:00:00+00:00"))


class IngestTest(ServiceTestBase):
    def test_identical_retransmission_creates_no_new_versions(self) -> None:
        entries = [entry("张伟", "1234")]
        first = self.service.submit_batch("一中", "B-1", entries, submitted_at="2026-09-11T10:00:00+00:00")
        rid = first["records"][0]
        again = self.service.submit_batch(
            "一中", "B-1", [dict(e) for e in entries], submitted_at="2026-09-11T12:30:00+00:00"
        )
        self.assertTrue(again["retransmitted"])
        self.assertEqual(again["changed"], [])
        lineage = self.service.record_lineage(rid)
        versions = [x for x in lineage["lineage"] if x["kind"] == "record_version"]
        self.assertEqual(len(versions), 1)
        batch = self.service.batch_view("一中", "B-1")
        self.assertEqual(batch["transmission_count"], 2, "重传事实必须留在谱系里")

    def test_revised_retransmission_creates_version_chain(self) -> None:
        first = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        rid = first["records"][0]
        old_fp = self.service.record_lineage(rid)["current_fingerprint"]
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234", phone_tail="7777")])
        lineage = self.service.record_lineage(rid)
        self.assertNotEqual(lineage["current_fingerprint"], old_fp)
        versions = [x for x in lineage["lineage"] if x["kind"] == "record_version"]
        self.assertEqual(len(versions), 2)
        self.assertIsNotNone(versions[0]["superseded_by"])

    def test_different_schools_same_entry_position_are_distinct(self) -> None:
        a = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        b = self.service.submit_batch("二中", "B-1", [entry("张伟", "1234", school="二中")])
        self.assertNotEqual(a["records"][0], b["records"][0])


class DedupeTest(ServiceTestBase):
    def _seed_duplicate_case(self):
        a = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        b = self.service.submit_batch(
            "二中",
            "B-7",
            [entry("张 伟", "1234", school="二中")],
        )
        self.service.submit_batch("三中", "B-9", [entry("王芳", "2222", school="三中")])
        return a["records"][0], b["records"][0]

    def test_detects_same_person_same_session(self) -> None:
        rid_a, rid_b = self._seed_duplicate_case()
        result = self.service.run_dedupe()
        self.assertEqual(result["candidate_count"], 1)
        suggestions = self.service.list_suggestions()
        self.assertEqual(len(suggestions), 1)
        sug = suggestions[0]
        self.assertEqual(sug["confidence"], "高")
        self.assertEqual(set(sug["rids"]), {rid_a, rid_b})
        reasons = {r for edge in sug["edges"] for r in edge["identity_reasons"]}
        self.assertIn("证件尾号一致", reasons)

    def test_rerun_is_stable(self) -> None:
        self._seed_duplicate_case()
        r1 = self.service.run_dedupe()
        event_count_after_first = len(self.service.event_log())
        r2 = self.service.run_dedupe()
        self.assertEqual(r1["run_id"], r2["run_id"])
        self.assertEqual(r2["raised"], [])
        self.assertTrue(any(s["reason"] == "建议已存在" for s in r2["suppressed"]))
        self.assertEqual(len(self.service.event_log()), event_count_after_first, "重跑不产生新事件")

    def test_reopen_new_store_replays_identical_state(self) -> None:
        import tempfile

        tmpdir = tempfile.mkdtemp()
        db = Path(tmpdir) / "events.db"
        svc1 = ServiceArchive(EventStore(db), FixedClock("2026-09-12T08:00:00+00:00"))
        svc1.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        svc1.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        run1 = svc1.run_dedupe()
        svc2 = ServiceArchive(EventStore(db), FixedClock("2026-09-12T08:00:00+00:00"))
        run2 = svc2.run_dedupe()
        self.assertEqual(run1["run_id"], run2["run_id"])
        self.assertEqual(run2["raised"], [])

    def test_rejected_duplicate_is_not_resuggested_while_evidence_unchanged(self) -> None:
        self._seed_duplicate_case()
        self.service.run_dedupe()
        sug = self.service.list_suggestions()[0]
        self.service.reject_merge(sug["suggestion_id"], OPERATOR, reason="两人同名同尾号")
        result = self.service.run_dedupe()
        self.assertEqual(result["raised"], [])
        self.assertEqual(self.service.list_suggestions(), [])

    def test_changed_evidence_opens_new_generation(self) -> None:
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        sug_id = self.service.list_suggestions()[0]["suggestion_id"]
        # 修订：二中补传了不同的手机号尾号，报文指纹变化、证据刷新
        self.service.submit_batch(
            "二中", "B-7", [entry("张 伟", "1234", school="二中", phone_tail="7777")]
        )
        self.service.run_dedupe()
        detail = self.service.suggestion_detail(sug_id)
        generations = detail["generations"]
        self.assertEqual(len(generations), 2)
        self.assertEqual(generations[0]["status"], "obsoleted")
        self.assertEqual(generations[-1]["status"], "open")
        self.assertEqual(generations[-1]["generation"], 1)

    def test_different_session_same_day_not_candidate(self) -> None:
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234", start="09:00", end="10:00", code="")])
        self.service.submit_batch(
            "二中", "B-7",
            [entry("张 伟", "1234", school="二中", start="14:00", end="15:00", code="")],
        )
        result = self.service.run_dedupe()
        self.assertEqual(result["candidate_count"], 0)

    def test_non_decider_cannot_confirm(self) -> None:
        self._seed_duplicate_case()
        self.service.run_dedupe()
        sug_id = self.service.list_suggestions()[0]["suggestion_id"]
        with self.assertRaises(PermissionDenied):
            self.service.confirm_merge(sug_id, GUARDIAN)


class MergeSplitArchiveTest(ServiceTestBase):
    def _confirmed_group(self):
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        sug = self.service.list_suggestions()[0]
        merged = self.service.confirm_merge(sug["suggestion_id"], OPERATOR, note="确认同人同场")
        return sug["suggestion_id"], merged["group_id"], sug["rids"]

    def test_confirm_merge_counts_service_once(self) -> None:
        _, group_id, rids = self._confirmed_group()
        view = self.service.group_view(group_id)
        self.assertEqual(view["state"], "已确认")
        self.assertEqual(view["counted_record_count"], 2)
        self.assertEqual(view["minutes"], 120.0, "同一分组只计一场 120 分钟，而非两条相加")

    def test_split_after_archive_rejected_correction_required(self) -> None:
        _, group_id, rids = self._confirmed_group()
        rid_b = next(r for r in rids if r != rids[0])
        self.service.archive_group(group_id, OPERATOR)
        with self.assertRaises(InvalidState):
            self.service.split_group(group_id, [rid_b], OPERATOR)

    def test_split_before_archive_keeps_lineage(self) -> None:
        _, group_id, rids = self._confirmed_group()
        rid_b = next(r for r in rids if "二中" in self.service.record_lineage(r)["source"])
        result = self.service.split_group(group_id, [rid_b], OPERATOR, reason="误合并")
        parent = self.service.group_view(group_id)
        child = self.service.group_view(result["new_group_id"])
        self.assertEqual(parent["state"], "已确认")
        self.assertEqual(child["origin"], "split")
        self.assertEqual(child["parent_id"], group_id)
        self.assertEqual(len(child["members"]), 1)
        # 拆分后去重重跑不会重新撮合：已有人工拆分决定（证据未再变化）
        self.service.run_dedupe()
        self.assertEqual(self.service.list_suggestions(), [])

    def test_double_confirm_is_idempotent(self) -> None:
        sug_id, group_id, _ = self._confirmed_group()
        with self.assertRaises(NotFound):
            self.service.confirm_merge(sug_id, OPERATOR)

    def test_correction_is_only_post_archive_adjustment(self) -> None:
        _, group_id, rids = self._confirmed_group()
        rid_b = rids[1]
        self.service.archive_group(group_id, OPERATOR)
        # 归档后发现计时多算 30 分钟
        self.service.apply_correction(
            group_id, "minutes_delta", OPERATOR, reason="迟到实际只服务 90 分钟", delta_minutes=-30
        )
        view = self.service.group_view(group_id)
        self.assertEqual(view["minutes"], 90.0)
        self.assertEqual(len(view["corrections"]), 1)
        # 事后确认二中那条是另一个人：用更正剔除，而不是物理删除
        self.service.apply_correction(
            group_id, "exclude_record", OPERATOR, reason="同名同人判定错误", rid=rid_b
        )
        view = self.service.group_view(group_id)
        self.assertEqual(view["counted_record_count"], 1)
        self.assertIn(rid_b, view["excluded"])
        lineage = self.service.record_lineage(rid_b)
        kinds = [x["kind"] for x in lineage["lineage"]]
        self.assertIn("correction", kinds, "被剔除记录仍保留完整来源谱系")

    def test_archive_blocked_while_suggestion_open(self) -> None:
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        singleton = self.service.list_groups()[0]["group_id"]
        with self.assertRaises(InvalidState):
            self.service.archive_group(singleton, OPERATOR)

    def test_venue_manager_can_cancel_but_not_archive(self) -> None:
        _, group_id, _ = self._confirmed_group()
        with self.assertRaises(PermissionDenied):
            self.service.archive_group(group_id, VENUE)


class CheckinCancelTest(ServiceTestBase):
    def test_late_checkin_marked(self) -> None:
        result = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234", start="09:00")])
        rid = result["records"][0]
        ok = self.service.record_checkin(rid, at="2026-09-10T09:10:00")
        self.assertFalse(ok["late"])
        late = self.service.record_checkin(rid, at="2026-09-10T09:20:00")
        self.assertTrue(late["late"])
        lineage = self.service.record_lineage(rid)
        checkins = [x for x in lineage["lineage"] if x["kind"] == "checkin"]
        self.assertEqual(len(checkins), 2, "补传的迟到签到作为新事件追加，不覆盖原签到")

    def test_cancelled_session_blocks_checkin_and_excluded_from_dedupe(self) -> None:
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.cancel_session(
            "文博中心", "2026-09-10", VENUE, session_code="A01", reason="台风"
        )
        rid = self.service.submit_batch("三中", "B-9", [entry("李娜", "3333", school="三中")])["records"][0]
        with self.assertRaises(InvalidState):
            self.service.record_checkin(rid)
        run = self.service.run_dedupe()
        self.assertEqual(run["candidate_count"], 0, "取消场次不参与去重撮合")

    def test_guardian_cannot_cancel(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.cancel_session("文博中心", "2026-09-10", GUARDIAN)


class StateDerivationTest(ServiceTestBase):
    def test_states_follow_contract(self) -> None:
        result = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        rid = result["records"][0]
        gid = self.service.list_groups()[0]["group_id"]
        self.assertEqual(self.service.group_view(gid)["state"], "草拟")
        self.service.record_checkin(rid, at="2026-09-10T09:05:00")
        self.assertEqual(self.service.group_view(gid)["state"], "执行中")
        # 出现重复提交后进入待核验
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        self.assertEqual(self.service.group_view(gid)["state"], "待核验")


if __name__ == "__main__":
    unittest.main()
