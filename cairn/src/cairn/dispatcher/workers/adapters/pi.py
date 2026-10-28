from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver


class PiDriver(WorkerDriver):
    type_name = "pi"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return self._wrap_with_models(
            worker,
            [
                "--provider",
                "cairn",
                "--model",
                env["PI_MODEL"],
                "--mode",
                "json",
                "--session-dir",
                self._session_dir(worker),
                "--no-session",
                "--no-tools",
                "-p",
                "Reply with exactly pong.",
            ],
            enable_tools=False,
            session_value=f"healthcheck-{worker.name}",
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", prompt])
        session_value = session or f"{worker.name}-{uuid4().hex[:12]}"
        return DriverResult(
            argv=self._wrap_with_models(worker, argv, session_value=session_value),
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
            "-p",
            prompt,
        ]
        return self._wrap_with_models(worker, argv, session_value=session)

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    def _wrap_with_models(
        self,
        worker: WorkerConfig,
        pi_argv: list[str],
        *,
        enable_tools: bool = True,
        session_value: str = "",
    ) -> list[str]:
        """Wrap pi with a generated models.json provider definition.

        ``session_value`` becomes the value of the optional session header
        (``CAIRN_LLM_SESSION_HEADER``); the value is passed as a shell argument
        so it never shows up in the static script text or logs.
        """
        script = r"""
agent_dir="$1"
session_value="$2"
shift 2
mkdir -p "$agent_dir/sessions"
python3 - "$agent_dir/models.json" "$session_value" <<'PY'
import json, os, sys
path = sys.argv[1]
session_value = sys.argv[2] if len(sys.argv) > 2 else ""
model = {"id": os.environ["PI_MODEL"], "name": os.environ["PI_MODEL"]}
window = os.environ.get("PI_MODEL_CONTEXT_WINDOW")
if window:
    model["contextWindow"] = int(window)
provider = {
    "baseUrl": os.environ["PI_BASE_URL"],
    "api": os.environ["PI_PROVIDER_API"],
    "apiKey": os.environ["PI_API_KEY"],
    "models": [model],
}
headers = {}
raw_headers = os.environ.get("CAIRN_LLM_HTTP_HEADERS", "")
if raw_headers:
    try:
        parsed_headers = json.loads(raw_headers)
    except ValueError:
        parsed_headers = {}
    if isinstance(parsed_headers, dict):
        headers = dict(parsed_headers)
session_name = os.environ.get("CAIRN_LLM_SESSION_HEADER", "").strip()
if session_name and session_value:
    headers = {
        name: value for name, value in headers.items() if name.lower() != session_name.lower()
    }
    headers[session_name] = session_value
if headers:
    provider["headers"] = headers
payload = {"providers": {"cairn": provider}}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
PY
exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"
"""
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            self._agent_dir(worker),
            session_value,
            *argv,
            *pi_argv,
        ]

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events
