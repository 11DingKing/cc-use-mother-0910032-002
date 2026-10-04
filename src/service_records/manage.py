"""管理命令。

用法：

    python -m service_records.manage init-db --db data/app.db
    python -m service_records.manage import --db data/app.db --file subs.jsonl \
        --actor op_zhang --role operator
    python -m service_records.manage dedup --db data/app.db --actor op_zhang
    python -m service_records.manage lineage --db data/app.db --record rec_xxx
    python -m service_records.manage verify-chain --db data/app.db
    python -m service_records.manage serve --db data/app.db --host 127.0.0.1 \
        --port 8080 --tokens tokens.json [--dev-headers]

``dedup`` 可重复执行：已存在的建议会被跳过，被驳回/拆分的配对不再成案，
输出按编号排序，因此结果稳定。

tokens.json 形如：
    {"t-op-1": {"account": "op_zhang", "role": "operator", "name": "张运营"}}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auth import ROLE_OPERATOR, Actor
from .httpapi import serve
from .models import Submission
from .service import DedupService
from .store import Store


def _submission_from_dict(data: dict) -> Submission:
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


def _load_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if isinstance(value, dict) and "submissions" in value:
        return list(value["submissions"])
    if isinstance(value, list):
        return value
    raise SystemExit("导入文件需为 JSONL，或含 submissions 数组的 JSON")


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service_records.manage")
    parser.add_argument("--db", default="data/app.db", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库表")

    p_import = sub.add_parser("import", help="从 JSONL/JSON 导入提交")
    p_import.add_argument("--file", required=True)
    p_import.add_argument("--actor", required=True)
    p_import.add_argument("--role", default=ROLE_OPERATOR)
    p_import.add_argument("--name", default="")

    p_dedup = sub.add_parser("dedup", help="重跑去重识别（可重复执行）")
    p_dedup.add_argument("--actor", required=True)
    p_dedup.add_argument("--role", default=ROLE_OPERATOR)
    p_dedup.add_argument("--proposals", action="store_true",
                         help="同时打印当前待处理建议")

    p_line = sub.add_parser("lineage", help="查看记录来源谱系")
    p_line.add_argument("--record", required=True)

    p_verify = sub.add_parser("verify", help="人工核验无冲突记录")
    p_verify.add_argument("--record", required=True)
    p_verify.add_argument("--actor", required=True)
    p_verify.add_argument("--role", default=ROLE_OPERATOR)
    p_verify.add_argument("--note", default="")

    p_conf = sub.add_parser("confirm", help="确认合并候选")
    p_conf.add_argument("--candidate", required=True)
    p_conf.add_argument("--actor", required=True)
    p_conf.add_argument("--role", default=ROLE_OPERATOR)
    p_conf.add_argument("--note", default="")

    p_rej = sub.add_parser("reject", help="拒绝合并候选（记录对永久阻断）")
    p_rej.add_argument("--candidate", required=True)
    p_rej.add_argument("--actor", required=True)
    p_rej.add_argument("--role", default=ROLE_OPERATOR)
    p_rej.add_argument("--note", default="")

    p_split = sub.add_parser("split", help="拆分误合并")
    p_split.add_argument("--survivor", required=True)
    p_split.add_argument("--release", nargs="*", help="要拆出的记录号，缺省全部拆出")
    p_split.add_argument("--actor", required=True)
    p_split.add_argument("--role", default=ROLE_OPERATOR)
    p_split.add_argument("--note", default="")

    p_checkin = sub.add_parser("late-checkin", help="补登迟到签到")
    p_checkin.add_argument("--record", required=True)
    p_checkin.add_argument("--at", required=True, help="签到时间 ISO8601")
    p_checkin.add_argument("--actor", required=True)
    p_checkin.add_argument("--role", default=ROLE_OPERATOR)

    p_cancel = sub.add_parser("cancel-session", help="取消场次")
    p_cancel.add_argument("--session", required=True)
    p_cancel.add_argument("--actor", required=True)
    p_cancel.add_argument("--role", default=ROLE_OPERATOR)
    p_cancel.add_argument("--reason", default="")

    p_arch = sub.add_parser("archive", help="归档已确认主记录")
    p_arch.add_argument("--records", nargs="+", required=True)
    p_arch.add_argument("--actor", required=True)
    p_arch.add_argument("--role", default=ROLE_OPERATOR)

    p_corr = sub.add_parser("correct", help="归档后更正（追加更正事件）")
    p_corr.add_argument("--record", required=True)
    p_corr.add_argument("--set", dest="changes", nargs="+", required=True,
                        metavar="字段=值", help="如 minutes=100 volunteer_name=王晓明")
    p_corr.add_argument("--reason", required=True)
    p_corr.add_argument("--actor", required=True)
    p_corr.add_argument("--role", default=ROLE_OPERATOR)

    sub.add_parser("verify-chain", help="校验事件哈希链完整性")

    p_list = sub.add_parser("list", help="列出记录或候选")
    p_list.add_argument("kind", choices=["records", "candidates"])
    p_list.add_argument("--status")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--tokens", help="令牌映射 JSON 文件")
    p_serve.add_argument("--dev-headers", action="store_true",
                         help="允许 X-Actor-* 头（仅限开发测试）")

    args = parser.parse_args(argv)

    store = Store(args.db)
    store.init()
    svc = DedupService(store)

    try:
        if args.command == "init-db":
            _print({"db": args.db, "initialized": True})

        elif args.command == "import":
            actor = Actor(args.actor, args.role, args.name)
            rows = _load_records(Path(args.file))
            result = svc.receive_batch(
                actor, [_submission_from_dict(row) for row in rows]
            )
            _print({"imported_file": args.file, **result})

        elif args.command == "dedup":
            actor = Actor(args.actor, args.role)
            result = svc.run_dedup(actor)
            _print(result)
            if args.proposals:
                _print({"proposals": svc.list_proposals("proposed")})

        elif args.command == "lineage":
            _print(svc.lineage(args.record))

        elif args.command == "verify":
            actor = Actor(args.actor, args.role)
            _print(svc.verify_record(actor, args.record, args.note))

        elif args.command == "confirm":
            actor = Actor(args.actor, args.role)
            _print(svc.confirm_merge(actor, args.candidate, args.note))

        elif args.command == "reject":
            actor = Actor(args.actor, args.role)
            _print(svc.reject_candidate(actor, args.candidate, args.note))

        elif args.command == "split":
            actor = Actor(args.actor, args.role)
            _print(svc.split_merge(actor, args.survivor, args.release, args.note))

        elif args.command == "late-checkin":
            actor = Actor(args.actor, args.role)
            _print(svc.late_checkin(actor, args.record, args.at))

        elif args.command == "cancel-session":
            actor = Actor(args.actor, args.role)
            _print(svc.cancel_session(actor, args.session, args.reason))

        elif args.command == "archive":
            actor = Actor(args.actor, args.role)
            _print(svc.archive(actor, args.records))

        elif args.command == "correct":
            actor = Actor(args.actor, args.role)
            changes: dict = {}
            for item in args.changes:
                if "=" not in item:
                    raise SystemExit(f"更正项格式应为 字段=值：{item}")
                key, raw = item.split("=", 1)
                changes[key] = int(raw) if key == "minutes" else raw
            _print(svc.correct_archived(actor, args.record, changes, args.reason))

        elif args.command == "verify-chain":
            _print(store.verify_chain())

        elif args.command == "list":
            if args.kind == "records":
                _print({"records": store.list_records(args.status)})
            else:
                _print({"candidates": svc.list_proposals(args.status)})

        elif args.command == "serve":
            tokens = {}
            if args.tokens:
                tokens = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
            httpd = serve(svc, args.host, args.port, tokens, args.dev_headers)
            print(f"listening on http://{args.host}:{args.port}", file=sys.stderr)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.server_close()
            return 0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
