"""Structured vulnerability (findings) board API.

All routes sit behind the global auth middleware, so both the browser session
and the dispatcher bearer token can use them. Writes are deduplicated by a
per-project fingerprint, which keeps agent re-reports idempotent.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Response

from cairn.server.db import get_conn
from cairn.server.models import (
    FINDING_SEVERITIES,
    FINDING_SEVERITY_RANK,
    FINDING_STATUSES,
    CreateFindingsRequest,
    CreateFindingsResponse,
    Finding,
    FindingListResponse,
    UpdateFindingRequest,
)
from cairn.server.services import (
    build_finding_summary,
    finding_to_model,
    get_finding_or_404,
    get_project_or_404,
    upsert_findings,
    utcnow,
    validate_facts_exist,
)

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["findings"])

_PATCHABLE_FIELDS = (
    "title",
    "severity",
    "status",
    "vuln_type",
    "target",
    "endpoint",
    "cve",
    "cwe",
    "evidence",
    "impact",
    "remediation",
    "fact_id",
)


def _severity_sorted(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Critical first, then high/medium/low/info, then oldest first."""
    return sorted(
        rows,
        key=lambda row: (
            FINDING_SEVERITY_RANK.get(row["severity"], len(FINDING_SEVERITY_RANK)),
            row["created_at"],
            row["id"],
        ),
    )


def _text_or_dash(value: str) -> str:
    return value if value else "无"


def _render_markdown(project: sqlite3.Row, rows: list[sqlite3.Row], summary) -> str:
    """Render the human-facing vulnerability report (stable template for e2e)."""
    lines = [
        f"# 漏洞报告：{project['title']}",
        "",
        f"- 项目：{project['id']}（{project['title']}）",
        f"- 状态：{project['status']}",
        f"- 生成时间：{utcnow()}",
        (
            f"- 漏洞总数：{summary.total}（critical {summary.critical} / "
            f"high {summary.high} / medium {summary.medium} / "
            f"low {summary.low} / info {summary.info}）"
        ),
        "",
        "## 概览",
        "",
        "| 严重度 | 数量 | | 状态 | 数量 |",
        "|---|---|---|---|---|",
    ]
    for index, severity in enumerate(FINDING_SEVERITIES):
        severity_count = getattr(summary, severity)
        if index < len(FINDING_STATUSES):
            status = FINDING_STATUSES[index]
            lines.append(
                f"| {severity} | {severity_count} | | {status} | "
                f"{getattr(summary, status)} |"
            )
        else:
            lines.append(f"| {severity} | {severity_count} | | | |")

    lines.extend(["", "## 严重度汇总"])
    if not rows:
        lines.extend(["", "暂无漏洞记录。"])
    for severity in FINDING_SEVERITIES:
        group = [row for row in rows if row["severity"] == severity]
        if not group:
            continue
        lines.extend(["", f"### {severity.capitalize()}"])
        for row in group:
            cve_cwe = (
                " / ".join(part for part in (row["cve"], row["cwe"]) if part) or "无"
            )
            lines.extend(
                [
                    "",
                    f"#### {row['id']} {row['title']}",
                    "",
                    (
                        f"- 严重度：{row['severity']} | 状态：{row['status']} | "
                        f"类型：{_text_or_dash(row['vuln_type'])}"
                    ),
                    f"- 目标：{_text_or_dash(row['target'])}",
                    f"- 入口：{_text_or_dash(row['endpoint'])}",
                    f"- CVE/CWE：{cve_cwe}",
                    f"- 证据：{_text_or_dash(row['evidence'])}",
                    f"- 影响：{_text_or_dash(row['impact'])}",
                    f"- 修复建议：{_text_or_dash(row['remediation'])}",
                ]
            )
    return "\n".join(lines) + "\n"


@router.get("/projects/{project_id}/findings", response_model=FindingListResponse)
def list_findings(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = _severity_sorted(
            conn.execute(
                "SELECT * FROM findings WHERE project_id = ?", (project_id,)
            ).fetchall()
        )
        return FindingListResponse(
            findings=[finding_to_model(row) for row in rows],
            summary=build_finding_summary(rows),
        )


# Declared before the /findings/{finding_id} routes so "export" is never
# swallowed as an id.
@router.get("/projects/{project_id}/findings/export")
def export_findings(project_id: str, format: str = "markdown"):
    if format != "markdown":
        raise HTTPException(400, "Supported formats: markdown")
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        rows = _severity_sorted(
            conn.execute(
                "SELECT * FROM findings WHERE project_id = ?", (project_id,)
            ).fetchall()
        )
        text = _render_markdown(project, rows, build_finding_summary(rows))
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}-findings.md"'
        },
    )


@router.post(
    "/projects/{project_id}/findings",
    response_model=CreateFindingsResponse,
    status_code=201,
)
def create_findings(project_id: str, body: CreateFindingsRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        try:
            rows, created, updated = upsert_findings(conn, project_id, body.findings)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        LOG.info(
            "findings upserted project=%s worker=%s created=%s updated=%s",
            project_id,
            body.worker,
            created,
            updated,
        )
        return CreateFindingsResponse(
            findings=[finding_to_model(row) for row in rows],
            created=created,
            updated=updated,
        )


@router.patch(
    "/projects/{project_id}/findings/{finding_id}",
    response_model=Finding,
)
def update_finding(project_id: str, finding_id: str, body: UpdateFindingRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_finding_or_404(conn, project_id, finding_id)
        updates = body.model_dump(exclude_unset=True)
        # Explicit nulls are ignored except for fact_id, which clears the link.
        fields = [
            name
            for name in _PATCHABLE_FIELDS
            if name in updates
            and (updates[name] is not None or name == "fact_id")
        ]
        if not fields:
            return finding_to_model(row)
        new_fact_id = updates.get("fact_id")
        if "fact_id" in fields and new_fact_id is not None:
            validate_facts_exist(conn, project_id, [new_fact_id])
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = [updates[name] for name in fields]
        params.extend([utcnow(), finding_id, project_id])
        conn.execute(
            f"UPDATE findings SET {assignments}, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            params,
        )
        updated_row = get_finding_or_404(conn, project_id, finding_id)
        LOG.info(
            "finding updated project=%s id=%s fields=%s",
            project_id,
            finding_id,
            fields,
        )
        return finding_to_model(updated_row)


@router.delete("/projects/{project_id}/findings/{finding_id}", status_code=204)
def delete_finding(project_id: str, finding_id: str) -> Response:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        get_finding_or_404(conn, project_id, finding_id)
        conn.execute(
            "DELETE FROM findings WHERE id = ? AND project_id = ?",
            (finding_id, project_id),
        )
    LOG.info("finding deleted project=%s id=%s", project_id, finding_id)
    return Response(status_code=204)
