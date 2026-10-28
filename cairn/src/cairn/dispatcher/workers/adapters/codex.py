from __future__ import annotations

import json

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters._curl import (
    curl_header_fragment,
    expand_env,
    filter_custom_headers,
    parse_custom_headers,
    render_curl_command,
)
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver

# Headers the healthcheck sends itself; a custom header with the same
# case-insensitive name is ignored (the built-in one wins for the probe).
_BUILTIN_HEADER_NAMES = frozenset({"authorization", "content-type"})

_HEALTHCHECK_SCRIPT = """
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
payload=$(printf '{"input":[{"content":"ping","role":"user"}],"model":"%s","stream":false}' "$CODEX_MODEL")
curl_rc=0
http_status=$(curl -sS -o "$body_file" -w '%{http_code}' \
  "$CODEX_BASE_URL/responses" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "content-type: application/json"__EXTRA_HEADERS__ \
  -d "$payload") || curl_rc=$?
if [ "${CAIRN_HEALTHCHECK_VERBOSE:-0}" = "1" ]; then
  printf 'http_status=%s\\n' "$http_status"
  if [ -s "$body_file" ]; then cat "$body_file"; fi
fi
if [ "$curl_rc" -ne 0 ]; then exit "$curl_rc"; fi
case "$http_status" in 2??) exit 0 ;; *) exit 1 ;; esac
""".strip()


def _custom_headers(worker: WorkerConfig) -> tuple[dict[str, str], bool]:
    headers, invalid = parse_custom_headers(worker.env)
    return filter_custom_headers(headers, _BUILTIN_HEADER_NAMES), invalid


def _healthcheck_script(worker: WorkerConfig) -> str:
    headers, _ = _custom_headers(worker)
    return _HEALTHCHECK_SCRIPT.replace("__EXTRA_HEADERS__", curl_header_fragment(headers))


def _http_headers_config_argument(headers: dict[str, str]) -> str:
    entries = ", ".join(
        f"{json.dumps(name)} = {json.dumps(value)}" for name, value in headers.items()
    )
    return f"model_providers.cairn.http_headers={{ {entries} }}"


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=0; " + _healthcheck_script(worker)]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return ["/bin/sh", "-lc", "CAIRN_HEALTHCHECK_VERBOSE=1; " + _healthcheck_script(worker)]

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        headers, invalid = _custom_headers(worker)
        custom_arguments: list[str] = []
        for name in headers:
            custom_arguments.extend(["-H", f"{name}: ***"])
        command = render_curl_command(
            self._healthcheck_url(worker),
            headers=[
                "-H",
                expand_env("Authorization: Bearer $OPENAI_API_KEY"),
                "-H",
                "content-type: application/json",
                *custom_arguments,
            ],
            payload=self._healthcheck_payload(worker),
        )
        if invalid:
            command += "  # invalid CAIRN_LLM_HTTP_HEADERS ignored"
        return command

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        env = worker.env
        argv = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
        ]
        self._append_http_headers(argv, worker)
        argv.extend(["--", prompt])
        return DriverResult(argv=argv)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        env = worker.env
        argv = [
            "codex",
            "exec",
            "resume",
            session,
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
        ]
        self._append_http_headers(argv, worker)
        argv.extend(["--", prompt])
        return argv

    @staticmethod
    def _append_http_headers(argv: list[str], worker: WorkerConfig) -> None:
        """Add the TOML inline table for custom headers right before ``--``."""
        headers, _ = _custom_headers(worker)
        if not headers:
            return
        argv.extend(["-c", _http_headers_config_argument(headers)])

    @staticmethod
    def _healthcheck_url(worker: WorkerConfig) -> str:
        return f"{worker.env['CODEX_BASE_URL']}/responses"

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        return (
            '{"input":[{"content":"ping","role":"user"}],'
            '"model":"'
            + worker.env["CODEX_MODEL"]
            + '","stream":false}'
        )
