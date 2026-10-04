"""基于标准库的 JSON HTTP 服务。

鉴权：``Authorization: Bearer <token>``，令牌在启动时通过 JSON 文件注入，
映射到账号与岗位；``dev_headers=True`` 时额外允许测试用的 X-Actor-* 头。

路由：

- ``POST /v1/submissions`` / ``/v1/submissions/batch``
- ``POST /v1/dedup-runs``            重跑去重（结果稳定）
- ``GET  /v1/candidates``
- ``POST /v1/candidates/{id}/confirm`` / ``reject``
- ``POST /v1/records/{id}/split``
- ``POST /v1/records/{id}/late-checkin``
- ``POST /v1/sessions/{code}/cancel``
- ``POST /v1/archive``
- ``POST /v1/records/{id}/corrections``
- ``GET  /v1/records`` / ``/v1/records/{id}/lineage``
- ``GET  /v1/health``
"""
from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from .auth import Actor, AuthzError
from .models import Submission
from .service import (
    ConflictError,
    DedupService,
    NotFoundError,
    ServiceError,
    ValidationError,
)


def _build_submission(data: dict) -> Submission:
    required = (
        "school_code", "submitter", "volunteer_name", "session_code",
        "service_start", "service_end", "minutes", "batch_no",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValidationError("提交缺少字段：" + "、".join(missing))
    return Submission(
        school_code=str(data["school_code"]),
        submitter=str(data["submitter"]),
        volunteer_name=str(data["volunteer_name"]),
        id_tail=str(data.get("id_tail", "")),
        session_code=str(data["session_code"]),
        session_name=str(data.get("session_name", "")),
        service_start=str(data["service_start"]),
        service_end=str(data["service_end"]),
        minutes=int(data["minutes"]),
        batch_no=str(data["batch_no"]),
        checkin_at=data.get("checkin_at"),
        payload=data.get("payload", {}),
        transmitted_at=data.get("transmitted_at"),
    )


def create_handler(service: DedupService, tokens: dict[str, dict] | None = None,
                   dev_headers: bool = False) -> type[BaseHTTPRequestHandler]:
    tokens = tokens or {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ServiceRecordsDedup/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # ---------------------------------------------------------- 工具

        def _actor(self) -> Actor:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                token = header[len("Bearer "):].strip()
                principal = tokens.get(token)
                if principal is None:
                    raise AuthzError("令牌无效")
                return Actor(account=principal["account"], role=principal["role"],
                             name=principal.get("name", ""))
            if dev_headers:
                account = self.headers.get("X-Actor-Account")
                role = self.headers.get("X-Actor-Role", "")
                if account and role:
                    return Actor(account=account, role=role,
                                 name=self.headers.get("X-Actor-Name", ""))
            raise AuthzError("缺少 Authorization: Bearer 令牌")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return value

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error_status(self, exc: Exception) -> int:
            if isinstance(exc, AuthzError):
                return HTTPStatus.FORBIDDEN
            if isinstance(exc, NotFoundError):
                return HTTPStatus.NOT_FOUND
            if isinstance(exc, ConflictError):
                return HTTPStatus.CONFLICT
            if isinstance(exc, ValidationError):
                return HTTPStatus.BAD_REQUEST
            return HTTPStatus.INTERNAL_SERVER_ERROR

        # ---------------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parsed.query
                if path == "/v1/health" and method == "GET":
                    self._send(HTTPStatus.OK, {"status": "ok"})
                    return
                actor = self._actor()
                body = self._read_json() if method == "POST" else {}
                self._route(method, path, query, actor, body)
            except ServiceError as exc:
                self._send(self._error_status(exc), {"error": str(exc)})
            except AuthzError as exc:
                self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": f"参数错误：{exc}"})
            except Exception as exc:  # noqa: BLE001 - 统一兜底
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def _route(self, method: str, path: str, query: str,
                   actor: Actor, body: dict) -> None:
            p = path.strip("/").split("/")
            S = HTTPStatus

            if method == "POST" and path == "/v1/submissions":
                result = service.receive(actor, _build_submission(body))
                self._send(S.CREATED, result)
            elif method == "POST" and path == "/v1/submissions/batch":
                subs = [_build_submission(item) for item in body.get("submissions", [])]
                self._send(S.CREATED, service.receive_batch(actor, subs))
            elif method == "POST" and path == "/v1/dedup-runs":
                self._send(S.OK, service.run_dedup(actor))
            elif method == "GET" and path == "/v1/candidates":
                status = body.get("status")
                if not status:
                    for kv in query.split("&"):
                        if kv.startswith("status="):
                            from urllib.parse import unquote
                            status = unquote(kv.split("=", 1)[1])
                self._send(S.OK, {"candidates": service.list_proposals(status)})
            elif method == "GET" and path == "/v1/records":
                self._send(S.OK, {"records": service.store.list_records()})
            elif len(p) == 4 and p[:2] == ["v1", "candidates"]:
                cid = p[2]
                if p[3] == "confirm" and method == "POST":
                    self._send(S.OK, service.confirm_merge(
                        actor, cid, str(body.get("note", ""))))
                elif p[3] == "reject" and method == "POST":
                    self._send(S.OK, service.reject_candidate(
                        actor, cid, str(body.get("note", ""))))
                else:
                    self._send(S.NOT_FOUND, {"error": "无此路由"})
            elif len(p) == 4 and p[:2] == ["v1", "records"]:
                rid = p[2]
                if p[3] == "lineage" and method == "GET":
                    self._send(S.OK, service.lineage(rid))
                elif p[3] == "split" and method == "POST":
                    self._send(S.OK, service.split_merge(
                        actor, rid, body.get("release_ids"),
                        str(body.get("note", ""))))
                elif p[3] == "late-checkin" and method == "POST":
                    self._send(S.OK, service.late_checkin(
                        actor, rid, str(body["checkin_at"])))
                elif p[3] == "verify" and method == "POST":
                    self._send(S.OK, service.verify_record(
                        actor, rid, str(body.get("note", ""))))
                elif p[3] == "corrections" and method == "POST":
                    self._send(S.OK, service.correct_archived(
                        actor, rid, dict(body.get("changes", {})),
                        str(body.get("reason", ""))))
                else:
                    self._send(S.NOT_FOUND, {"error": "无此路由"})
            elif len(p) == 4 and p[:2] == ["v1", "sessions"] and method == "POST":
                if p[3] == "cancel":
                    self._send(S.OK, service.cancel_session(
                        actor, p[2], str(body.get("reason", ""))))
                else:
                    self._send(S.NOT_FOUND, {"error": "无此路由"})
            elif method == "POST" and path == "/v1/archive":
                ids = body.get("record_ids", [])
                self._send(S.OK, service.archive(actor, list(ids)))
            else:
                self._send(S.NOT_FOUND, {"error": "无此路由"})

    return Handler


def serve(service: DedupService, host: str, port: int,
          tokens: dict[str, dict] | None = None,
          dev_headers: bool = False) -> HTTPServer:
    httpd = HTTPServer((host, port), create_handler(service, tokens, dev_headers))
    return httpd
