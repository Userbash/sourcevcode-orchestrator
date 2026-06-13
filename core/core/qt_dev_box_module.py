from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kernel_api import KernelAPI


@dataclass(slots=True)
class QtDevBoxModule:
    name: str = "qt_dev_box"
    container_name: str = field(default_factory=lambda: os.getenv("HOST_BRIDGE_GH_DISTROBOX", "qt-dev-box"))
    repo_path: str = field(default_factory=lambda: os.getenv("QT_DEV_BOX_REPO_PATH", str(Path("/tmp") / Path(__file__).resolve().parents[2].name)))
    _api: KernelAPI | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        if api:
            api.log("info", f"[QT_DEV_BOX] module loaded for {self.container_name} -> {self.repo_path}")

    def on_unload(self) -> None:
        self._api = None

    def health(self) -> dict[str, Any]:
        assert self._api is not None, "KernelAPI not attached"
        host_bridge = getattr(self._api, "host_bridge")
        result = host_bridge.execute(["distrobox", "list", "--no-color"])
        repo_check = host_bridge.execute([
            "distrobox",
            "enter",
            self.container_name,
            "--",
            "sh",
            "-lc",
            f'test -d "{self.repo_path}"',
        ])
        return {
            "container": self.container_name in result.stdout and result.returncode == 0,
            "repo": repo_check.returncode == 0,
            "ready": result.returncode == 0 and self.container_name in result.stdout and repo_check.returncode == 0,
            "repo_path": self.repo_path,
            "container_name": self.container_name,
        }

    def run(self, command: str, *, timeout: int = 600) -> Any:
        assert self._api is not None, "KernelAPI not attached"
        host_bridge = getattr(self._api, "host_bridge")
        full_cmd = [
            "distrobox",
            "enter",
            self.container_name,
            "--",
            "bash",
            "-lc",
            f'cd "{self.repo_path}" && {command}',
        ]
        return host_bridge.execute(full_cmd, timeout=timeout)

    def clone_repo(self, url: str, *, timeout: int = 600) -> Any:
        assert self._api is not None, "KernelAPI not attached"
        host_bridge = getattr(self._api, "host_bridge")
        full_cmd = [
            "distrobox",
            "enter",
            self.container_name,
            "--",
            "bash",
            "-lc",
            f'cd /tmp && rm -rf "{self.repo_path.rsplit("/", 1)[-1]}" && git clone "{url}" "{self.repo_path}"',
        ]
        return host_bridge.execute(full_cmd, timeout=timeout)
