"""SQLite schema and queries: ``items``, ``sessions``, ``meta``.

All timestamps are naive UTC ISO strings (``YYYY-MM-DDTHH:MM:SS``) so SQL string
comparison orders correctly.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from ccinbox import config

OPEN_STATUSES = ("open", "snoozed")
STATUSES = ("open", "snoozed", "done", "dismissed", "auto_resolved")
ACTIONS = ("done", "dismiss", "snooze", "undo", "reopen")
_ACTION_STATUS = {"done": "done", "dismiss": "dismissed", "snooze": "snoozed", "reopen": "open"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    snoozed_until TEXT,
    prev_status TEXT,
    prev_snoozed_until TEXT,
    session_id TEXT,
    session_title TEXT,
    cwd TEXT,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, fingerprint)
);
CREATE INDEX IF NOT EXISTS items_status_idx ON items(status, snoozed_until);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    project TEXT,
    transcript_mtime REAL NOT NULL DEFAULT 0,
    processed_at TEXT,
    items_added INTEGER NOT NULL DEFAULT 0,
    items_resolved INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def next_snooze_time(now: datetime | None = None) -> str:
    """Tomorrow at ``SNOOZE_HOUR`` local time, returned as UTC ISO."""
    local = (now or datetime.now(timezone.utc)).astimezone()
    target = (local + timedelta(days=1)).replace(hour=config.SNOOZE_HOUR, minute=0, second=0, microsecond=0)
    return to_iso(target)


def connect(path=None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "topic" not in cols:  # DBs created before topics existed
        conn.execute("ALTER TABLE items ADD COLUMN topic TEXT NOT NULL DEFAULT ''")


def fingerprint(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ---- items -----------------------------------------------------------------


def insert_item(
    conn: sqlite3.Connection,
    project: str,
    title: str,
    details: str = "",
    *,
    topic: str = "",
    session_id: str | None = None,
    session_title: str | None = None,
    cwd: str | None = None,
) -> int | None:
    """Insert an open item. Returns the new id, or None when the fingerprint already exists."""
    title = " ".join(title.split()).strip()
    fp = fingerprint(title)
    if not fp:
        return None
    ts = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (project, title, details, topic, fingerprint, status, session_id, session_title,"
        " cwd, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
        (project, title, details or "", clean_topic(topic), fp, session_id, session_title, cwd, ts, ts),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def clean_topic(topic) -> str:
    return " ".join(str(topic or "").split())[:40]


def set_topic(conn: sqlite3.Connection, item_id: int, topic: str) -> bool:
    cur = conn.execute("UPDATE items SET topic = ?, updated_at = ? WHERE id = ?", (clean_topic(topic), now_iso(), item_id))
    return cur.rowcount == 1


def topics(conn: sqlite3.Connection, project: str | None = None) -> list[str]:
    """Distinct topics in use on still-owed items, most used first."""
    sql = "SELECT topic, COUNT(*) AS n FROM items WHERE status IN ('open', 'snoozed') AND topic != ''"
    args: tuple = ()
    if project:
        sql += " AND project = ?"
        args = (project,)
    return [r["topic"] for r in conn.execute(sql + " GROUP BY topic ORDER BY n DESC, topic", args)]


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def wake_snoozed(conn: sqlite3.Connection) -> int:
    now = now_iso()
    cur = conn.execute(
        "UPDATE items SET status = 'open', snoozed_until = NULL, updated_at = ?"
        " WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
        (now, now),
    )
    return cur.rowcount


def open_items(conn: sqlite3.Connection, project: str | None = None) -> list[sqlite3.Row]:
    """Open and snoozed items (both count as 'still owed'), oldest first."""
    sql = "SELECT * FROM items WHERE status IN ('open', 'snoozed')"
    args: tuple = ()
    if project:
        sql += " AND project = ?"
        args = (project,)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def inbox(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    wake_snoozed(conn)
    return conn.execute("SELECT * FROM items WHERE status = 'open' ORDER BY created_at DESC, id DESC").fetchall()


def snoozed_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM items WHERE status = 'snoozed' ORDER BY snoozed_until").fetchall()


def recently_resolved(conn: sqlite3.Connection, days: int | None = None) -> list[sqlite3.Row]:
    days = config.RESOLVED_STRIP_DAYS if days is None else days
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(days=days))
    return conn.execute(
        "SELECT * FROM items WHERE status = 'auto_resolved' AND updated_at >= ? ORDER BY updated_at DESC", (cutoff,)
    ).fetchall()


def list_items(conn: sqlite3.Connection, status: str | None = None, project: str | None = None) -> list[sqlite3.Row]:
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if project:
        clauses.append("project = ?")
        args.append(project)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(f"SELECT * FROM items{where} ORDER BY project, id", args).fetchall()


def apply_action(conn: sqlite3.Connection, item_id: int, action: str, until: str | None = None) -> sqlite3.Row | None:
    """Apply a UI action. Every non-undo action saves the prior state so ``undo`` can restore it."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now_iso()
    if action == "undo":
        if not row["prev_status"]:
            return row
        conn.execute(
            "UPDATE items SET status = ?, snoozed_until = ?, prev_status = NULL, prev_snoozed_until = NULL,"
            " updated_at = ? WHERE id = ?",
            (row["prev_status"], row["prev_snoozed_until"], now, item_id),
        )
        return get_item(conn, item_id)
    new_until = None
    if action == "snooze":
        new_until = to_iso(parse_iso(until)) if until else next_snooze_time()
    conn.execute(
        "UPDATE items SET status = ?, snoozed_until = ?, prev_status = ?, prev_snoozed_until = ?, updated_at = ?"
        " WHERE id = ?",
        (_ACTION_STATUS[action], new_until, row["status"], row["snoozed_until"], now, item_id),
    )
    return get_item(conn, item_id)


