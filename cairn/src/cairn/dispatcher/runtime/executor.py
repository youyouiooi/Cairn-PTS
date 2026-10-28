from __future__ import annotations

import logging
import sys
from typing import Protocol

from cairn.dispatcher.config import ContainerConfig, DispatchConfig, resolve_executor

LOG = logging.getLogger(__name__)


class RuntimeExecutor(Protocol):
    def close(self) -> None: ...
    def container_name(self, project_id: str) -> str: ...
    def ensure_running(self, project_id: str) -> str: ...
    def create_startup_container(self) -> str: ...
    def inspect_state(self, name: str) -> str | None: ...
    def cleanup_completed(self, project_id: str) -> bool: ...
    def cleanup_stopped(self, project_id: str) -> bool: ...
    def cleanup_orphan(self, name: str) -> bool: ...
    def managed_container_names(self) -> list[str]: ...
    def needs_completed_cleanup(self, project_id: str) -> bool: ...
    def needs_orphan_cleanup(self, name: str) -> bool: ...
    def needs_stopped_cleanup(self, project_id: str) -> bool: ...
    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ): ...
    def write_text_file(self, container_name: str, path: str, content: str) -> None: ...
    def remove_container(self, name: str, *, force: bool = True) -> None: ...


def create_executor(config: DispatchConfig) -> RuntimeExecutor:
    mode = resolve_executor(config.runtime.executor)
    LOG.info("runtime executor=%s platform=%s", mode, sys.platform)
    if mode == "local":
        from cairn.dispatcher.runtime.local import LocalExecutor

        return LocalExecutor(config.container)
    warn_if_host_network_on_darwin(config.container, mode)
    from cairn.dispatcher.runtime.containers import ContainerManager

    return ContainerManager(config.container)


def warn_if_host_network_on_darwin(container: ContainerConfig, executor: str) -> None:
    if resolve_executor(executor) != "docker":
        return
    if sys.platform == "darwin" and container.network_mode == "host":
        LOG.warning(
            "container.network_mode=host is the Docker Desktop Linux VM, not the Mac host; "
            "using bridge instead. Set network_mode: bridge to silence this warning."
        )
