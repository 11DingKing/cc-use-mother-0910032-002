"""HTTP 接口冒烟测试：鉴权、提交、去重、裁决、归档、更正全链路。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_records.httpapi import serve
from service_records.service import DedupService
from service_records.store import Store


def payload_base(**over) -> dict:
    data = {
        "school_code": "S1",
        "submitter": "wang",
        "volunteer_name": "王晓明",
        "id_tail": "1234",
        "session_code": "SESS-01",
        "session_name": "展厅引导",
        "service_start": "2026-09-01T09:00:00+08:00",
        "service_end": "2026-09-01T11:00:00+08:00",
        "minutes": 120,
        "batch_no": "B20260901",
    }
    data.update(over)
    return data


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = Store(":memory:")
        cls.store.init()
        cls.svc = DedupService(cls.store)
        cls.httpd = serve(cls.svc, "127.0.0.1", 0, dev_headers=True)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def request(self, method: str, path: str, body: dict | None = None,
                actor: tuple[str, str] | None = ("op_zhang", "operator")):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor-Account", actor[0])
            req.add_header("X-Actor-Role", actor[1])
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_no_auth(self) -> None:
        status, body = self.request("GET", "/v1/health", actor=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_token_forbidden(self) -> None:
        status, _ = self.request("POST", "/v1/submissions",
                                 payload_base(), actor=None)
        self.assertEqual(status, 403)

    def test_full_flow_over_http(self) -> None:
        s1, a = self.request("POST", "/v1/submissions", payload_base())
        self.assertEqual(s1, 201)
        s2, b = self.request(
            "POST", "/v1/submissions",
            payload_base(volunteer_name="王小明"),
        )
        self.assertEqual(s2, 201)
        # 完全相同的材料重传
        s3, again = self.request("POST", "/v1/submissions", payload_base())
        self.assertEqual(s3, 201)
        self.assertTrue(again["retransmitted"])

        s4, run = self.request("POST", "/v1/dedup-runs", {})
        self.assertEqual(s4, 200)
        self.assertEqual(run["proposed_new"], 1)

        s5, listed = self.request("GET", "/v1/candidates?status=proposed")
        self.assertEqual(s5, 200)
        cand = listed["candidates"][0]

        # 监护人无权确认
        s6, _ = self.request(
            "POST", f"/v1/candidates/{cand['id']}/confirm", {"note": "x"},
            actor=("g1", "guardian"),
        )
        self.assertEqual(s6, 403)

        s7, merged = self.request(
            "POST", f"/v1/candidates/{cand['id']}/confirm",
            {"note": "同一人"}, actor=("mgr_li", "venue_manager"),
        )
        self.assertEqual(s7, 200)
        survivor = merged["survivor_id"]

        s8, archived = self.request("POST", "/v1/archive",
                                    {"record_ids": [survivor]})
        self.assertEqual(s8, 200)
        self.assertEqual(archived["archived"], [survivor])

        s9, corrected = self.request(
            "POST", f"/v1/records/{survivor}/corrections",
            {"changes": {"minutes": 90}, "reason": "核实签到表"},
        )
        self.assertEqual(s9, 200)
        self.assertEqual(corrected["corrected"], ["minutes"])

        s10, lineage = self.request("GET", f"/v1/records/{survivor}/lineage")
        self.assertEqual(s10, 200)
        types = [e["type"] for e in lineage["events"]]
        self.assertIn("record.corrected", types)
        self.assertIn("candidate.confirmed", types)
        self.assertTrue(lineage["chain_ok"])

    def test_validation_error_is_400(self) -> None:
        bad = payload_base(minutes=-1)
        status, body = self.request("POST", "/v1/submissions", bad)
        self.assertEqual(status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
