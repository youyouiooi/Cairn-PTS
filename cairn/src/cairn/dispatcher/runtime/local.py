from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path, PurePosixPath

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)
EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0


def _default_path() -> str:
    extras = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".npm-global" / "bin"),
    ]
    current = os.environ.get("PATH", "/usr/bin:/bin")
    parts = current.split(":")
    for extra in extras:
        if extra not in parts and Path(extra).is_dir():
            parts.insert(0, extra)
    return ":".join(parts)


class LocalManagedProcess:
    def __init__(self, command: list[str], env: dict[str, str], cwd: str):
        self.command = command
        self.env = env
        self.cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._read_error: str | None = None
        self._done = threading.Event()

    def start(self) -> None:
        merged = os.environ.copy()
        merged["PATH"] = _default_path()
        merged.update(self.env)
        Path(self.cwd).mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=merged,
            cwd=self.cwd,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read_streams, daemon=True)
        self._reader.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._reader is not None
        self._reader.join(timeout=timeout)
        if self._reader.is_alive():
            self._timed_out = True
            self.kill()
            self._reader.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        if self._reader.is_alive():
            if self._returncode is None:
                self._returncode = 137
            self._done.set()
        self._done.wait(timeout=0)
        if self._read_error and not self._stderr:
            self._stderr.append(self._read_error)
        return ProcessResult(
            returncode=self._returncode if self._returncode is not None else 1,
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                proc.kill()
            except OSError:
                return

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _read_streams(self) -> None:
        assert self._proc is not None
        proc = self._proc

        def pump(stream, bucket: list[str]) -> None:
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    bucket.append(chunk.decode("utf-8", errors="replace"))
            except OSError as exc:
                self._read_error = str(exc)
            finally:
                stream.close()

        out_thread = threading.Thread(target=pump, args=(proc.stdout, self._stdout), daemon=True)
        err_thread = threading.Thread(target=pump, args=(proc.stderr, self._stderr), daemon=True)
        out_thread.start()
        err_thread.start()
        self._returncode = proc.wait()
        out_thread.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        err_thread.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        self._done.set()


class LocalExecutor:
    """Host-process executor used on Apple Silicon so Cairn does not need the amd64 Kali image."""

    _PREFIX = "cairn-dispatch-"
    _STARTUP_PREFIX = "cairn-startup-healthcheck-"

    def __init__(self, config: ContainerConfig, root: Path | None = None):
        self._config = config
        self._root = root or (Path.home() / ".local" / "share" / "cairn" / "workspaces")
        self._root.mkdir(parents=True, exist_ok=True)
        self._startup: dict[str, Path] = {}

    def close(self) -> None:
        for name in list(self._startup):
            self.remove_container(name, force=True)

    def container_name(self, project_id: str) -> str:
        sanitized = project_id.replace("/", "-")
        return f"{self._PREFIX}{sanitized}"

    def _workspace(self, name: str) -> Path:
        if name in self._startup:
            return self._startup[name]
        return self._root / name

    def ensure_running(self, project_id: str) -> str:
        name = self.container_name(project_id)
        path = self._workspace(name)
        path.mkdir(parents=True, exist_ok=True)
        git_dir = path / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=path, check=False, capture_output=True)
        LOG.info("using local workspace project=%s path=%s", project_id, path)
        return name

    def create_startup_container(self) -> str:
        name = f"{self._STARTUP_PREFIX}{os.urandom(6).hex()}"
        path = Path("/tmp") / name
        path.mkdir(parents=True, exist_ok=True)
        self._startup[name] = path
        return name

    def inspect_state(self, name: str) -> str | None:
        path = self._workspace(name)
        if path.is_dir():
            return "running"
        return None

    def cleanup_completed(self, project_id: str) -> bool:
        name = self.container_name(project_id)
        if self._config.completed_action == "remove":
            return self._remove_workspace(name)
        LOG.info("keeping local workspace for completed project project=%s", project_id)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        LOG.info("keeping local workspace for stopped project project=%s", project_id)
        return True

    def cleanup_orphan(self, name: str) -> bool:
        return self._remove_workspace(name)

    def managed_container_names(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(path.name for path in self._root.iterdir() if path.name.startswith(self._PREFIX) and path.is_dir())

    def needs_completed_cleanup(self, project_id: str) -> bool:
        if self._config.completed_action != "remove":
            return False
        return self.inspect_state(self.container_name(project_id)) is not None

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self.inspect_state(name) is not None

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalManagedProcess:
        del timeout_seconds, kill_after_seconds
        cwd = str(self._workspace(container_name))
        return LocalManagedProcess(command, env, cwd)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        if ".." in PurePosixPath(path).parts:
            raise ValueError(f"invalid file path: {path}")
        workspace = self._workspace(container_name).resolve()
        snapshot_root = Path("/tmp/cairn-prompts").resolve()
        if path.startswith("/"):
            destination = Path(path)
        else:
            destination = workspace / path
        resolved = destination.resolve()
        allowed = (workspace, snapshot_root)
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise ValueError(f"path escapes workspace: {path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    def remove_container(self, name: str, *, force: bool = True) -> None:
        del force
        self._remove_workspace(name)
        self._startup.pop(name, None)

    def _remove_workspace(self, name: str) -> bool:
        path = self._workspace(name)
        if not path.exists():
            return True
        LOG.info("removing local workspace container=%s path=%s", name, path)
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()
