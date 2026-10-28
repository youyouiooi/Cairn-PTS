from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None


def default_path() -> Path:
    env = os.environ.get("CAIRN_DB_PATH", "").strip()
    return Path(env) if env else DEFAULT_DB


def current_path() -> Path | None:
    return _db_path


def reset() -> None:
    """Drop the process-wide DB path so tests can bind a temporary file."""
    global _db_path
    _db_path = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS llm_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    protocol TEXT NOT NULL DEFAULT 'anthropic',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    small_fast_model TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO llm_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS llm_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    protocol TEXT NOT NULL DEFAULT 'anthropic',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    small_fast_model TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    headers TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS llm_configs_name_uniq ON llm_configs(name);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    status TEXT NOT NULL DEFAULT 'open',
    vuln_type TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    cve TEXT NOT NULL DEFAULT '',
    cwe TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    remediation TEXT NOT NULL DEFAULT '',
    fact_id TEXT,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS findings_project_fingerprint_uniq ON findings(project_id, fingerprint);

CREATE INDEX IF NOT EXISTS findings_project_severity_idx ON findings(project_id, severity);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS auth_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    csrf_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_legacy_llm_config(conn: sqlite3.Connection) -> None:
    """Copy the legacy single-row llm_config into llm_configs once.

    The legacy table is kept so an operator can roll back to an older build.
    """
    existing = conn.execute("SELECT COUNT(*) FROM llm_configs").fetchone()[0]
    if existing:
        return
    legacy = conn.execute("SELECT * FROM llm_config WHERE id = 1").fetchone()
    if legacy is None:
        return
    if not (legacy["base_url"] or legacy["model"] or legacy["api_key"]):
        return
    conn.execute(
        """
        INSERT INTO llm_configs
            (name, enabled, protocol, base_url, model, small_fast_model, api_key, headers, priority, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 0, ?)
        """,
        (
            "default",
            int(legacy["enabled"] or 0),
            legacy["protocol"] or "anthropic",
            legacy["base_url"] or "",
            legacy["model"] or "",
            legacy["small_fast_model"] or "",
            legacy["api_key"] or "",
            legacy["updated_at"] or "",
        ),
    )


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_llm_config(conn)
    try:
        os.chmod(_db_path, 0o600)
    except OSError:
        pass


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
