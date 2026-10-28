"""Multi-provider LLM configuration pool API.

Browser sessions manage the ordered pool (masked views, never the API key);
the dispatcher reads the full pool over the bearer-token runtime endpoint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cairn.server import auth
from cairn.server.db import get_conn
from cairn.server.models import (
    LLM_PROTOCOLS,
    MAX_LLM_CONFIGS,
    LlmConfigCreate,
    LlmConfigListResponse,
    LlmConfigPayload,
    LlmConfigReorder,
    LlmConfigRuntimeResponse,
    LlmConfigTestRequest,
    LlmConfigTestResponse,
    LlmConfigUpdate,
    LlmConfigView,
    LlmProtocol,
)
from cairn.server.services import utcnow

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["llm-config"])

ANTHROPIC_VERSION = "2023-06-01"
CONNECT_TIMEOUT_SECONDS = 3.05
MAX_PREVIEW_BYTES = 4096
MAX_PREVIEW_CHARS = 512
MAX_ERROR_CHARS = 300


# --- auth channels ------------------------------------------------------------


def _identity(request: Request):
    identity = getattr(request.state, "identity", None)
    if identity is None:
        identity = auth.authenticate_request(request)
    return identity


def _require_session(request: Request):
    identity = _identity(request)
    if identity is None:
        raise HTTPException(401, "Not authenticated")
    if identity.via != "session":
        raise HTTPException(403, "API configuration can only be changed from a logged-in session")
    return identity


def _require_token(request: Request):
    identity = _identity(request)
    if identity is None:
        raise HTTPException(401, "Not authenticated")
    if identity.via != "token":
        raise HTTPException(403, "Runtime configuration is only available to the dispatcher")
    return identity


# --- row helpers --------------------------------------------------------------


def _protocol(value: str) -> LlmProtocol:
    return value if value in LLM_PROTOCOLS else "anthropic"  # type: ignore[return-value]


def _row_headers(row: sqlite3.Row) -> dict[str, str]:
    raw = row["headers"] or "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(name): "" if value is None else str(value) for name, value in parsed.items()}


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "已配置"
    return f"{key[:3]}…{key[-4:]}"


def _view(row: sqlite3.Row) -> LlmConfigView:
    """Browser-facing view. Never contains the API key."""
    key = row["api_key"] or ""
    return LlmConfigView(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        protocol=_protocol(row["protocol"]),
        base_url=row["base_url"],
        model=row["model"],
        small_fast_model=row["small_fast_model"],
        headers=_row_headers(row),
        priority=row["priority"],
        updated_at=row["updated_at"],
        has_api_key=bool(key),
        api_key_hint=_mask(key),
    )


def _payload(row: sqlite3.Row) -> LlmConfigPayload:
    """Dispatcher-facing payload, including the plaintext API key."""
    return LlmConfigPayload(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        protocol=_protocol(row["protocol"]),
        base_url=row["base_url"],
        model=row["model"],
        small_fast_model=row["small_fast_model"],
        headers=_row_headers(row),
        priority=row["priority"],
        updated_at=row["updated_at"],
        api_key=row["api_key"] or "",
    )


def _load_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM llm_configs ORDER BY priority ASC, id ASC"
    ).fetchall()


def _load_one(conn: sqlite3.Connection, config_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM llm_configs WHERE id = ?", (config_id,)).fetchone()


# --- probe helpers ------------------------------------------------------------


def _merge_headers(base: dict[str, str], custom: dict[str, str] | None) -> dict[str, str]:
    """Layer custom request headers over the protocol defaults (case-insensitive)."""
    merged = dict(base)
    for name, value in (custom or {}).items():
        lowered = name.lower()
        for existing in [key for key in merged if key.lower() == lowered]:
            del merged[existing]
        merged[name] = value
    return merged


def _probe(
    protocol: LlmProtocol,
    base_url: str,
    model: str,
    headers: dict[str, str] | None,
    api_key: str,
) -> tuple[str, dict, dict[str, str]]:
    """Return (url, json_body, request_headers) for one protocol."""
    base_headers = {"content-type": "application/json"}
    if protocol == "anthropic":
        url = f"{base_url}/v1/messages"
        body = {
            "model": model,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "ping"}],
        }
        base_headers["Authorization"] = f"Bearer {api_key}"
        base_headers["anthropic-version"] = ANTHROPIC_VERSION
    elif protocol == "openai_responses":
        url = f"{base_url}/responses"
        body = {
            "model": model,
            "input": [{"role": "user", "content": "ping"}],
            "max_output_tokens": 16,
            "stream": False,
        }
        base_headers["Authorization"] = f"Bearer {api_key}"
    elif protocol == "openai_chat":
        url = f"{base_url}/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10,
            "stream": False,
        }
        base_headers["Authorization"] = f"Bearer {api_key}"
    elif protocol == "gemini":
        url = f"{base_url}/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 10},
        }
        base_headers["x-goog-api-key"] = api_key
    else:  # pragma: no cover - Pydantic already restricts the protocol
        raise HTTPException(400, "Unsupported protocol")
    return url, body, _merge_headers(base_headers, headers)


def _redact(text: str, key: str) -> str:
    if key and key in text:
        return text.replace(key, "***")
    return text


def _read_preview(response) -> str:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(1024):
        if not chunk:
            continue
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_PREVIEW_BYTES:
            break
    return b"".join(chunks)[:MAX_PREVIEW_BYTES].decode("utf-8", "replace")


def _execute_probe(
    protocol: LlmProtocol,
    base_url: str,
    model: str,
    headers: dict[str, str] | None,
    api_key: str,
    timeout_seconds: int,
) -> LlmConfigTestResponse:
    url, body, request_headers = _probe(protocol, base_url, model, headers, api_key)
    started = time.perf_counter()
    try:
        response = requests.post(
            url,
            json=body,
            headers=request_headers,
            timeout=(CONNECT_TIMEOUT_SECONDS, timeout_seconds),
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = _redact(str(exc), api_key)[:MAX_ERROR_CHARS] or "request failed"
        return LlmConfigTestResponse(ok=False, latency_ms=latency_ms, error=message)

    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        ok = 200 <= response.status_code < 300
        preview = None
        error = None if ok else f"HTTP {response.status_code}"
        if not ok:
            try:
                preview = _redact(_read_preview(response), api_key)[:MAX_PREVIEW_CHARS]
            except Exception as exc:  # noqa: BLE001 - a broken body read must not 500
                detail = (
                    _redact(str(exc), api_key)[:MAX_ERROR_CHARS]
                    or "failed to read response body"
                )
                error = f"HTTP {response.status_code}: {detail}"
        return LlmConfigTestResponse(
            ok=ok,
            status_code=response.status_code,
            latency_ms=latency_ms,
            error=error,
            response_preview=preview,
        )
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - closing a dead stream must not 500
            pass


# --- endpoints ----------------------------------------------------------------


@router.get(
    "/llm-configs",
    response_model=LlmConfigListResponse,
    dependencies=[Depends(_require_session)],
)
def list_llm_configs():
    """Every configuration (including disabled ones), ordered by priority then id."""
    with get_conn() as conn:
        rows = _load_all(conn)
    return LlmConfigListResponse(configs=[_view(row) for row in rows])


@router.get(
    "/llm-configs/runtime",
    response_model=LlmConfigRuntimeResponse,
    dependencies=[Depends(_require_token)],
)
def get_llm_configs_runtime():
    """Enabled pool in dispatch order, with plaintext keys (dispatcher bearer only)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM llm_configs WHERE enabled = 1 ORDER BY priority ASC, id ASC"
        ).fetchall()
    return LlmConfigRuntimeResponse(configs=[_payload(row) for row in rows])


