from __future__ import annotations

import hashlib
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int
    finding_count: int = 0


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


# --- LLM / API configuration -------------------------------------------------

LlmProtocol = Literal["anthropic", "openai_responses", "openai_chat", "gemini"]

LLM_PROTOCOLS: tuple[str, ...] = ("anthropic", "openai_responses", "openai_chat", "gemini")

MAX_LLM_CONFIGS = 50
MAX_LLM_NAME_LENGTH = 64
MAX_LLM_HEADERS = 20
MAX_LLM_HEADER_VALUE_LENGTH = 1024
MAX_LLM_HEADERS_TOTAL_LENGTH = 8192
MAX_LLM_SESSION_HEADER_LENGTH = 64

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Header names the LLM config may not repurpose as the per-session header:
# these carry credentials, protocol metadata, or transport semantics.
_RESERVED_SESSION_HEADERS = frozenset(
    {
        "authorization",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
        "x-api-key",
        "x-goog-api-key",
        "anthropic-version",
    }
)


def normalize_llm_name(value: str) -> str:
    """Validate and normalize a user-facing configuration name."""
    text = (value or "").strip()
    if not text:
        raise ValueError("name is required")
    if len(text) > MAX_LLM_NAME_LENGTH:
        raise ValueError(f"name is too long (max {MAX_LLM_NAME_LENGTH})")
    return text


def normalize_llm_headers(value: dict[str, str] | None) -> dict[str, str]:
    """Validate and normalize custom request headers.

    Names must be HTTP token characters (RFC 7230); values must be free of
    control characters. This keeps the dispatcher's shell/TOML injection safe.
    """
    if not value:
        return {}
    if len(value) > MAX_LLM_HEADERS:
        raise ValueError(f"too many headers (max {MAX_LLM_HEADERS})")
    normalized: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    for raw_name, raw_value in value.items():
        name = (raw_name or "").strip()
        if not name or not _HEADER_NAME_RE.match(name):
            raise ValueError(f"invalid header name: {name[:64]!r}")
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(f"duplicate header name: {name}")
        seen.add(lowered)
        text = "" if raw_value is None else str(raw_value).strip()
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise ValueError(f"header {name!r} contains control characters")
        if len(text) > MAX_LLM_HEADER_VALUE_LENGTH:
            raise ValueError(f"header {name!r} value is too long (max {MAX_LLM_HEADER_VALUE_LENGTH})")
        total += len(name) + len(text)
        normalized[name] = text
    if total > MAX_LLM_HEADERS_TOTAL_LENGTH:
        raise ValueError(f"headers are too large in total (max {MAX_LLM_HEADERS_TOTAL_LENGTH} bytes)")
    return normalized


def normalize_llm_session_header(value: str | None) -> str:
    """Validate the name of the per-session header.

    The configured header is sent with every request, but its value is filled
    in by the dispatcher with the current session id (never stored here). An
    empty value disables the header.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_LLM_SESSION_HEADER_LENGTH:
        raise ValueError(f"session_header is too long (max {MAX_LLM_SESSION_HEADER_LENGTH})")
    if not _HEADER_NAME_RE.match(text):
        raise ValueError(f"invalid session_header name: {text!r}")
    if text.lower() in _RESERVED_SESSION_HEADERS:
        raise ValueError(f"session_header {text!r} is reserved")
    return text


def _validate_llm_model_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = (value or "").strip()
    if len(text) > 256:
        raise ValueError("model name is too long (max 256)")
    return text


def _validate_llm_api_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) > 4096:
        raise ValueError("api_key is too long (max 4096)")
    return text


def normalize_llm_base_url(value: str) -> str:
    """Validate and normalize a provider base URL.

    Empty strings are allowed here so a disabled config can be stored without a
    URL; callers that require one (enabled config, connection test) check that
    separately.
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an http(s) URL without credentials, query, or fragment")
    return text


class LlmConfigEntry(BaseModel):
    """One LLM configuration as stored by the server (no API key)."""

    id: int
    name: str
    enabled: bool = True
    protocol: LlmProtocol = "anthropic"
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    session_header: str = ""
    priority: int = 0
    updated_at: str = ""


class LlmConfigPayload(LlmConfigEntry):
    """Full configuration, including the API key. Dispatcher (bearer) only."""

    api_key: str = ""


class LlmConfigView(LlmConfigEntry):
    """Browser-facing configuration. Never contains the API key."""

    has_api_key: bool = False
    api_key_hint: str = ""


class LlmConfigListResponse(BaseModel):
    configs: list[LlmConfigView]


class LlmConfigRuntimeResponse(BaseModel):
    """Ordered pool consumed by the dispatcher (contains plaintext keys)."""

    configs: list[LlmConfigPayload]


