from __future__ import annotations

import logging
import os
from typing import Any

from .base_agent import BaseAgent
from core.core.host_bridge import HostBridge
from core.core.models import AgentHealth, AgentResult, AgentStatus, Task, TaskStatus

logger = logging.getLogger("qt_dev_box_agent")

class QtDevBoxAgent(BaseAgent):
    """
    Specialized agent that executes tasks inside the 'qt-dev-box' distrobox container.
    Handles C++/Qt compilation, builds, and specialized container-bound operations.
    """

    def __init__(self, agent_id: str = "qt-dev-box-worker") -> None:
        super().__init__(agent_id, capabilities=["qt_build", "cpp_compile", "container_exec", "code", "test"])
        self.host_bridge = HostBridge()
        self.container_name = os.getenv("HOST_BRIDGE_GH_DISTROBOX", "qt-dev-box")

    def health(self) -> AgentHealth:
        # Check if container exists and is reachable
        try:
            result = self.host_bridge.execute(["distrobox", "list", "--no-color"])
            if result.returncode == 0 and self.container_name in result.stdout:
                return AgentHealth(
                    agent_id=self.agent_id,
                    status=AgentStatus.READY,
                    capabilities=self.capabilities,
                )
            return AgentHealth(
                agent_id=self.agent_id,
                status=AgentStatus.FAILED,
                capabilities=self.capabilities,
                last_error="container_not_found",
            )
        except Exception as e:
            return AgentHealth(
                agent_id=self.agent_id,
                status=AgentStatus.FAILED,
                capabilities=self.capabilities,
                last_error=str(e),
            )

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        self.active_tasks += 1
        try:
            # If the task is a direct command or code generation, we wrap it
            command = task.input.description
            # Simple heuristic: if it looks like a bash command, run it. 
            # Otherwise, use it as an instruction for internal tools.
            
            # For now, let's treat all 'container_exec' or 'qt_build' tasks as shell commands
            full_cmd = ["distrobox", "enter", self.container_name, "--", "bash", "-c", command]
            
            result = self.host_bridge.execute(full_cmd, timeout=task.context.timeout_sec or 600)
            
            status = TaskStatus.DONE if result.returncode == 0 else TaskStatus.FAILED
            summary = result.stdout if result.returncode == 0 else result.stderr
            
            return self.result(task, summary, status, errors=[result.stderr] if result.returncode != 0 else [])
        except Exception as e:
            self.last_error = str(e)
            return self.result(task, f"Container execution error: {str(e)}", TaskStatus.FAILED, errors=[str(e)])
        finally:
            self.active_tasks = max(0, self.active_tasks - 1)
