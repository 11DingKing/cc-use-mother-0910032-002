"""HTTP 服务启动入口。

用法::

    python3 -m tools.serve --host 127.0.0.1 --port 8080 --db service_archive.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service_archive.api import create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动服务记录防重复归档 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="service_archive.db")
    args = parser.parse_args(argv)

    httpd = create_server(args.host, args.port, args.db)
    print(f"服务监听 http://{args.host}:{args.port}（事件库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
