"""SQLite 持久化层。

设计要点：

- ``records``：服务记录主表，含来源指纹、身份线索、场次证据与合并指针。
- ``sources``：指纹首次出现与重传计数，保证批量重传可识别、幂等。
- ``candidates`` / ``candidate_members``：冲突候选与合并建议，状态机留痕。
- ``blocked_pairs``：人工驳回或拆分解除的记录对，重跑去重不再成案。
- ``events``：仅追加的来源谱系事件流，prev/哈希链防篡改。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from .canonical import canonical_dumps, content_id, sha256_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    fingerprint     TEXT PRIMARY KEY,
    first_record_id TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    transmit_count  INTEGER NOT NULL DEFAULT 1,
    last_batch_no   TEXT NOT NULL,
    last_transmit_seq INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS records (
    id            TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    school_code   TEXT NOT NULL,
    submitter     TEXT NOT NULL,
    volunteer_name TEXT NOT NULL,
    id_tail       TEXT NOT NULL DEFAULT '',
    session_code  TEXT NOT NULL,
    session_name  TEXT NOT NULL DEFAULT '',
    service_start TEXT NOT NULL,
    service_end   TEXT NOT NULL,
    minutes       INTEGER NOT NULL,
    checkin_at    TEXT,
    batch_no      TEXT NOT NULL,
    transmit_seq  INTEGER NOT NULL DEFAULT 1,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL,
    merged_into   TEXT REFERENCES records(id),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_code);
CREATE INDEX IF NOT EXISTS idx_records_merged ON records(merged_into);

CREATE TABLE IF NOT EXISTS candidates (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    survivor_id  TEXT NOT NULL REFERENCES records(id),
    members_json TEXT NOT NULL,
    scores_json  TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    run_no       INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS candidate_members (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    record_id    TEXT NOT NULL REFERENCES records(id),
    role         TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    PRIMARY KEY (candidate_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_record ON candidate_members(record_id);

CREATE TABLE IF NOT EXISTS blocked_pairs (
    pair_key     TEXT PRIMARY KEY,
    record_a     TEXT NOT NULL,
    record_b     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    candidate_id TEXT REFERENCES candidates(id),
    blocked_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    record_id  TEXT REFERENCES records(id),
    actor      TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_record ON events(record_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS runs (
    run_no     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    input_sig  TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # HTTP 服务在工作线程中串行使用同一连接：关闭同线程校验，
        # 依赖上层调用方保证不跨线程并发写入。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def init(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- 记录

    def get_record(self, record_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_record_required(self, record_id: str) -> dict:
        rec = self.get_record(record_id)
        if rec is None:
            raise KeyError(record_id)
        return rec

    def find_by_fingerprint(self, fingerprint: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM records WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def insert_record(self, fields: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO records (
                id, fingerprint, school_code, submitter, volunteer_name, id_tail,
                session_code, session_name, service_start, service_end, minutes,
                checkin_at, batch_no, transmit_seq, payload_json, status,
                merged_into, created_at
            ) VALUES (
                :id, :fingerprint, :school_code, :submitter, :volunteer_name, :id_tail,
                :session_code, :session_name, :service_start, :service_end, :minutes,
                :checkin_at, :batch_no, :transmit_seq, :payload_json, :status,
                :merged_into, :created_at
            )
            """,
            fields,
        )

    def touch_source(self, fingerprint: str, record_id: str, seen_at: str,
                     batch_no: str, transmit_seq: int) -> None:
        self._conn.execute(
            """
            INSERT INTO sources (fingerprint, first_record_id, first_seen_at,
                                 transmit_count, last_batch_no, last_transmit_seq)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                transmit_count = transmit_count + 1,
                last_batch_no = excluded.last_batch_no,
                last_transmit_seq = excluded.last_transmit_seq
            """,
            (fingerprint, record_id, seen_at, batch_no, transmit_seq),
        )

    def get_source(self, fingerprint: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sources WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def active_records_for_dedup(self) -> list[dict]:
        """参与去重重跑的记录：未取消、未归档、未作为重复并入他者。"""
        rows = self._conn.execute(
            """
            SELECT * FROM records
            WHERE status NOT IN ('cancelled', 'archived') AND merged_into IS NULL
            ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def list_records(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM records ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def records_by_session(self, session_code: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE session_code = ? ORDER BY id", (session_code,)
        ).fetchall()
        return [dict(r) for r in rows]

    def merged_group(self, survivor_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE id = ? OR merged_into = ? ORDER BY id",
            (survivor_id, survivor_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, record_ids: Sequence[str], status: str) -> None:
        self._conn.executemany(
            "UPDATE records SET status = ? WHERE id = ?",
            [(status, rid) for rid in record_ids],
        )

    def set_merged_into(self, record_ids: Sequence[str], survivor_id: str) -> None:
        self._conn.executemany(
            "UPDATE records SET merged_into = ?, status = 'confirmed' WHERE id = ?",
            [(survivor_id, rid) for rid in record_ids],
        )

    def unmerge(self, record_ids: Sequence[str]) -> None:
        self._conn.executemany(
            "UPDATE records SET merged_into = NULL, status = 'received' WHERE id = ?",
            [(rid,) for rid in record_ids],
        )

    def mark_survivor_confirmed(self, survivor_id: str) -> None:
        self._conn.execute(
            "UPDATE records SET status = 'confirmed' WHERE id = ? AND status <> 'archived'",
            (survivor_id,),
        )

    def set_checkin(self, record_id: str, checkin_at: str) -> None:
        self._conn.execute(
            "UPDATE records SET checkin_at = ? WHERE id = ?", (checkin_at, record_id)
        )

    def correct_field(self, record_id: str, field: str, value: Any) -> None:
        # field 来自服务层白名单，不接受任意列名
        self._conn.execute(
            f"UPDATE records SET {field} = ? WHERE id = ?", (value, record_id)
        )

    def update_transmit(self, record_id: str, transmit_seq: int) -> None:
        self._conn.execute(
            "UPDATE records SET transmit_seq = ? WHERE id = ?",
            (transmit_seq, record_id),
        )

    # ---------------------------------------------------------------- 候选

    @staticmethod
    def candidate_id(members: Sequence[str]) -> str:
        return content_id("cand_", sorted(members))

    def insert_candidate(self, cand: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO candidates (id, status, survivor_id, members_json, scores_json,
                                    reasons_json, run_no, created_at, decided_by,
                                    decided_at, decision_note)
            VALUES (:id, :status, :survivor_id, :members_json, :scores_json,
                    :reasons_json, :run_no, :created_at, :decided_by,
                    :decided_at, :decision_note)
            """,
            cand,
        )
        self._conn.executemany(
            "INSERT INTO candidate_members (candidate_id, record_id, role, ord) "
            "VALUES (?, ?, ?, ?)",
            [
                (cand["id"], rid, role, i)
                for i, (rid, role) in enumerate(cand["members_roles"])
            ],
        )

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_candidate_by_members(self, members: Sequence[str]) -> dict | None:
        return self.get_candidate(self.candidate_id(members))

    def list_candidates(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM candidates ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def candidate_members(self, candidate_id: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT cm.record_id AS record_id, cm.role AS role, r.status AS status
            FROM candidate_members cm JOIN records r ON r.id = cm.record_id
            WHERE cm.candidate_id = ? ORDER BY cm.ord""",
            (candidate_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def decide_candidate(self, candidate_id: str, status: str,
                         actor: str, decided_at: str, note: str) -> None:
        self._conn.execute(
            """
            UPDATE candidates SET status = ?, decided_by = ?, decided_at = ?,
                                  decision_note = ?
            WHERE id = ?
            """,
            (status, actor, decided_at, note, candidate_id),
        )

    def supersede_candidate(self, candidate_id: str) -> None:
        self._conn.execute(
            "UPDATE candidates SET status = 'superseded' WHERE id = ?",
            (candidate_id,),
        )

    def set_candidate_run_no(self, candidate_id: str, run_no: int) -> None:
        self._conn.execute(
            "UPDATE candidates SET run_no = ? WHERE id = ?", (run_no, candidate_id)
        )

    # ------------------------------------------------------------- 阻断对

    @staticmethod
    def pair_key(a: str, b: str) -> str:
        lo, hi = sorted((a, b))
        return f"{lo}|{hi}"

    def block_pair(self, a: str, b: str, reason: str, blocked_by: str,
                   created_at: str, candidate_id: str | None = None) -> bool:
        """登记阻断对；已存在则返回 False。"""
        key = self.pair_key(a, b)
        lo, hi = sorted((a, b))
        cur = self._conn.execute(
            "SELECT 1 FROM blocked_pairs WHERE pair_key = ?", (key,)
        ).fetchone()
        if cur:
            return False
        self._conn.execute(
            """
            INSERT INTO blocked_pairs (pair_key, record_a, record_b, reason,
                                       candidate_id, blocked_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (key, lo, hi, reason, candidate_id, blocked_by, created_at),
        )
        return True

    def is_blocked(self, a: str, b: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocked_pairs WHERE pair_key = ?", (self.pair_key(a, b),)
        ).fetchone()
        return row is not None

    def all_blocked_pairs(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT record_a, record_b FROM blocked_pairs ORDER BY pair_key"
        ).fetchall()
        return [(r["record_a"], r["record_b"]) for r in rows]

    # ---------------------------------------------------------------- 事件

    def add_event(self, event_type: str, actor: str, occurred_at: str,
                  payload: dict, record_id: str | None = None,
                  event_id: str | None = None) -> dict:
        prev = self._conn.execute(
            "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["hash"] if prev else ""
        eid = event_id or content_id(
            "evt_",
            {"t": event_type, "r": record_id, "p": payload, "at": occurred_at},
        )
        body = canonical_dumps(
            {
                "event_id": eid,
                "type": event_type,
                "record_id": record_id,
                "actor": actor,
                "occurred_at": occurred_at,
                "payload": payload,
                "prev_hash": prev_hash,
            }
        )
        digest = sha256_text(prev_hash + "\n" + body)
        self._conn.execute(
            """
            INSERT INTO events (event_id, event_type, record_id, actor, occurred_at,
                                payload_json, prev_hash, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, event_type, record_id, actor, occurred_at,
             canonical_dumps(payload), prev_hash, digest),
        )
        return {"event_id": eid, "hash": digest, "seq": self._conn.execute(
            "SELECT seq FROM events WHERE event_id = ?", (eid,)
        ).fetchone()["seq"]}

    def list_events(self, record_id: str | None = None) -> list[dict]:
        if record_id:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE record_id = ? ORDER BY seq", (record_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def verify_chain(self) -> dict:
        """重算整条事件哈希链，返回链长与第一个断裂点（若有）。"""
        rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev_hash = ""
        for r in rows:
            payload = json.loads(r["payload_json"])
            body = canonical_dumps(
                {
                    "event_id": r["event_id"],
                    "type": r["event_type"],
                    "record_id": r["record_id"],
                    "actor": r["actor"],
                    "occurred_at": r["occurred_at"],
                    "payload": payload,
                    "prev_hash": r["prev_hash"],
                }
            )
            expect = sha256_text(prev_hash + "\n" + body)
            if r["prev_hash"] != prev_hash or r["hash"] != expect:
                return {"ok": False, "length": len(rows), "broken_at": r["seq"]}
            prev_hash = r["hash"]
        return {"ok": True, "length": len(rows), "broken_at": None}

    # ---------------------------------------------------------------- 运行

    def record_run(self, kind: str, started_at: str, finished_at: str,
                   stats: dict, input_sig: str) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO runs (kind, started_at, finished_at, stats_json, input_sig)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, started_at, finished_at, canonical_dumps(stats), input_sig),
        )
        return int(cur.lastrowid)

    def list_runs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY run_no"
        ).fetchall()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()
