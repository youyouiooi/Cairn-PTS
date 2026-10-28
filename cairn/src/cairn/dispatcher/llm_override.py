"""Pure helpers for the server-managed LLM configuration pool.

The dispatcher pulls an ordered list of LLM configurations from the server
(``GET /llm-configs/runtime``) and maps them onto workers. Everything in this
module is a pure function of its inputs so the pool/rotation policy can be
unit-tested without HTTP, Docker, or a running scheduler.

The ``DispatchConfig`` handed in as ``base`` is never mutated: in-flight tasks
hold references to its ``WorkerConfig`` objects and must keep the credentials
they started with. Each refresh builds a new config from that pristine base.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.server.models import LlmConfigPayload

# pi speaks every protocol through a different provider ``api`` value.
PI_API_BY_PROTOCOL: dict[str, str] = {
    "anthropic": "anthropic-messages",
    "openai_responses": "openai-responses",
    "openai_chat": "openai-completions",
    "gemini": "google-generative-ai",
}

CONFIG_FAILURE_COOLDOWN_BASE_SECONDS = 30
CONFIG_FAILURE_COOLDOWN_MAX_SECONDS = 300

# Custom header env var shared by every adapter. The value is a JSON object and
# may contain secrets; adapters must never log it verbatim.
LLM_HTTP_HEADERS_ENV = "CAIRN_LLM_HTTP_HEADERS"
# Name of the per-session header (e.g. ``x-opencode-session``). Only the header
# name travels in the env; adapters fill its value with the current session id.
LLM_SESSION_HEADER_ENV = "CAIRN_LLM_SESSION_HEADER"
# claudecode reads its extra headers from this env var directly.
ANTHROPIC_CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"


@dataclass(frozen=True)
class LlmCandidate:
    """One enabled LLM configuration from the server pool."""

    config_id: int
    name: str
    protocol: str
    base_url: str
    model: str
    small_fast_model: str
    api_key: str
    headers: dict[str, str]
    session_header: str = ""


@dataclass
class LlmApplyResult:
    """A rebuilt DispatchConfig plus per-worker application details."""

    config: DispatchConfig
    applied_workers: list[str] = field(default_factory=list)
    skipped_workers: list[str] = field(default_factory=list)
    applied_config_ids: dict[str, int] = field(default_factory=dict)


def protocol_supported(worker_type: str, protocol: str) -> bool:
    """Whether ``worker_type`` can talk to a provider speaking ``protocol``."""
    if worker_type == "claudecode":
        return protocol == "anthropic"
    if worker_type == "codex":
        return protocol == "openai_responses"
    if worker_type == "pi":
        return protocol in PI_API_BY_PROTOCOL
    return False


def parse_pool(payload: Sequence[LlmConfigPayload]) -> list[LlmCandidate]:
    """Map runtime payloads to candidates, preserving the server order.

    The runtime endpoint already filters disabled rows and requires an API key
    for enabled ones; both checks are repeated defensively so a hand-edited row
    can never blank out a worker's credentials.
    """
    candidates: list[LlmCandidate] = []
    for entry in payload:
        if not entry.enabled or not entry.api_key:
            continue
        candidates.append(
            LlmCandidate(
                config_id=entry.id,
                name=entry.name,
                protocol=entry.protocol,
                base_url=entry.base_url,
                model=entry.model,
                small_fast_model=entry.small_fast_model,
                api_key=entry.api_key,
                headers=dict(entry.headers),
                session_header=entry.session_header,
            )
        )
    return candidates


def pool_fingerprint(pool: Sequence[LlmCandidate]) -> tuple:
    """Cheap change detector for a pool; order sensitive, header order agnostic."""
    return tuple(
        (
            candidate.config_id,
            candidate.protocol,
            candidate.base_url,
            candidate.model,
            candidate.small_fast_model,
            candidate.api_key,
            tuple(sorted(candidate.headers.items())),
            candidate.session_header,
        )
        for candidate in pool
    )


def candidates_for_worker(worker: WorkerConfig, pool: Sequence[LlmCandidate]) -> list[LlmCandidate]:
    """Compatible candidates for ``worker`` in pool order."""
    return [candidate for candidate in pool if protocol_supported(worker.type, candidate.protocol)]


def cooldown_seconds(failures: int) -> float:
    """Exponential backoff after consecutive failures: 30, 60, ... capped at 300."""
    count = max(1, failures)
    return float(min(CONFIG_FAILURE_COOLDOWN_MAX_SECONDS, CONFIG_FAILURE_COOLDOWN_BASE_SECONDS * 2 ** (count - 1)))


def select_candidate(
    worker: WorkerConfig,
    pool: Sequence[LlmCandidate],
    *,
    cooldown_until: Mapping[int, float],
    now: float,
    preferred_id: int | None = None,
) -> LlmCandidate | None:
    """Pick the candidate a worker should use, honouring cooldowns.

    Preference order: the worker's current config when it is still cooling-free,
    then the first available candidate in pool order, then (when every candidate
    is cooling down) the one whose cooldown expires first so it keeps probing.
    """
    candidates = candidates_for_worker(worker, pool)
    if not candidates:
        return None
    if preferred_id is not None:
        for candidate in candidates:
            if candidate.config_id == preferred_id and not _is_cooling(cooldown_until, candidate.config_id, now):
                return candidate
    for candidate in candidates:
        if not _is_cooling(cooldown_until, candidate.config_id, now):
            return candidate
    return _earliest_cooldown(candidates, cooldown_until, now=now)


def next_candidate_after_failure(
    worker: WorkerConfig,
    pool: Sequence[LlmCandidate],
    *,
    failed_id: int,
    cooldown_until: Mapping[int, float],
    now: float,
) -> LlmCandidate | None:
    """Next compatible candidate after ``failed_id`` cooled down.

    Skip the failed config entirely. Prefer the first candidate that is not
    cooling; when every alternative is cooling, return the earliest expiry (it
    is the closest to being probed again). ``None`` when there is no alternative.
    """
    candidates = [
        candidate
        for candidate in candidates_for_worker(worker, pool)
        if candidate.config_id != failed_id
    ]
    if not candidates:
        return None
    for candidate in candidates:
        if not _is_cooling(cooldown_until, candidate.config_id, now):
            return candidate
    return _earliest_cooldown(candidates, cooldown_until, now=now)


def worker_env_overrides(worker: WorkerConfig, candidate: LlmCandidate) -> dict[str, str] | None:
    """Env keys the candidate replaces for this worker, or None when unsupported.

    The dynamic session header travels as a bare header *name* in
    ``CAIRN_LLM_SESSION_HEADER``; adapters compute its value per request. It is
    never written into the static ``CAIRN_LLM_HTTP_HEADERS`` JSON. A stale static
    header with the same (case-insensitive) name is filtered from the static
    headers so it cannot shadow the dynamic value.
    """
    if not protocol_supported(worker.type, candidate.protocol):
        return None
    static_headers = _static_headers(candidate)
    headers_env = _headers_env(static_headers)
    session_overrides = _session_header_override(candidate)

    if worker.type == "claudecode":
        overrides = {
            "ANTHROPIC_BASE_URL": candidate.base_url,
            "ANTHROPIC_MODEL": candidate.model,
            "ANTHROPIC_AUTH_TOKEN": candidate.api_key,
        }
        if candidate.small_fast_model:
            overrides["ANTHROPIC_SMALL_FAST_MODEL"] = candidate.small_fast_model
        if headers_env is not None:
            overrides[ANTHROPIC_CUSTOM_HEADERS_ENV] = "\n".join(
                f"{name}: {value}" for name, value in static_headers.items()
            )
            overrides[LLM_HTTP_HEADERS_ENV] = headers_env
        overrides.update(session_overrides)
        return overrides

    if worker.type == "codex":
        overrides = {
            "CODEX_BASE_URL": candidate.base_url,
            "CODEX_MODEL": candidate.model,
            "OPENAI_API_KEY": candidate.api_key,
        }
        if headers_env is not None:
            overrides[LLM_HTTP_HEADERS_ENV] = headers_env
        overrides.update(session_overrides)
        return overrides

    if worker.type == "pi":
        overrides = {
            "PI_BASE_URL": candidate.base_url,
            "PI_MODEL": candidate.model,
            "PI_API_KEY": candidate.api_key,
            "PI_PROVIDER_API": PI_API_BY_PROTOCOL[candidate.protocol],
        }
        if headers_env is not None:
            overrides[LLM_HTTP_HEADERS_ENV] = headers_env
        overrides.update(session_overrides)
        return overrides

    return None


def apply_pool(
    base: DispatchConfig,
    pool: Sequence[LlmCandidate],
    active_ids: Mapping[str, int],
) -> LlmApplyResult:
    """Rebuild ``base`` with each worker's active candidate applied to its env.

    ``active_ids`` records which config each worker is currently using (from a
    previous rotation); a missing/stale entry falls back to the first compatible
    candidate in pool order. Workers with no compatible candidate (``mock`` or a
    protocol it cannot speak) are passed through untouched and reported as
    skipped. ``base`` itself is left intact.
    """
    now = time.monotonic()
    workers: list[WorkerConfig] = []
    applied_workers: list[str] = []
    skipped_workers: list[str] = []
    applied_config_ids: dict[str, int] = {}

    for worker in base.workers:
        candidate = _candidate_by_id(worker, pool, active_ids.get(worker.name))
        if candidate is None:
            candidate = select_candidate(worker, pool, cooldown_until={}, now=now)
        overrides = None if candidate is None else worker_env_overrides(worker, candidate)
        if candidate is None or overrides is None:
            workers.append(worker)
            skipped_workers.append(worker.name)
            continue
        data = worker.model_dump()
        data["env"] = {**data["env"], **overrides}
        workers.append(WorkerConfig.model_validate(data))
        applied_workers.append(worker.name)
        applied_config_ids[worker.name] = candidate.config_id

    return LlmApplyResult(
        config=base.model_copy(update={"workers": workers}),
        applied_workers=applied_workers,
        skipped_workers=skipped_workers,
        applied_config_ids=applied_config_ids,
    )


def _static_headers(candidate: LlmCandidate) -> dict[str, str]:
    """Candidate headers minus any stale duplicate of the dynamic session header."""
    session_name = candidate.session_header.lower()
    if not session_name:
        return dict(candidate.headers)
    return {
        name: value
        for name, value in candidate.headers.items()
        if name.lower() != session_name
    }


def _headers_env(headers: dict[str, str]) -> str | None:
    if not headers:
        return None
    return json.dumps(headers, ensure_ascii=False)


def _session_header_override(candidate: LlmCandidate) -> dict[str, str]:
    if not candidate.session_header:
        return {}
    return {LLM_SESSION_HEADER_ENV: candidate.session_header}


def _is_cooling(cooldown_until: Mapping[int, float], config_id: int, now: float) -> bool:
    return cooldown_until.get(config_id, 0.0) > now


def _earliest_cooldown(
    candidates: Sequence[LlmCandidate],
    cooldown_until: Mapping[int, float],
    *,
    now: float,
) -> LlmCandidate:
    # Missing cooldowns count as "already expired" (0.0); ties keep pool order.
    return min(
        candidates,
        key=lambda candidate: (cooldown_until.get(candidate.config_id, 0.0), candidates.index(candidate)),
    )


def _candidate_by_id(
    worker: WorkerConfig,
    pool: Sequence[LlmCandidate],
    config_id: int | None,
) -> LlmCandidate | None:
    if config_id is None:
        return None
    for candidate in pool:
        if candidate.config_id == config_id:
            return candidate if protocol_supported(worker.type, candidate.protocol) else None
    return None