@router.post(
    "/llm-configs",
    response_model=LlmConfigView,
    status_code=201,
    dependencies=[Depends(_require_session)],
)
def create_llm_config(body: LlmConfigCreate):
    now = utcnow()
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_configs").fetchone()[0]
        if count >= MAX_LLM_CONFIGS:
            raise HTTPException(
                400, f"too many LLM configurations (max {MAX_LLM_CONFIGS})"
            )
        priority = conn.execute(
            "SELECT COALESCE(MAX(priority), -10) + 10 FROM llm_configs"
        ).fetchone()[0]
        try:
            cursor = conn.execute(
                """
                INSERT INTO llm_configs
                    (name, enabled, protocol, base_url, model, small_fast_model,
                     api_key, headers, priority, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.name,
                    int(body.enabled),
                    body.protocol,
                    body.base_url,
                    body.model,
                    body.small_fast_model,
                    body.api_key,
                    json.dumps(body.headers, ensure_ascii=False),
                    priority,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "LLM config name already exists") from None
        row = _load_one(conn, cursor.lastrowid)
    LOG.info(
        "llm config created id=%s name=%s protocol=%s priority=%s",
        cursor.lastrowid,
        body.name,
        body.protocol,
        priority,
    )
    return _view(row)


@router.put(
    "/llm-configs/{config_id}",
    response_model=LlmConfigView,
    dependencies=[Depends(_require_session)],
)
def update_llm_config(config_id: int, body: LlmConfigUpdate):
    now = utcnow()
    with get_conn() as conn:
        row = _load_one(conn, config_id)
        if row is None:
            raise HTTPException(404, "LLM config not found")
        if body.api_key is None:
            if body.base_url != row["base_url"] or body.protocol != row["protocol"]:
                raise HTTPException(400, "base_url/protocol changed; re-enter the API key")
            new_key = row["api_key"] or ""
        else:
            new_key = body.api_key
        if body.enabled and not new_key:
            raise HTTPException(400, "API key is required when enabled")
        try:
            conn.execute(
                """
                UPDATE llm_configs
                SET name = ?, enabled = ?, protocol = ?, base_url = ?, model = ?,
                    small_fast_model = ?, api_key = ?, headers = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    body.name,
                    int(body.enabled),
                    body.protocol,
                    body.base_url,
                    body.model,
                    body.small_fast_model,
                    new_key,
                    json.dumps(body.headers, ensure_ascii=False),
                    now,
                    config_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "LLM config name already exists") from None
        updated = _load_one(conn, config_id)
    LOG.info(
        "llm config updated id=%s name=%s enabled=%s protocol=%s api_key_set=%s",
        config_id,
        body.name,
        body.enabled,
        body.protocol,
        bool(new_key),
    )
    return _view(updated)


