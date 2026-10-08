from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from berkeleypulse.config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY,
  origin TEXT NOT NULL,
  external_id TEXT,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  term TEXT,
  syllabus_text TEXT NOT NULL DEFAULT '',
  material_hash TEXT,
  model_json TEXT,
  parsed_with TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  external_id TEXT,
  name TEXT NOT NULL,
  due_at TEXT,
  points REAL,
  description TEXT NOT NULL DEFAULT '',
  group_name TEXT,
  group_weight REAL
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  locator TEXT NOT NULL,
  text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY,
  message_id TEXT NOT NULL UNIQUE,
  from_addr TEXT NOT NULL,
  from_name TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL,
  sent_at TEXT,
  snippet TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  score INTEGER NOT NULL,
  reason TEXT NOT NULL,
  course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
  demo INTEGER NOT NULL DEFAULT 0,
  dismissed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail_prefs (
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  weight INTEGER NOT NULL,
  samples INTEGER NOT NULL,
  PRIMARY KEY (kind, key)
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(starts_at);
CREATE TABLE IF NOT EXISTS hidden_events (
  signature TEXT PRIMARY KEY
);

CREATE INDEX IF NOT EXISTS idx_emails_score ON emails(score, dismissed);
CREATE INDEX IF NOT EXISTS idx_assignments_course ON assignments(course_id);

CREATE TABLE IF NOT EXISTS file_stamps (
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (course_id, external_id)
);

CREATE TABLE IF NOT EXISTS file_bodies (
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY (course_id, external_id)
);

CREATE TABLE IF NOT EXISTS billing (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  customer_id TEXT NOT NULL DEFAULT '',
  subscription_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  price_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  posted_at TEXT,
  updated_at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  starts_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (course_id, external_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / "pulse.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "courses", "hidden", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "courses", "sort_order", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "documents", "stability", "TEXT NOT NULL DEFAULT 'static'")
    _ensure_column(conn, "emails", "user_rating", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "courses", "file_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_course_order(conn)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    names = {row["name"] for row in rows}
    if column not in names:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"])


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def meta_bump(conn: sqlite3.Connection, key: str, amount: int = 1) -> None:
    current = int(meta_get(conn, key, "0") or "0")
    meta_set(conn, key, str(current + amount))


def _ensure_course_order(conn: sqlite3.Connection) -> None:
    if meta_get(conn, "course_order_ready") == "1":
        return
    rows = conn.execute("SELECT id FROM courses ORDER BY code, id").fetchall()
    for index, row in enumerate(rows):
        conn.execute("UPDATE courses SET sort_order = ? WHERE id = ?", (index, row["id"]))
    meta_set(conn, "course_order_ready", "1")
