from __future__ import annotations

from core.agents.qt_dev_box_agent import QtDevBoxAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


class DummyResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DummyModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def health(self):
        self.calls.append(("health", None))
        return {"ready": True, "repo_path": "/tmp/Neko_Throne"}

    def run(self, command: str, *, timeout: int = 600):
        self.calls.append(("run", command))
        return DummyResult(stdout=f"ran:{command}")


class DummyAPI:
    def __init__(self, module: DummyModule) -> None:
        self.module = module

    def get_module(self, name: str):
        return self.module if name == "qt_dev_box" else None


def _task(description: str = "pwd") -> Task:
    return Task(
        type=TaskType.CODE,
        input=TaskInput(description=description),
        context=TaskContext(project="demo", repo_path=".", branch="main"),
    )


def test_qt_dev_box_agent_uses_orchestrator_module_for_health(monkeypatch):
    monkeypatch.delenv("QT_DEV_BOX_REPO_PATH", raising=False)
    agent = QtDevBoxAgent()
    module = DummyModule()
    agent.set_api(DummyAPI(module))

    health = agent.health()

    assert agent.repo_path == "/tmp/Neko_Throne"
    assert health.status.value == "ready"
    assert module.calls == [("health", None)]


def test_qt_dev_box_agent_uses_orchestrator_module_for_execution(monkeypatch):
    monkeypatch.setenv("QT_DEV_BOX_REPO_PATH", "/tmp/Neko_Throne")
    agent = QtDevBoxAgent()
    module = DummyModule()
    agent.set_api(DummyAPI(module))

    result = agent.run(_task("git status --short"))

    assert module.calls[-1] == ("run", "git status --short")
    assert result.output.summary == "ran:git status --short"