@router.delete(
    "/llm-configs/{config_id}",
    status_code=204,
    dependencies=[Depends(_require_session)],
)
def delete_llm_config(config_id: int) -> Response:
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM llm_configs WHERE id = ?", (config_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "LLM config not found")
    LOG.info("llm config deleted id=%s", config_id)
    return Response(status_code=204)


@router.post(
    "/llm-configs/reorder",
    response_model=LlmConfigListResponse,
    dependencies=[Depends(_require_session)],
)
def reorder_llm_configs(body: LlmConfigReorder):
    with get_conn() as conn:
        existing = [row["id"] for row in _load_all(conn)]
        if len(body.ids) != len(existing) or set(body.ids) != set(existing):
            raise HTTPException(400, "ids must cover all configurations")
        for index, config_id in enumerate(body.ids):
            conn.execute(
                "UPDATE llm_configs SET priority = ? WHERE id = ?",
                (index * 10, config_id),
            )
        rows = _load_all(conn)
    LOG.info("llm configs reordered ids=%s", body.ids)
    return LlmConfigListResponse(configs=[_view(row) for row in rows])


@router.post(
    "/llm-configs/test",
    response_model=LlmConfigTestResponse,
    dependencies=[Depends(_require_session)],
)
def test_llm_config(body: LlmConfigTestRequest):
    """Probe a draft configuration without saving anything."""
    api_key = body.api_key or ""
    if not api_key:
        if body.id is None:
            raise HTTPException(400, "API key required")
        with get_conn() as conn:
            row = _load_one(conn, body.id)
        # Only reuse the stored secret for the exact target it was saved with, so
        # a session cannot redirect the key to a host of its choosing.
        if (
            row is None
            or not row["api_key"]
            or row["base_url"] != body.base_url
            or row["protocol"] != body.protocol
        ):
            raise HTTPException(400, "API key required")
        api_key = row["api_key"]
    return _execute_probe(
        body.protocol,
        body.base_url,
        body.model,
        body.headers,
        api_key,
        body.timeout_seconds,
    )
