from __future__ import annotations

import logging
import os

from .base_agent import BaseAgent
from core.core.models import AgentHealth, AgentResult, AgentStatus, Task, TaskStatus

logger = logging.getLogger("qt_dev_box_agent")


class QtDevBoxAgent(BaseAgent):
    """
    Orchestrator-owned adapter for the qt-dev-box container.
    The orchestrator module is the source of truth for repo and container control.
    """

    def __init__(self, agent_id: str = "qt-dev-box-worker") -> None:
        super().__init__(agent_id, capabilities=["qt_build", "cpp_compile", "container_exec", "code", "test"])
        self.container_name = os.getenv("HOST_BRIDGE_GH_DISTROBOX", "qt-dev-box")
        self.repo_path = os.getenv("QT_DEV_BOX_REPO_PATH", "/tmp/Neko_Throne")

    def _module(self):
        api = self.get_api()
        if api is None:
            raise RuntimeError("qt-dev-box agent requires orchestrator API")
        module = api.get_module("qt_dev_box")
        if module is None:
            raise RuntimeError("qt_dev_box module is not loaded in orchestrator")
        return module

    def health(self) -> AgentHealth:
        try:
            module = self._module()
            snapshot = module.health()
            if snapshot.get("ready"):
                return AgentHealth(agent_id=self.agent_id, status=AgentStatus.READY, capabilities=self.capabilities)
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.FAILED, capabilities=self.capabilities, last_error="container_or_repo_not_ready")
        except Exception as e:
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.FAILED, capabilities=self.capabilities, last_error=str(e))

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        self.active_tasks += 1
        try:
            command = task.input.description
            timeout_sec = getattr(task.context, "timeout_sec", None) or 600
            result = self._module().run(command, timeout=timeout_sec)

            status = TaskStatus.DONE if result.returncode == 0 else TaskStatus.FAILED
            summary = result.stdout if result.returncode == 0 else result.stderr
            return self.result(task, summary, status, errors=[result.stderr] if result.returncode != 0 else [])
        except Exception as e:
            self.last_error = str(e)
            return self.result(task, f"Container execution error: {str(e)}", TaskStatus.FAILED, errors=[str(e)])
        finally:
            self.active_tasks = max(0, self.active_tasks - 1)
