from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import (
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    MAX_FINDINGS_PER_PROJECT,
    Finding,
    FindingInput,
    FindingSummary,
    Intent,
    ProjectMeta,
    ProjectReason,
    finding_fingerprint,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


# --- findings -----------------------------------------------------------------


def next_finding_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "finding", "v", project_id)


def finding_to_model(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        severity=row["severity"],
        status=row["status"],
        vuln_type=row["vuln_type"],
        target=row["target"],
        endpoint=row["endpoint"],
        cve=row["cve"],
        cwe=row["cwe"],
        evidence=row["evidence"],
        impact=row["impact"],
        remediation=row["remediation"],
        fact_id=row["fact_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def build_finding_summary(rows: list[sqlite3.Row]) -> FindingSummary:
    severity_counts = {name: 0 for name in FINDING_SEVERITIES}
    status_counts = {name: 0 for name in FINDING_STATUSES}
    for row in rows:
        severity = row["severity"]
        if severity in severity_counts:
            severity_counts[severity] += 1
        status = row["status"]
        if status in status_counts:
            status_counts[status] += 1
    return FindingSummary(
        total=len(rows), **severity_counts, **status_counts
    )


def get_finding_or_404(
    conn: sqlite3.Connection, project_id: str, finding_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM findings WHERE id = ? AND project_id = ?",
        (finding_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Finding not found")
    return row


def upsert_findings(
    conn: sqlite3.Connection, project_id: str, items: list[FindingInput]
) -> tuple[list[sqlite3.Row], int, int]:
    """Create or update findings, deduplicated by fingerprint within a project.

    Returns (affected rows ordered by id, created count, updated count). A human
    status (anything other than the default "open") is never overwritten by an
    agent re-reporting the same finding.
    """
    fact_ids: list[str] = []
    for item in items:
        if item.fact_id and item.fact_id not in fact_ids:
            fact_ids.append(item.fact_id)
    if fact_ids:
        validate_facts_exist(conn, project_id, fact_ids)

    existing_rows = conn.execute(
        "SELECT * FROM findings WHERE project_id = ?", (project_id,)
    ).fetchall()
    by_fingerprint = {row["fingerprint"]: row for row in existing_rows}
    total = len(existing_rows)
    created = 0
    updated = 0
    touched: set[str] = set()
    now = utcnow()

    for item in items:
        fingerprint = finding_fingerprint(
            item.title, item.target, item.endpoint, item.vuln_type
        )
        existing = by_fingerprint.get(fingerprint)
        if existing is None:
            if total + 1 > MAX_FINDINGS_PER_PROJECT:
                raise ValueError(
                    f"too many findings for project (max {MAX_FINDINGS_PER_PROJECT})"
                )
            finding_id = next_finding_id(conn, project_id)
            conn.execute(
                """
                INSERT INTO findings
                    (id, project_id, title, severity, status, vuln_type, target,
                     endpoint, cve, cwe, evidence, impact, remediation, fact_id,
                     fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id,
                    project_id,
                    item.title,
                    item.severity,
                    item.status,
                    item.vuln_type,
                    item.target,
                    item.endpoint,
                    item.cve,
                    item.cwe,
                    item.evidence,
                    item.impact,
                    item.remediation,
                    item.fact_id,
                    fingerprint,
                    now,
                    now,
                ),
            )
            total += 1
            created += 1
        else:
            finding_id = existing["id"]
            # A triaged status is human input; only "open" is agent-overwritable.
            status = existing["status"]
            if status == "open":
                status = item.status
            conn.execute(
                """
                UPDATE findings
                SET title = ?, severity = ?, status = ?, vuln_type = ?, target = ?,
                    endpoint = ?, cve = ?, cwe = ?, evidence = ?, impact = ?,
                    remediation = ?, fact_id = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    item.title,
                    item.severity,
                    status,
                    item.vuln_type,
                    item.target,
                    item.endpoint,
                    item.cve,
                    item.cwe,
                    item.evidence,
                    item.impact,
                    item.remediation,
                    item.fact_id,
                    now,
                    finding_id,
                    project_id,
                ),
            )
            updated += 1
        touched.add(finding_id)
        refreshed = conn.execute(
            "SELECT * FROM findings WHERE id = ? AND project_id = ?",
            (finding_id, project_id),
        ).fetchone()
        assert refreshed is not None
        by_fingerprint[fingerprint] = refreshed

    rows = conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()
    return [row for row in rows if row["id"] in touched], created, updated