class LlmConfigCreate(BaseModel):
    name: str
    enabled: bool = True
    protocol: LlmProtocol = "anthropic"
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    session_header: str = ""
    api_key: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_llm_name(value)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_llm_base_url(value)

    @field_validator("model", "small_fast_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        return _validate_llm_model_name(value) or ""

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        return _validate_llm_api_key(value) or ""

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_llm_headers(value)

    @field_validator("session_header")
    @classmethod
    def validate_session_header(cls, value: str) -> str:
        return normalize_llm_session_header(value)

    @model_validator(mode="after")
    def validate_session_header_not_static(self) -> "LlmConfigCreate":
        if self.session_header and any(
            name.lower() == self.session_header.lower() for name in self.headers
        ):
            raise ValueError("session_header must not also be listed in headers")
        return self

    @model_validator(mode="after")
    def validate_enabled_fields(self) -> "LlmConfigCreate":
        if self.enabled:
            if not self.base_url:
                raise ValueError("base_url is required when enabled")
            if not self.model:
                raise ValueError("model is required when enabled")
            if not self.api_key:
                raise ValueError("api_key is required when enabled")
        return self


class LlmConfigUpdate(BaseModel):
    """Full replacement of the mutable fields of one configuration."""

    name: str
    enabled: bool
    protocol: LlmProtocol
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    session_header: str = ""
    # None = keep the stored key, "" = clear it, any other value = replace it.
    api_key: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_llm_name(value)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_llm_base_url(value)

    @field_validator("model", "small_fast_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        return _validate_llm_model_name(value) or ""

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str | None) -> str | None:
        return _validate_llm_api_key(value)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_llm_headers(value)

    @field_validator("session_header")
    @classmethod
    def validate_session_header(cls, value: str) -> str:
        return normalize_llm_session_header(value)

    @model_validator(mode="after")
    def validate_session_header_not_static(self) -> "LlmConfigUpdate":
        if self.session_header and any(
            name.lower() == self.session_header.lower() for name in self.headers
        ):
            raise ValueError("session_header must not also be listed in headers")
        return self

    @model_validator(mode="after")
    def validate_enabled_fields(self) -> "LlmConfigUpdate":
        if self.enabled:
            if not self.base_url:
                raise ValueError("base_url is required when enabled")
            if not self.model:
                raise ValueError("model is required when enabled")
        return self


class LlmConfigReorder(BaseModel):
    ids: list[int]

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("ids must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("ids must be unique")
        return value


class LlmConfigTestRequest(BaseModel):
    """Probe a draft (or stored) configuration without saving it."""

    id: int | None = None
    protocol: LlmProtocol
    base_url: str
    model: str
    small_fast_model: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    session_header: str = ""
    api_key: str | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=30)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_llm_base_url(value)

    @field_validator("model", "small_fast_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        return _validate_llm_model_name(value) or ""

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str | None) -> str | None:
        return _validate_llm_api_key(value)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_llm_headers(value)

    @field_validator("session_header")
    @classmethod
    def validate_session_header(cls, value: str) -> str:
        return normalize_llm_session_header(value)

    @model_validator(mode="after")
    def validate_session_header_not_static(self) -> "LlmConfigTestRequest":
        if self.session_header and any(
            name.lower() == self.session_header.lower() for name in self.headers
        ):
            raise ValueError("session_header must not also be listed in headers")
        return self

    @model_validator(mode="after")
    def validate_required_fields(self) -> "LlmConfigTestRequest":
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.model:
            raise ValueError("model is required")
        return self


class LlmConfigTestResponse(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int = 0
    error: str | None = None
    response_preview: str | None = None


# --- Findings (structured vulnerability records) -----------------------------

FindingSeverity = Literal["critical", "high", "medium", "low", "info"]
FindingStatus = Literal["open", "confirmed", "fixed", "false_positive"]

FINDING_SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
FINDING_STATUSES: tuple[str, ...] = ("open", "confirmed", "fixed", "false_positive")

# Order used by the UI table: critical first, info last.
FINDING_SEVERITY_RANK: dict[str, int] = {name: index for index, name in enumerate(FINDING_SEVERITIES)}

MAX_FINDINGS_PER_PROJECT = 500
MAX_FINDINGS_PER_REQUEST = 100
MAX_FINDING_TITLE_LENGTH = 300
MAX_FINDING_SHORT_FIELD_LENGTH = 512
MAX_FINDING_LONG_FIELD_LENGTH = 4000

_FINDING_WHITESPACE_RE = re.compile(r"\s+")

_FINDING_SHORT_FIELDS = ("vuln_type", "target", "endpoint", "cve", "cwe")
_FINDING_LONG_FIELDS = ("evidence", "impact", "remediation")


def normalize_finding_severity(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text not in FINDING_SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(FINDING_SEVERITIES)}")
    return text


def normalize_finding_status(value: str | None) -> str:
    text = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text not in FINDING_STATUSES:
        raise ValueError(f"status must be one of {', '.join(FINDING_STATUSES)}")
    return text


def _normalize_finding_text(value: str | None, *, limit: int) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise ValueError(f"finding text is too long (max {limit})")
    return text


def _normalize_finding_title(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("title is required")
    if len(text) > MAX_FINDING_TITLE_LENGTH:
        raise ValueError(f"title is too long (max {MAX_FINDING_TITLE_LENGTH})")
    return text


def _normalize_finding_fact_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def finding_fingerprint(title: str, target: str, endpoint: str, vuln_type: str) -> str:
    """Stable dedupe key for one vulnerability inside a project.

    Whitespace is collapsed and case ignored so that agents re-reporting the
    same finding (from a fresh session or a re-run) update one row instead of
    creating duplicates.
    """
    parts = []
    for value in (title, target, endpoint, vuln_type):
        text = _FINDING_WHITESPACE_RE.sub(" ", (value or "").strip().lower())
        parts.append(text)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


class Finding(BaseModel):
    """One structured vulnerability record stored for a project."""

    id: str
    project_id: str
    title: str
    severity: FindingSeverity = "info"
    status: FindingStatus = "open"
    vuln_type: str = ""
    target: str = ""
    endpoint: str = ""
    cve: str = ""
    cwe: str = ""
    evidence: str = ""
    impact: str = ""
    remediation: str = ""
    fact_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


class FindingInput(BaseModel):
    """A finding as reported by a worker or entered in the UI."""

    title: str
    severity: FindingSeverity = "info"
    status: FindingStatus = "open"
    vuln_type: str = ""
    target: str = ""
    endpoint: str = ""
    cve: str = ""
    cwe: str = ""
    evidence: str = ""
    impact: str = ""
    remediation: str = ""
    fact_id: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _normalize_finding_title(value)

    @field_validator("severity", mode="before")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        return normalize_finding_severity(value)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, value: str) -> str:
        return normalize_finding_status(value)

    @field_validator(*_FINDING_SHORT_FIELDS)
    @classmethod
    def validate_short_field(cls, value: str) -> str:
        return _normalize_finding_text(value, limit=MAX_FINDING_SHORT_FIELD_LENGTH)

    @field_validator(*_FINDING_LONG_FIELDS)
    @classmethod
    def validate_long_field(cls, value: str) -> str:
        return _normalize_finding_text(value, limit=MAX_FINDING_LONG_FIELD_LENGTH)

    @field_validator("fact_id")
    @classmethod
    def validate_fact_id(cls, value: str | None) -> str | None:
        return _normalize_finding_fact_id(value)


class CreateFindingsRequest(BaseModel):
    findings: list[FindingInput] = Field(min_length=1, max_length=MAX_FINDINGS_PER_REQUEST)
    worker: str | None = None


class UpdateFindingRequest(BaseModel):
    """PATCH-style update; unset fields keep their stored value."""

    title: str | None = None
    severity: FindingSeverity | None = None
    status: FindingStatus | None = None
    vuln_type: str | None = None
    target: str | None = None
    endpoint: str | None = None
    cve: str | None = None
    cwe: str | None = None
    evidence: str | None = None
    impact: str | None = None
    remediation: str | None = None
    fact_id: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_finding_title(value)

    @field_validator("severity", mode="before")
    @classmethod
    def validate_severity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_finding_severity(value)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_finding_status(value)

    @field_validator(*_FINDING_SHORT_FIELDS)
    @classmethod
    def validate_short_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_finding_text(value, limit=MAX_FINDING_SHORT_FIELD_LENGTH)

    @field_validator(*_FINDING_LONG_FIELDS)
    @classmethod
    def validate_long_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_finding_text(value, limit=MAX_FINDING_LONG_FIELD_LENGTH)

    @field_validator("fact_id")
    @classmethod
    def validate_fact_id(cls, value: str | None) -> str | None:
        return _normalize_finding_fact_id(value)


class FindingSummary(BaseModel):
    total: int = 0
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0
    open: int = 0
    confirmed: int = 0
    fixed: int = 0
    false_positive: int = 0


class FindingListResponse(BaseModel):
    findings: list[Finding]
    summary: FindingSummary


class CreateFindingsResponse(BaseModel):
    findings: list[Finding]
    created: int = 0
    updated: int = 0

