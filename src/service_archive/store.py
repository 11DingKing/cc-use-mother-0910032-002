"""SQLite 只增事件存储。

事件一经写入不可修改、不可删除；``idempotency_key`` 保证管理命令重跑时
同一逻辑操作不会产生第二条事件。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    stream           TEXT NOT NULL,
    type             TEXT NOT NULL,
    operator         TEXT,
    created_at       TEXT NOT NULL,
    idempotency_key  TEXT,
    payload          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idem
    ON events(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


class EventStore:
    def __init__(self, path: str | Path | None = ":memory:") -> None:
        self._lock = threading.RLock()
        if path == ":memory:":
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def append(
        self,
        stream: str,
        etype: str,
        payload: dict,
        created_at: str,
        operator: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Event, bool]:
        """追加事件。

        幂等键已存在时返回原事件，``appended=False``。
        """
        if idempotency_key is not None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    return self._to_event(row), False
            return self._insert(stream, etype, payload, created_at, operator, idempotency_key)
        return self._insert(stream, etype, payload, created_at, operator, idempotency_key)

    def _insert(self, stream, etype, payload, created_at, operator, idempotency_key):
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO events(stream, type, operator, created_at, idempotency_key, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (stream, etype, operator, created_at, idempotency_key, json.dumps(payload, ensure_ascii=False)),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # 并发写入了相同幂等键：返回既有事件。
                row = self._conn.execute(
                    "SELECT * FROM events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return self._to_event(row), False
        event = Event(
            seq=cur.lastrowid,
            etype=etype,
            payload=payload,
            created_at=created_at,
            stream=stream,
            operator=operator,
            idempotency_key=idempotency_key,
        )
        return event, True

    def read_all(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            return [self._to_event(row) for row in rows]

    @staticmethod
    def _to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=row["seq"],
            etype=row["type"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            stream=row["stream"],
            operator=row["operator"],
            idempotency_key=row["idempotency_key"],
        )

    def close(self) -> None:
        self._conn.close()
