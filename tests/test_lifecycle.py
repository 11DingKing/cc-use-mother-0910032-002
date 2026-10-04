"""取消、拆分边界与更正通道的补充测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive import EventStore, FixedClock, Principal, ServiceArchive

OPERATOR = Principal(name="周运营", role="文博中心运营员")
VENUE = Principal(name="钱馆长", role="场馆负责人")


def entry(name, tail, school="一中", **extra):
    data = {
        "姓名": name,
        "证件尾号": tail,
        "手机号": "13800008888",
        "学校": school,
        "场馆": "文博中心",
        "服务日期": "2026-09-10",
        "场次码": "A01",
        "开始时间": "09:00",
        "结束时间": "11:00",
    }
    data.update(extra)
    return data


class CancellationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ServiceArchive(EventStore(":memory:"), FixedClock("2026-09-12T08:00:00+00:00"))

    def test_cancel_obsoletes_open_suggestion_and_unblocks_archive(self) -> None:
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        self.assertEqual(len(self.service.list_suggestions()), 1)
        self.service.cancel_session("文博中心", "2026-09-10", VENUE, session_code="A01")
        self.assertEqual(self.service.list_suggestions(), [])
        groups = self.service.list_groups()
        for group in groups:
            self.assertEqual(group["state"], "已取消")
            # 建议已作废，归档不再被阻塞
            self.service.archive_group(group["group_id"], OPERATOR)
        self.assertTrue(all(g["archived"] for g in self.service.list_groups()))

    def test_cancel_is_idempotent(self) -> None:
        self.service.cancel_session("文博中心", "2026-09-10", VENUE)
        self.service.cancel_session("文博中心", "2026-09-10", VENUE)
        events = [e for e in self.service.event_log() if e["type"] == "session_cancelled"]
        self.assertEqual(len(events), 1)


class SplitBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ServiceArchive(EventStore(":memory:"), FixedClock("2026-09-12T08:00:00+00:00"))

    def _merged(self):
        self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        self.service.run_dedupe()
        sug = self.service.list_suggestions()[0]
        merged = self.service.confirm_merge(sug["suggestion_id"], OPERATOR)
        return merged["group_id"], sug["rids"]

    def test_split_then_evidence_change_allows_new_suggestion(self) -> None:
        group_id, rids = self._merged()
        rid_b = next(r for r in rids if self.service.record_lineage(r)["source"] == "二中")
        self.service.split_group(group_id, [rid_b], OPERATOR, reason="误判")
        self.service.run_dedupe()
        self.assertEqual(self.service.list_suggestions(), [])
        # 二中修订证件尾号，报文指纹变化，解除拆分决定的压制，可重新提请
        self.service.submit_batch("二中", "B-7", [entry("张 伟", "1299", school="二中")])
        self.service.run_dedupe()
        self.assertEqual(len(self.service.list_suggestions()), 1)


class RetransmissionFoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ServiceArchive(EventStore(":memory:"), FixedClock("2026-09-12T08:00:00+00:00"))

    def test_same_moment_retry_folds_to_one_transmission(self) -> None:
        payload = [entry("张伟", "1234")]
        self.service.submit_batch("一中", "B-1", payload, submitted_at="2026-09-11T10:00:00+00:00")
        self.service.submit_batch("一中", "B-1", [dict(e) for e in payload],
                                  submitted_at="2026-09-11T10:00:00+00:00")
        view = self.service.batch_view("一中", "B-1")
        self.assertEqual(view["transmission_count"], 1)

    def test_distinct_deliveries_both_recorded(self) -> None:
        payload = [entry("张伟", "1234")]
        self.service.submit_batch("一中", "B-1", payload, submitted_at="2026-09-11T10:00:00+00:00")
        self.service.submit_batch("一中", "B-1", [dict(e) for e in payload],
                                  submitted_at="2026-09-11T11:00:00+00:00")
        view = self.service.batch_view("一中", "B-1")
        self.assertEqual(view["transmission_count"], 2)
        self.assertEqual(view["transmissions"][1]["kind"], "retransmitted")


class AttachCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ServiceArchive(EventStore(":memory:"), FixedClock("2026-09-12T08:00:00+00:00"))

    def test_attach_orphan_record_post_archive(self) -> None:
        res = self.service.submit_batch("一中", "B-1", [entry("张伟", "1234")])
        rid = res["records"][0]
        gid = self.service.list_groups()[0]["group_id"]
        self.service.archive_group(gid, OPERATOR)
        # 另有一条同场记录，以更正事件补挂到已归档组
        other = self.service.submit_batch("二中", "B-7", [entry("张 伟", "1234", school="二中")])
        other_rid = other["records"][0]
        self.service.apply_correction(
            gid, "attach_record", OPERATOR, reason="漏报补挂", rid=other_rid
        )
        view = self.service.group_view(gid)
        self.assertIn(other_rid, view["attached"])
        # 被吸收记录的旧单例组不再为它计时
        singleton = next(g for g in self.service.list_groups() if g["group_id"] != gid)
        self.assertEqual(singleton["counted_record_count"], 0)
        # 归档组的更正谱系可追溯
        self.assertEqual(len(view["corrections"]), 1)
        self.assertIn("correction", [x["kind"] for x in self.service.record_lineage(other_rid)["lineage"]])


if __name__ == "__main__":
    unittest.main()
