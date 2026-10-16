"""Локальная база SQLite: письма, цепочки, черновики, образцы ответов, запуски."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    mailbox TEXT NOT NULL,
    uid_validity INTEGER,
    uid INTEGER NOT NULL,
    message_id TEXT,
    in_reply_to TEXT,
    refs TEXT,
    subject TEXT,
    norm_subject TEXT,
    from_raw TEXT,
    from_addr TEXT,
    to_raw TEXT,
    cc_raw TEXT,
    date TEXT NOT NULL,
    text TEXT,
    new_text TEXT,
    is_mine INTEGER DEFAULT 0,
    category TEXT,
    thread_id TEXT,
    incidents TEXT,
    attachments TEXT,
    fetched_at TEXT,
    UNIQUE (mailbox, uid_validity, uid)
);
CREATE INDEX IF NOT EXISTS ix_messages_mid ON messages(message_id);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS ix_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    category TEXT,
    subject TEXT,
    norm_subject TEXT,
    incidents TEXT,
    first_date TEXT,
    last_date TEXT,
    msg_count INTEGER DEFAULT 0,
    flags TEXT,
    state TEXT,
    analyzed_upto TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_threads_last ON threads(last_date);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    thread_id TEXT,
    reply_to_id INTEGER,
    reply_all INTEGER DEFAULT 0,
    text_proposed TEXT,
    text_final TEXT,
    status TEXT DEFAULT 'proposed',
    created_at TEXT,
    sent_at TEXT,
    send_result TEXT
);

CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY,
    source TEXT,
    subject TEXT,
    incoming TEXT,
    reply TEXT,
    terms TEXT,
    date TEXT,
    UNIQUE (source, subject, date)
);

CREATE TABLE IF NOT EXISTS events (
    uid TEXT PRIMARY KEY,
    start TEXT,
    end TEXT,
    subject TEXT,
    location TEXT,
    organizer TEXT,
    status TEXT,
    source TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT,
    started_at TEXT,
    period_from TEXT,
    period_to TEXT,
    output TEXT,
    info TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_FIELDS = {"refs", "incidents", "attachments", "flags", "state", "info"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # --- общие помощники -------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            if d[k]:
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    pass
        return d

    def _rows(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self._rows(self.db.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        return self._row(self.db.execute(sql, tuple(params)).fetchone())

    @staticmethod
    def _dump(v: Any) -> Any:
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v

    # --- kv --------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # --- письма ------------------------------------------------------------
    def has_message(self, mailbox: str, uid_validity: int | None, uid: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE mailbox=? AND uid_validity IS ? AND uid=?",
            (mailbox, uid_validity, uid),
        ).fetchone()
        return row is not None

    def insert_message(self, m: dict[str, Any]) -> int | None:
        cols = [
            "mailbox", "uid_validity", "uid", "message_id", "in_reply_to", "refs", "subject",
            "norm_subject", "from_raw", "from_addr", "to_raw", "cc_raw", "date", "text", "new_text",
            "is_mine", "category", "thread_id", "incidents", "attachments", "fetched_at",
        ]
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO messages({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            [self._dump(m.get(c)) for c in cols],
        )
        return cur.lastrowid if cur.rowcount else None

    def update_message(self, msg_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE messages SET {sets} WHERE id=?", [self._dump(v) for v in fields.values()] + [msg_id])

    def message(self, msg_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM messages WHERE id=?", (msg_id,))

    def messages_without_thread(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM messages WHERE thread_id IS NULL ORDER BY date, id")

    def thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM messages WHERE thread_id=? ORDER BY date, id", (thread_id,))

    def messages_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM messages WHERE date>=? AND date<? ORDER BY date, id", (start, end))

    def find_by_message_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = [i for i in ids if i]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return self.query(f"SELECT * FROM messages WHERE message_id IN ({q}) AND thread_id IS NOT NULL", ids)

    # --- цепочки -----------------------------------------------------------
    def thread(self, thread_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM threads WHERE id=?", (thread_id,))

    def upsert_thread(self, t: dict[str, Any]) -> None:
        cols = ["id", "category", "subject", "norm_subject", "incidents", "first_date", "last_date",
                "msg_count", "flags", "state", "analyzed_upto", "updated_at"]
        vals = [self._dump(t.get(c)) for c in cols]
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
        self.db.execute(
            f"INSERT INTO threads({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            vals,
        )

    def threads_active_since(self, since: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM threads WHERE last_date>=? ORDER BY last_date DESC", (since,))

    def threads_by_incident(self, incident: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM threads WHERE incidents LIKE ? ORDER BY last_date DESC", (f'%"{incident}"%',))

    def threads_by_subject(self, norm_subject: str, since: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM threads WHERE norm_subject=? AND last_date>=? ORDER BY last_date DESC",
            (norm_subject, since),
        )

    # --- черновики ---------------------------------------------------------
    def add_draft(self, d: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO drafts(thread_id, reply_to_id, reply_all, text_proposed, status, created_at) VALUES(?,?,?,?,?,?)",
            (d["thread_id"], d["reply_to_id"], int(bool(d.get("reply_all"))), d["text_proposed"], "proposed", d["created_at"]),
        )
        return int(cur.lastrowid)

    def drafts(self, statuses: tuple[str, ...] = ("proposed",)) -> list[dict[str, Any]]:
        q = ",".join("?" * len(statuses))
        return self.query(f"SELECT * FROM drafts WHERE status IN ({q}) ORDER BY id", statuses)

    def draft(self, draft_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def update_draft(self, draft_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE drafts SET {sets} WHERE id=?", [self._dump(v) for v in fields.values()] + [draft_id])

    # --- образцы ответов ---------------------------------------------------
    def add_example(self, e: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO examples(source, subject, incoming, reply, terms, date) VALUES(?,?,?,?,?,?)",
            (e["source"], e.get("subject"), e.get("incoming"), e["reply"], e.get("terms"), e.get("date")),
        )

    def examples(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM examples ORDER BY date DESC")

    # --- календарь -----------------------------------------------------------
    def upsert_event(self, ev: dict[str, Any]) -> None:
        cols = ["uid", "start", "end", "subject", "location", "organizer", "status", "source", "updated_at"]
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
        self.db.execute(
            f"INSERT INTO events({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) ON CONFLICT(uid) DO UPDATE SET {updates}",
            [ev.get(c) for c in cols],
        )

    def events_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM events WHERE start<? AND end>? AND (status IS NULL OR status!='CANCELLED') ORDER BY start",
            (end, start),
        )

    # --- запуски -------------------------------------------------------------
    def add_run(self, kind: str, started_at: str, period_from: str | None, period_to: str | None,
                output: str | None = None, info: dict[str, Any] | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(kind, started_at, period_from, period_to, output, info) VALUES(?,?,?,?,?,?)",
            (kind, started_at, period_from, period_to, output, self._dump(info or {})),
        )
        return int(cur.lastrowid)

    def last_run(self, kind: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
