"""HTTP 接口端到端回归测试（真实回环请求）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive.api import create_server


def entry():
    return {
        "姓名": "张伟",
        "证件尾号": "1234",
        "手机号": "13800008888",
        "学校": "一中",
        "场馆": "文博中心",
        "服务日期": "2026-09-10",
        "场次码": "A01",
        "开始时间": "09:00",
        "结束时间": "11:00",
    }


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = create_server("127.0.0.1", 0, ":memory:")
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self) -> None:
        status, _ = self.call("POST", "/api/batches",
                              {"source": "一中", "batch_id": "B-1", "entries": [entry()]})
        self.assertEqual(status, 201)
        second = entry()
        second["学校"] = "二中"
        self.call("POST", "/api/batches", {"source": "二中", "batch_id": "B-7", "entries": [second]})
        status, body = self.call("POST", "/api/dedupe/run", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["candidate_count"], 1)

        status, body = self.call("GET", "/api/suggestions")
        sug = body["suggestions"][0]
        self.assertEqual(sug["confidence"], "高")

        status, _ = self.call(
            "POST", f"/api/suggestions/{sug['suggestion_id']}/confirm",
            {"operator": "孙某", "role": "监护人"},
        )
        self.assertEqual(status, 403)

        status, body = self.call(
            "POST", f"/api/suggestions/{sug['suggestion_id']}/confirm",
            {"operator": "周运营", "role": "文博中心运营员"},
        )
        self.assertEqual(status, 201)
        gid = body["group_id"]

        status, body = self.call("GET", f"/api/groups/{gid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "已确认")
        self.assertEqual(body["minutes"], 120.0)

        status, _ = self.call(
            "POST", f"/api/groups/{gid}/archive",
            {"operator": "周运营", "role": "文博中心运营员"},
        )
        self.assertEqual(status, 201)
        status, body = self.call("GET", f"/api/groups/{gid}")
        self.assertEqual(body["state"], "已归档")

    def test_lineage_route(self) -> None:
        self.call("POST", "/api/batches",
                  {"source": "一中", "batch_id": "B-1", "entries": [entry()]})
        _, body = self.call("GET", "/api/groups")
        rid = body["groups"][0]["members"][0]["rid"]
        status, lineage = self.call("GET", f"/api/records/{rid}/lineage")
        self.assertEqual(status, 200)
        self.assertTrue(any(x["kind"] == "record_version" for x in lineage["lineage"]))


if __name__ == "__main__":
    unittest.main()
