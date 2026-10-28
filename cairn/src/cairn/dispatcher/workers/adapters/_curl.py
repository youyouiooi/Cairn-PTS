from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Mapping

from cairn.dispatcher.llm_override import LLM_HTTP_HEADERS_ENV


_VERBOSE_CURL_SCRIPT = """
url="$1"
payload="$2"
shift 2

body_file="$(mktemp)"
cleanup() {
    rm -f "$body_file"
}
trap cleanup EXIT

curl_rc=0
http_status="$(
    curl -sS -o "$body_file" -w '%{http_code}' "$url" "$@" -d "$payload"
)" || curl_rc=$?

printf 'http_status=%s\\n' "$http_status"
if [ -s "$body_file" ]; then
    cat "$body_file"
fi

if [ "$curl_rc" -ne 0 ]; then
    exit "$curl_rc"
fi

case "$http_status" in
    2??) exit 0 ;;
    *) exit 1 ;;
esac
""".strip()


def build_verbose_curl_healthcheck(url: str, *, headers: list[str], payload: str) -> list[str]:
    return ["/bin/sh", "-lc", _VERBOSE_CURL_SCRIPT, "--", url, payload, *headers]


def parse_custom_headers(env: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """Parse ``CAIRN_LLM_HTTP_HEADERS`` (JSON object) from a worker env.

    Returns ``(headers, invalid)``. A missing/empty value yields ``({}, False)``;
    malformed JSON or non-string entries yield ``(valid-subset, True)`` so
    callers can still send what they can parse and mention the problem.
    """
    raw = env.get(LLM_HTTP_HEADERS_ENV, "")
    if not raw:
        return {}, False
    try:
        value = json.loads(raw)
    except ValueError:
        return {}, True
    if not isinstance(value, dict):
        return {}, True
    headers: dict[str, str] = {}
    invalid = False
    for name, header_value in value.items():
        if not isinstance(name, str) or not isinstance(header_value, str):
            invalid = True
            continue
        headers[name] = header_value
    return headers, invalid


def curl_header_fragment(headers: Mapping[str, str]) -> str:
    """Shell fragment appending ``-H`` arguments to a /bin/sh curl invocation.

    The caller keeps a literal ``\\`` + newline after the placeholder so the
    fragment continues the same logical command; values are ``shlex.quote``d so
    arbitrary header values can never escape into extra shell words.
    """
    return "".join(
        f" \\\n  -H {shlex.quote(f'{name}: {value}')}" for name, value in headers.items()
    )


def filter_custom_headers(
    headers: Mapping[str, str],
    builtin_names: frozenset[str],
) -> dict[str, str]:
    """Drop custom headers that would duplicate a built-in header (case-insensitive)."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in builtin_names
    }


@dataclass(frozen=True, slots=True)
class ShellArgument:
    text: str
    expand_env: bool = False


def expand_env(text: str) -> ShellArgument:
    return ShellArgument(text=text, expand_env=True)


def render_curl_command(url: str, *, headers: list[str | ShellArgument], payload: str) -> str:
    parts = ["curl", "-sS", url, *headers, "-d", payload]
    return " ".join(_render_shell_argument(part) for part in parts)


def _render_shell_argument(part: str | ShellArgument) -> str:
    if isinstance(part, ShellArgument):
        if part.expand_env:
            return _double_quote_with_env_expansion(part.text)
        return shlex.quote(part.text)
    return shlex.quote(part)


def _double_quote_with_env_expansion(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'
