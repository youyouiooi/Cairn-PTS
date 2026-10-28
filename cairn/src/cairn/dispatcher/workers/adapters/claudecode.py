from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters._curl import (
    curl_header_fragment,
    expand_env,
    filter_custom_headers,
    parse_custom_headers,
    render_curl_command,
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


def _custom_headers(worker: WorkerConfig) -> tuple[dict[str, str], bool]:
    headers, invalid = parse_custom_headers(worker.env)
    return filter_custom_headers(headers, _BUILTIN_HEADER_NAMES), invalid


def _healthcheck_script(worker: WorkerConfig) -> str:
    headers, _ = _custom_headers(worker)
    return _HEALTHCHECK_SCRIPT.replace("__EXTRA_HEADERS__", curl_header_fragment(headers))


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=0; " + _healthcheck_script(worker)]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=1; " + _healthcheck_script(worker)]

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        env = worker.env
        headers, invalid = _custom_headers(worker)
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
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
