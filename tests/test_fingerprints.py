"""线索规范化与匹配规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive.fingerprints import (
    IdentityClues,
    SessionEvidence,
    compare_identity,
    confidence_label,
    extract_clues,
    extract_session,
    norm_text,
    record_id,
    same_session,
    source_fingerprint,
    tail4,
)


class NormalizeTest(unittest.TestCase):
    def test_full_width_and_spaces(self) -> None:
        self.assertEqual(norm_text("张　伟 "), "张伟")
        self.assertEqual(norm_text("Ａ０１"), "A01")
        self.assertEqual(tail4("身份证 110101201001011234"), "1234")

    def test_aliases(self) -> None:
        clues = extract_clues({"姓名": "李 明", "证件尾号": "5678", "学校": "第三中学"})
        self.assertEqual(clues.name, "李明")
        self.assertEqual(clues.id_tail, "5678")
        clues2 = extract_clues({"volunteer_name": "李明", "id_number": "x9999"})
        self.assertEqual(clues2.id_tail, "9999")

    def test_stable_record_id_and_fingerprint(self) -> None:
        rid1 = record_id("一中", "B-09", 3)
        rid2 = record_id("一中", "B-09", 3)
        self.assertEqual(rid1, rid2)
        self.assertTrue(rid1.startswith("rid_"))
        fp1 = source_fingerprint({"姓名": "张伟", "x": 1})
        fp2 = source_fingerprint({"x": 1, "姓名": "张伟"})
        self.assertEqual(fp1, fp2, "键顺序不影响指纹")


class MatchTest(unittest.TestCase):
    def test_identity_scores(self) -> None:
        a = IdentityClues("张伟", "1234", "8888", "一中")
        b = IdentityClues("张伟", "1234", "9999", "二中")
        c = IdentityClues("张伟", "0000", "", "三中")
        match = compare_identity(a, b)
        self.assertIsNotNone(match)
        self.assertEqual(match.score, 4)  # 姓名 + 证件尾号
        weak = compare_identity(a, c)
        self.assertEqual(weak.score, 1)
        self.assertIsNone(compare_identity(a, IdentityClues("王芳", "0000", "", "")))

    def test_same_session_by_code_or_overlap(self) -> None:
        s1 = SessionEvidence("文博中心", "2026-09-10", "A01", 9 * 60, 11 * 60)
        s2 = SessionEvidence("文博中心", "2026-09-10", "a01", 9 * 60 + 30, 12 * 60)
        same, reason = same_session(s1, s2)
        self.assertTrue(same)
        self.assertEqual(reason, "同场次编码")

        s3 = SessionEvidence("文博中心", "2026-09-10", "", 10 * 60, 12 * 60)
        s4 = SessionEvidence("文博中心", "2026-09-10", "", 8 * 60, 10 * 60 + 30)
        self.assertTrue(same_session(s3, s4)[0])
        s5 = SessionEvidence("图书馆", "2026-09-10", "", 10 * 60, 12 * 60)
        self.assertFalse(same_session(s3, s5)[0])

    def test_confidence(self) -> None:
        self.assertEqual(confidence_label(4, True), "高")
        self.assertEqual(confidence_label(1, True), "中")
        self.assertEqual(confidence_label(4, False), "低")

    def test_extract_session_cross_midnight(self) -> None:
        ev = extract_session({"场馆": "馆", "服务日期": "2026-09-10", "开始时间": "22:00", "结束时间": "01:00"})
        self.assertEqual(ev.end_min - ev.start_min, 180)


if __name__ == "__main__":
    unittest.main()
