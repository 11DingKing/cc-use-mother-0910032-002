"""管理命令：去重重跑。

用法::

    python3 -m tools.dedupe --db service_archive.db [--run-id ID] [--json]

该命令可安全地反复执行：run_id 默认由全部来源指纹与算法版本派生，
数据未变则零新增事件，建议编号、状态与人工结论保持稳定。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive.service import ServiceArchive
from service_archive.store import EventStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重跑服务记录去重")
    parser.add_argument("--db", default="service_archive.db", help="SQLite 事件库路径")
    parser.add_argument("--run-id", default=None, help="显式运行编号（默认由指纹派生）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    service = ServiceArchive(EventStore(args.db))
    result = service.run_dedupe(args.run_id)
    pending = service.list_suggestions()

    if args.json:
        print(json.dumps({"run": result, "pending": pending}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"运行编号：{result['run_id']}")
        print(f"候选组数：{result['candidate_count']}，新提出：{len(result['raised'])}，"
              f"作废：{len(result['obsoleted'])}，抑制：{len(result['suppressed'])}")
        print(f"待人工处理建议：{len(pending)}")
        for item in pending:
            print(f"  - {item['suggestion_id']}#g{item['generation']} "
                  f"置信度={item['confidence']} 成员={len(item['rids'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
