"""管理命令端到端冒烟：导入 → 重跑去重（两次，结果稳定）→ 谱系 → 链校验。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_submissions(path: Path) -> None:
    rows = [
        {
            "school_code": "S1", "submitter": "wang", "volunteer_name": "王晓明",
            "id_tail": "1234", "session_code": "SESS-01",
            "session_name": "展厅引导",
            "service_start": "2026-09-01T09:00:00+08:00",
            "service_end": "2026-09-01T11:00:00+08:00",
            "minutes": 120, "batch_no": "B01",
        },
        {
            "school_code": "S2", "submitter": "chen", "volunteer_name": "王小明",
            "id_tail": "1234", "session_code": "SESS-01",
            "session_name": "展厅引导",
            "service_start": "2026-09-01T09:00:00+08:00",
            "service_end": "2026-09-01T11:00:00+08:00",
            "minutes": 120, "batch_no": "B02",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


class CliSmokeTest(unittest.TestCase):
    def run_cli(self, db: Path, *args: str) -> dict:
        env_cmd = [
            sys.executable, "-m", "service_records.manage", "--db", str(db), *args
        ]
        proc = subprocess.run(
            env_cmd, cwd=ROOT, capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
            check=True,
        )
        return json.loads(proc.stdout)

    def test_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            data = Path(tmp) / "subs.jsonl"
            write_submissions(data)

            self.run_cli(db, "init-db")
            imported = self.run_cli(
                db, "import", "--file", str(data), "--actor", "op_zhang"
            )
            self.assertEqual(imported["accepted"], 2)

            # 同文件再次导入：指纹一致，全部识别为重传
            again = self.run_cli(
                db, "import", "--file", str(data), "--actor", "op_zhang"
            )
            self.assertEqual(again["retransmitted"], 2)
            self.assertEqual(again["accepted"], 0)

            r1 = self.run_cli(db, "dedup", "--actor", "op_zhang")
            r2 = self.run_cli(db, "dedup", "--actor", "op_zhang")
            self.assertEqual(r1["proposed_new"], 1)
            self.assertEqual(r2["proposed_new"], 0)
            self.assertEqual(r1["input_sig"], r2["input_sig"])

            candidates = self.run_cli(db, "list", "candidates")["candidates"]
            self.assertEqual(len(candidates), 1)

            chain = self.run_cli(db, "verify-chain")
            self.assertTrue(chain["ok"])
            self.assertGreaterEqual(chain["length"], 4)

            records = self.run_cli(db, "list", "records")["records"]
            lineage = self.run_cli(
                db, "lineage", "--record", records[0]["id"]
            )
            self.assertIn("record.received",
                          [e["type"] for e in lineage["events"]])


if __name__ == "__main__":
    unittest.main()
