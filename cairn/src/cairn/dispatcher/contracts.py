from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object
from cairn.server.models import (
    FINDING_SEVERITIES,
    MAX_FINDING_LONG_FIELD_LENGTH,
    MAX_FINDING_SHORT_FIELD_LENGTH,
    MAX_FINDING_TITLE_LENGTH,
    finding_fingerprint,
)

MAX_OUTPUT_FINDINGS = 50


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload) - {"findings"}
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) - {"findings"} != {"fact", "complete"}:
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload) - {"findings"}
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) - {"findings"} == {"description"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete", "findings"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()


_FINDING_SHORT_TEXT_FIELDS = ("vuln_type", "target", "endpoint", "cve", "cwe")
_FINDING_LONG_TEXT_FIELDS = ("evidence", "impact", "remediation")


def _finding_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finding_severity(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in FINDING_SEVERITIES:
            return text
    return "info"


def validate_findings(raw: Any) -> list[dict[str, Any]]:
    """Best-effort validation of an agent-provided ``findings`` array.

    Worker output quality is not guaranteed, so malformed entries are skipped
    instead of failing the whole task. Only the fields Cairn stores are kept;
    text is stripped and truncated, severity is normalized (unknown -> info)
    and status is always forced to "open" (only humans change status later).
    """

    if not isinstance(raw, list):
        raise ValueError("findings must be an array")
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if len(findings) >= MAX_OUTPUT_FINDINGS:
            break
        if not isinstance(item, dict):
            continue
        title = _finding_text(item.get("title"))
        if not title or len(title) > MAX_FINDING_TITLE_LENGTH:
            continue
        finding: dict[str, Any] = {
            "title": title,
            "severity": _finding_severity(item.get("severity")),
            "status": "open",
        }
        for field in _FINDING_SHORT_TEXT_FIELDS:
            finding[field] = _finding_text(item.get(field))[:MAX_FINDING_SHORT_FIELD_LENGTH]
        for field in _FINDING_LONG_TEXT_FIELDS:
            finding[field] = _finding_text(item.get(field))[:MAX_FINDING_LONG_FIELD_LENGTH]
        fact_id = item.get("fact_id")
        finding["fact_id"] = fact_id.strip() if isinstance(fact_id, str) and fact_id.strip() else None
        fingerprint = finding_fingerprint(
            title,
            finding["target"],
            finding["endpoint"],
            finding["vuln_type"],
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        findings.append(finding)
    return findings


def extract_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract optional structured findings from a worker payload.

    Never raises: an unparseable payload simply means "no findings to write".
    """

    try:
        if not isinstance(payload, dict):
            return []
        accepted = payload.get("accepted")
        if accepted is False:
            return []
        if accepted is True:
            data = payload.get("data")
            if not isinstance(data, dict):
                return []
        else:
            data = payload
        raw = data.get("findings")
        if raw is None:
            return []
        return validate_findings(raw)
    except Exception:
        return []
