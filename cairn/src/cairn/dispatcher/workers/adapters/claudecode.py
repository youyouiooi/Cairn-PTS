from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.llm_override import ANTHROPIC_CUSTOM_HEADERS_ENV
from cairn.dispatcher.workers.adapters._curl import (
    curl_header_fragment,
    expand_env,
    filter_custom_headers,
    parse_custom_headers,
    render_curl_command,
    session_header_name,
    with_session_header,
)
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"
# Headers the healthcheck sends itself; a custom header with the same
# case-insensitive name is ignored (the built-in one wins for the probe).
_BUILTIN_HEADER_NAMES = frozenset({"authorization", "anthropic-version", "content-type"})

_HEALTHCHECK_SCRIPT = """
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
payload=$(printf '{"model":"%s","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}' "$ANTHROPIC_MODEL")
curl_rc=0
http_status=$(curl -sS -o "$body_file" -w '%{http_code}' \
  "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "anthropic-version: __ANTHROPIC_VERSION__" \
  -H "content-type: application/json"__EXTRA_HEADERS__ \
  -d "$payload") || curl_rc=$?
if [ "${CAIRN_HEALTHCHECK_VERBOSE:-0}" = "1" ]; then
  printf 'http_status=%s\\n' "$http_status"
  if [ -s "$body_file" ]; then cat "$body_file"; fi
fi
if [ "$curl_rc" -ne 0 ]; then exit "$curl_rc"; fi
case "$http_status" in 2??) exit 0 ;; *) exit 1 ;; esac
""".replace("__ANTHROPIC_VERSION__", ANTHROPIC_VERSION).strip()


def _request_headers(worker: WorkerConfig, session_value: str) -> tuple[dict[str, str], bool]:
    """Static custom headers plus the dynamic session header for one command."""
    headers, invalid = parse_custom_headers(worker.env)
    headers = filter_custom_headers(headers, _BUILTIN_HEADER_NAMES)
    headers = with_session_header(headers, session_header_name(worker), session_value)
    return headers, invalid


def _headers_string(worker: WorkerConfig, session_value: str) -> str:
    headers, _ = _request_headers(worker, session_value)
    return "\n".join(f"{name}: {value}" for name, value in headers.items())


def _healthcheck_script(worker: WorkerConfig) -> str:
    headers, _ = _request_headers(worker, f"healthcheck-{worker.name}")
    return _HEALTHCHECK_SCRIPT.replace("__EXTRA_HEADERS__", curl_header_fragment(headers))


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=0; " + _healthcheck_script(worker)]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=1; " + _healthcheck_script(worker)]

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        env = worker.env
        headers, invalid = _request_headers(worker, f"healthcheck-{worker.name}")
        custom_arguments: list[str] = []
        for name in headers:
            custom_arguments.extend(["-H", f"{name}: ***"])
        command = render_curl_command(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                expand_env("Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"),
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
                *custom_arguments,
            ],
            payload=(
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        )
        if invalid:
            command += "  # invalid CAIRN_LLM_HTTP_HEADERS ignored"
        return command

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        argv = [
            "claude",
            "--session-id",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
        return DriverResult(argv=_with_session_env(worker, session, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        argv = [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
        return _with_session_env(worker, session, argv)


def _with_session_env(worker: WorkerConfig, session_value: str, argv: list[str]) -> list[str]:
    """Prepend ``env ANTHROPIC_CUSTOM_HEADERS=...`` when a session header is configured.

    Claude Code reads ``ANTHROPIC_CUSTOM_HEADERS`` itself, so overriding it for
    the child process is the documented way to attach per-request headers; the
    value is one ``Name: value`` pair per line and may contain newlines as a
    single argv element. With no session header this returns ``argv`` unchanged.
    """
    if not session_header_name(worker) or not session_value:
        return argv
    merged = _headers_string(worker, session_value)
    return ["env", f"{ANTHROPIC_CUSTOM_HEADERS_ENV}={merged}", *argv]