def append_note(conn: sqlite3.Connection, item_id: int, note: str) -> bool:
    """Add a short note to an item's details unless it is already there."""
    row = get_item(conn, item_id)
    if row is None or note in (row["details"] or ""):
        return False
    details = (row["details"] + " " if row["details"] else "") + note
    conn.execute("UPDATE items SET details = ?, updated_at = ? WHERE id = ?", (details[:1000], now_iso(), item_id))
    return True


def auto_resolve(conn: sqlite3.Connection, ids: list[int], session_id: str) -> list[int]:
    done = []
    now = now_iso()
    for item_id in ids:
        cur = conn.execute(
            "UPDATE items SET status = 'auto_resolved', resolved_by = ?, prev_status = status,"
            " prev_snoozed_until = snoozed_until, snoozed_until = NULL, updated_at = ?"
            " WHERE id = ? AND status IN ('open', 'snoozed')",
            (session_id, now, item_id),
        )
        if cur.rowcount == 1:
            done.append(item_id)
    return done


# ---- sessions ---------------------------------------------------------------


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()


def record_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    transcript_path: str,
    project: str,
    transcript_mtime: float,
    items_added: int = 0,
    items_resolved: int = 0,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, transcript_path, project, transcript_mtime, processed_at, items_added,"
        " items_resolved, cost_usd, input_tokens, output_tokens, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(session_id) DO UPDATE SET transcript_path = excluded.transcript_path,"
        " project = excluded.project, transcript_mtime = excluded.transcript_mtime,"
        " processed_at = excluded.processed_at, items_added = excluded.items_added,"
        " items_resolved = excluded.items_resolved, cost_usd = excluded.cost_usd,"
        " input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, error = excluded.error",
        (
            session_id,
            transcript_path,
            project,
            transcript_mtime,
            now_iso(),
            items_added,
            items_resolved,
            cost_usd,
            input_tokens,
            output_tokens,
            error,
        ),
    )


def sessions(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sessions ORDER BY processed_at DESC LIMIT ?", (limit,)).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")}
    s = conn.execute(
        "SELECT COUNT(*) AS sessions, COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens, SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS failed"
        " FROM sessions"
    ).fetchone()
    return {"items": by_status, **dict(s)}


# ---- meta -------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
