"""SQLite storage for the society: runs, activities, messages, papers, artifacts, and events."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, question TEXT, status TEXT, config TEXT,
    created_at TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS run_agents (
    run_id INTEGER, agent_id TEXT, paused INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, agent_id)
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY, run_id INTEGER, agent_id TEXT, trigger TEXT, status TEXT,
    prompt_hash TEXT, started_at TEXT, ended_at TEXT, error TEXT, public_update TEXT
);
CREATE TABLE IF NOT EXISTS prompt_versions (hash TEXT PRIMARY KEY, content TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS config_versions (id INTEGER PRIMARY KEY, path TEXT, content TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY, run_id INTEGER, title TEXT, author TEXT, created_at TEXT);
-- A message belongs to a thread (public) or has a recipient (direct message).
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, run_id INTEGER, thread_id INTEGER, author TEXT, recipient TEXT,
    body TEXT, created_at TEXT, idem TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS unread (message_id INTEGER, agent_id TEXT, PRIMARY KEY (message_id, agent_id));

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE, collection TEXT, title TEXT, authors TEXT, year TEXT,
    arxiv_id TEXT, doi TEXT, url TEXT, sha256 TEXT UNIQUE, notes_path TEXT, added_by TEXT, created_at TEXT
);
-- Reading state per agent: notes_pending -> completed. last_page is the furthest page read.
CREATE TABLE IF NOT EXISTS paper_reads (
    paper_id INTEGER, agent_id TEXT, status TEXT, last_page INTEGER, PRIMARY KEY (paper_id, agent_id)
);

-- kind is 'insight' or 'proposal'. Each revision is kept in artifact_versions.
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY, kind TEXT, author TEXT, title TEXT, path TEXT,
    revision INTEGER, forked_from INTEGER, run_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id INTEGER, revision INTEGER, author TEXT, body TEXT, refs TEXT, created_at TEXT, idem TEXT UNIQUE,
    PRIMARY KEY (artifact_id, revision)
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY, run_id INTEGER, author TEXT, activity_id INTEGER, hypothesis TEXT,
    path TEXT, exit_code INTEGER, refs TEXT, created_at TEXT, idem TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY, activity_id INTEGER, agent_id TEXT, tool TEXT, args TEXT, result TEXT,
    started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY, activity_id INTEGER, agent_id TEXT, started_at TEXT, seconds REAL,
    input_tokens INTEGER, output_tokens INTEGER, ok INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, run_id INTEGER, agent_id TEXT, activity_id INTEGER, type TEXT,
    payload TEXT, created_at TEXT
);
-- Full-text search over messages, paper notes, insights, and proposals. ref is e.g. 'message:4'.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(ref UNINDEXED, owner UNINDEXED, title, body);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        # Tools run in worker threads and share this connection. The lock keeps each statement and its
        # result together, so an insert from another thread cannot change the row id we read back.
        self.lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        # Databases created before last_page existed.
        if 'last_page' not in [c['name'] for c in self.all('PRAGMA table_info(paper_reads)')]:
            self.execute('ALTER TABLE paper_reads ADD COLUMN last_page INTEGER')

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, params)

    def insert(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).lastrowid

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def emit(self, type: str, payload: dict, run_id=None, agent_id=None, activity_id=None) -> int:
        """Record an event. The web UI reads events from this table."""
        return self.insert(
            'INSERT INTO events (run_id, agent_id, activity_id, type, payload, created_at) VALUES (?,?,?,?,?,?)',
            (run_id, agent_id, activity_id, type, json.dumps(payload), now()),
        )

    def index(self, ref: str, owner: str | None, title: str, body: str) -> None:
        self.execute('DELETE FROM search WHERE ref = ?', (ref,))
        self.execute('INSERT INTO search (ref, owner, title, body) VALUES (?,?,?,?)', (ref, owner, title, body))

    def search(self, query: str, prefix: str = '', limit: int = 10) -> list[dict]:
        # Quote each word so FTS5 syntax characters in agent queries do not raise errors.
        q = ' '.join('"' + w.replace('"', '') + '"' for w in query.split())
        if not q:
            return []
        return self.all(
            "SELECT ref, owner, title, snippet(search, 3, '[', ']', '...', 20) AS snippet FROM search "
            'WHERE search MATCH ? AND ref LIKE ? ORDER BY rank LIMIT ?',
            (q, prefix + '%', limit),
        )
