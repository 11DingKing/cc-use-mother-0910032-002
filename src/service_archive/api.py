"""HTTP 接口（纯标准库实现）。

路由：

- ``POST /api/batches``                       学校提交/重传补录批次
- ``POST /api/dedupe/run``                    重跑去重（管理命令亦可调用）
- ``GET  /api/suggestions``                   待处理合并建议
- ``GET  /api/suggestions/<id>``              建议各代详情
- ``POST /api/suggestions/<id>/confirm``      授权确认合并
- ``POST /api/suggestions/<id>/reject``       授权拒绝合并
- ``POST /api/groups/<id>/split``             拆分误合并
- ``POST /api/records/<rid>/checkin``         签到（迟到自动标记）
- ``POST /api/sessions/cancel``               场次取消
- ``POST /api/groups/<id>/archive``           归档
- ``POST /api/groups/<id>/corrections``       归档后更正
- ``GET  /api/groups``、``/api/groups/<id>``  分组视图
- ``GET  /api/records/<rid>/lineage``         来源谱系
- ``GET  /api/batches/<source>/<batch_id>``   批次传输谱系
- ``GET  /api/events``                        原始事件账本
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .auth import Principal
from .errors import DomainError, InvalidState, NotFound, PermissionDenied
from .service import ServiceArchive
from .store import EventStore

DEFAULT_DB = os.environ.get("SERVICE_ARCHIVE_DB", "service_archive.db")


def build_service(db_path: str | None = None) -> ServiceArchive:
    return ServiceArchive(EventStore(db_path or DEFAULT_DB))


def _principal(body: dict) -> Principal:
    return Principal(name=body.get("operator", "匿名"), role=body.get("role", ""))


class ApiHandler(BaseHTTPRequestHandler):
    service: ServiceArchive  # 由工厂注入类属性

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    # ---- 基础收发 ----

    def _send(self, status: int, value) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _handle(self, fn, success: int = 200) -> None:
        try:
            self._send(success, fn())
        except PermissionDenied as exc:
            self._send(403, {"error": "permission_denied", "message": str(exc)})
        except NotFound as exc:
            self._send(404, {"error": "not_found", "message": str(exc)})
        except InvalidState as exc:
            self._send(409, {"error": "invalid_state", "message": str(exc)})
        except DomainError as exc:
            self._send(422, {"error": "domain_conflict", "message": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = [unquote(p) for p in path.split("/") if p]

        if path == "/api/suggestions":
            self._handle(lambda: {"suggestions": self.service.list_suggestions()})
        elif path == "/api/groups":
            self._handle(lambda: {"groups": self.service.list_groups()})
        elif path == "/api/events":
            self._handle(lambda: {"events": self.service.event_log()})
        elif len(parts) == 3 and parts[:2] == ["api", "suggestions"]:
            self._handle(lambda: self.service.suggestion_detail(parts[2]))
        elif len(parts) == 3 and parts[:2] == ["api", "groups"]:
            self._handle(lambda: self.service.group_view(parts[2]))
        elif len(parts) == 4 and parts[:2] == ["api", "records"] and parts[3] == "lineage":
            self._handle(lambda: self.service.record_lineage(parts[2]))
        elif len(parts) == 4 and parts[:2] == ["api", "batches"]:
            self._handle(lambda: self.service.batch_view(parts[2], parts[3]))
        else:
            self._send(404, {"error": "not_found", "message": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        parts = [unquote(p) for p in path.split("/") if p]

        if path == "/api/batches":
            body = self._body()
            self._handle(
                lambda: self.service.submit_batch(
                    source=body["source"],
                    batch_id=body["batch_id"],
                    entries=body["entries"],
                    submitted_at=body.get("submitted_at"),
                ),
                201,
            )
        elif path == "/api/dedupe/run":
            body = self._body()
            self._handle(lambda: self.service.run_dedupe(body.get("run_id")))
        elif path == "/api/sessions/cancel":
            body = self._body()
            self._handle(
                lambda: self.service.cancel_session(
                    site=body["site"],
                    service_date=body["service_date"],
                    principal=_principal(body),
                    session_code=body.get("session_code", ""),
                    reason=body.get("reason", ""),
                ),
                201,
            )
        elif len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "confirm":
            body = self._body()
            self._handle(
                lambda: self.service.confirm_merge(
                    parts[2],
                    _principal(body),
                    note=body.get("note", ""),
                    canonical_rid=body.get("canonical_rid"),
                ),
                201,
            )
        elif len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "reject":
            body = self._body()
            self._handle(
                lambda: self.service.reject_merge(parts[2], _principal(body), body.get("reason", "")),
                201,
            )
        elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "split":
            body = self._body()
            self._handle(
                lambda: self.service.split_group(
                    parts[2], body["peeled_rids"], _principal(body), body.get("reason", "")
                ),
                201,
            )
        elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "archive":
            body = self._body()
            self._handle(lambda: self.service.archive_group(parts[2], _principal(body)), 201)
        elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "corrections":
            body = self._body()
            self._handle(
                lambda: self.service.apply_correction(
                    group_id=parts[2],
                    kind=body["kind"],
                    principal=_principal(body),
                    reason=body.get("reason", ""),
                    delta_minutes=float(body.get("delta_minutes", 0)),
                    rid=body.get("rid"),
                    note=body.get("note", ""),
                ),
                201,
            )
        elif len(parts) == 4 and parts[:2] == ["api", "records"] and parts[3] == "checkin":
            body = self._body()
            self._handle(
                lambda: self.service.record_checkin(parts[2], body.get("at"), int(body.get("late_grace_minutes", 15))),
                201,
            )
        else:
            self._send(404, {"error": "not_found", "message": path})


def create_server(host: str = "127.0.0.1", port: int = 8080, db_path: str | None = None) -> ThreadingHTTPServer:
    service = build_service(db_path)

    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd
