from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.core.orchestrator import Orchestrator
from core.core.sourcecraft_module import SourceCraftModule


class _FakeAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def log(self, level: str, message: str) -> None:
        self.messages.append((level, message))


def _make_src_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_sourcecraft_module_reports_ready_and_exposes_context(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    api = _FakeAPI()
    module.on_load(api)

    context: dict[str, object] = {}
    task = SimpleNamespace(type=SimpleNamespace(value="code"), input=SimpleNamespace(description="Create a PR for repo release automation"))
    module.before_task(task, context)

    final = module.finalize()

    assert final["status"] == "ready"
    assert final["version"] == "Version: 0.1.2"
    assert final["binary"] == str(src)
    assert final["role"]["name"] == "sourcecraft"
    assert "repository operations" in final["role"]["summary"].lower() or "repository" in final["role"]["summary"].lower()
    assert context["sourcecraft"]["enabled"] is True
    assert context["sourcecraft"]["likely_repo_work"] is True
    assert context["sourcecraft"]["role"]["name"] == "sourcecraft"
    assert context["sourcecraft"]["delegation"]["recommended_owner"] == "sourcecraft"
    assert context["sourcecraft"]["delegation"]["should_delegate"] is True
    assert context["sourcecraft"]["automation"]["owner"] == "sourcecraft"
    assert any("SOURCECRAFT" in message for _, message in api.messages)


def test_sourcecraft_module_gracefully_degrades_when_binary_missing(monkeypatch):
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", "/nonexistent/sourcecraft-src")

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    final = module.finalize()

    assert final["status"] == "error"
    assert final["binary"] is None
    assert "not found" in str(final["last_error"])


def test_sourcecraft_module_builds_delegation_profile():
    from core.core.models import Task, TaskContext, TaskInput, TaskType

    module = SourceCraftModule()
    module._status = "ready"
    task = Task(TaskType.PLAN, TaskInput("Prepare repo status, PR draft, and release notes"), TaskContext("demo", ".", "main"), required_capability="sourcecraft")

    profile = module.build_delegation_profile(task, {"description": task.input.description})

    assert profile["should_delegate"] is True
    assert profile["recommended_owner"] == "sourcecraft"
    assert profile["task_family"] == "repo_ops"
    assert "summarize repository state" in profile["sourcecraft_actions"]


def test_orchestrator_registers_sourcecraft_module():
    orchestrator = Orchestrator()

    assert "sourcecraft" in orchestrator.loaded_kernel_modules()
    state = orchestrator.module_manager.finalize()
    assert "sourcecraft" in state


def test_task_decomposer_auto_marks_sourcecraft_repo_tasks():
    from core.core.models import Task, TaskContext, TaskInput, TaskType
    from core.core.task_decomposer import TaskDecomposer

    task = Task(TaskType.PLAN, TaskInput("Prepare release notes and PR flow for repo status"), TaskContext("demo", ".", "main"))
    plan = TaskDecomposer().decompose(task)

    assert plan.atomic_tasks[0].required_capability == "sourcecraft"
    assert plan.atomic_tasks[1].required_capability == "code"


def test_api_bridge_sourcecraft_delegate_preview():
    from core.core.api_bridge_module import APIBridgeModule, SourceCraftDelegateRequest
    from core.core.models import TaskAcceptance, TaskStatus

    class _Router:
        def route(self, task):
            return TaskAcceptance(task.task_id, TaskStatus.ACCEPTED, "orchestrator", "high", "preview")

    class _Scheduler:
        def schedule(self, task):
            from core.core.models import SchedulerDecision
            return SchedulerDecision(task.task_id, "orchestrator", None, True, "preview", 9.0)

    class _FakeAPI2:
        def __init__(self):
            self.router = _Router()
            self.scheduler = _Scheduler()
            self.sourcecraft = SourceCraftModule()
            self.sourcecraft.on_load(_FakeAPI())

        def get_module(self, name):
            if name == "sourcecraft":
                return self.sourcecraft
            return None

        def get_context(self, key):
            return getattr(self, key, None)

    module = APIBridgeModule()
    module._api = _FakeAPI2()
    response = module._sourcecraft_delegate(SourceCraftDelegateRequest(description="Prepare SourceCraft release notes for repo status", task_type="plan", repo_path=".", branch="main"))

    assert response["status"] == "ok"
    assert response["sourcecraft"]["role"]["name"] == "sourcecraft"
    assert response["delegation"]["recommended_owner"] == "sourcecraft"
    assert response["delegation"]["should_delegate"] is True
    assert response["route"]["assigned_agent"] == "orchestrator"
    assert response["schedule"]["route_mode"] == "orchestrator"


def test_task_decomposer_marks_sourcecraft_dag_nodes_in_context():
    from core.core.models import Priority, SecurityPolicy, TaskPayload, encapsulate
    from core.core.task_decomposer import TaskDecomposer

    payload = TaskPayload(
        objective="Prepare SourceCraft release notes for repo status",
        input_data={"repo": "."},
        context={"branch": "main"},
        acceptance_criteria=["release notes prepared"],
        expected_output_format="json",
    )
    envelope = encapsulate(
        payload,
        {
            "target_capability": "sourcecraft",
            "priority": Priority.NORMAL,
            "security_policy": SecurityPolicy(),
        },
    )

    graph = TaskDecomposer().decompose_to_graph(envelope)

    assert graph.nodes
    assert all(node.payload.context.get("sourcecraft_role") is True for node in graph.nodes.values())
    assert all(node.payload.context.get("sourcecraft_role_name") == "sourcecraft" for node in graph.nodes.values())


def test_task_decomposer_uses_local_llm_layered_draft():
    from core.core.models import Task, TaskContext, TaskInput, TaskType
    from core.core.task_decomposer import TaskDecomposer

    task = Task(TaskType.PLAN, TaskInput("Add Telegram authorization with backend, frontend, tests, and docs"), TaskContext("demo", ".", "main"))
    advisory_context = {
        "local_llm": {
            "decomposition": {
                "status": "model",
                "layers": [
                    {"name": "intake", "objective": "Normalize the request", "capability": "plan", "task_type": "plan", "dependencies": []},
                    {"name": "implementation", "objective": "Implement backend and frontend changes", "capability": "code", "task_type": "code", "dependencies": ["intake"]},
                    {"name": "verification", "objective": "Prepare tests and checks", "capability": "test", "task_type": "test", "dependencies": ["implementation"]},
                ],
            }
        }
    }

    plan = TaskDecomposer().decompose(task, advisory_context=advisory_context)

    assert [atomic.draft_layer for atomic in plan.atomic_tasks] == ["intake", "implementation", "verification"]
    assert plan.draft_layers[0]["name"] == "intake"
    assert plan.atomic_tasks[1].dependencies == [plan.atomic_tasks[0].task_id]


def test_orchestrator_create_execution_plan_uses_local_llm_advisory(monkeypatch):
    from core.core.models import ExecutionPlan, Task, TaskContext, TaskInput, TaskType

    orchestrator = Orchestrator()
    task = Task(TaskType.PLAN, TaskInput("Add Telegram authorization with backend, frontend, tests, and docs"), TaskContext("demo", ".", "main"))

    captured: dict[str, object] = {}

    def fake_decompose(task_obj, advisory_context=None):
        captured["advisory_context"] = advisory_context
        return ExecutionPlan(root_task_id=task_obj.task_id, atomic_tasks=[task_obj], draft_layers=[])

    monkeypatch.setattr(orchestrator.decomposer, "decompose", fake_decompose)
    orchestrator.module_manager = SimpleNamespace(get_module=lambda name: None)
    monkeypatch.setattr(orchestrator, "_build_decomposition_advisory", lambda task_obj: {"local_llm": {"decomposition": {"layers": [{"name": "intake", "objective": "Normalize request", "capability": "plan", "task_type": "plan", "dependencies": []}]}}})

    plan = orchestrator.create_execution_plan(task)

    assert len(plan.atomic_tasks) == 1
    assert "local_llm" in captured["advisory_context"]
    assert captured["advisory_context"]["local_llm"]["decomposition"]["layers"][0]["name"] == "intake"



def test_api_bridge_full_health_snapshot_contains_providers_and_agents():
    from core.core.api_bridge_module import APIBridgeModule

    class _Health:
        def __init__(self, payload):
            self._payload = payload

        def as_dict(self):
            return self._payload

    class _Healthcheck:
        def check_providers(self):
            return {
                "gemini": _Health({"provider": "gemini", "status": "healthy"}),
                "mistral": _Health({"provider": "mistral", "status": "healthy"}),
            }

        def check_all(self):
            return [
                _Health({"agent_id": "codex-main", "status": "ready"}),
                _Health({"agent_id": "tester-1", "status": "ready"}),
            ]

    class _ModuleManager:
        def finalize(self):
            return {"sourcecraft": {"status": "ready"}}

    class _Registry:
        def list_agents(self):
            return [1, 2]

    class _API:
        def __init__(self):
            self.healthcheck = _Healthcheck()
            self.registry = _Registry()
            self.module_manager = _ModuleManager()
            self.sourcecraft = SourceCraftModule()
            self.sourcecraft.on_load(_FakeAPI())

        def get_context(self, key):
            return getattr(self, key, None)

        def get_module(self, name):
            return self.sourcecraft if name == "sourcecraft" else None

    module = APIBridgeModule()
    module._api = _API()
    snapshot = module._health_full_snapshot()

    assert snapshot["status"] == "ok"
    assert snapshot["overall_ok"] is True
    assert snapshot["summary"]["provider_count"] == 2
    assert snapshot["summary"]["agent_count"] == 2
    assert snapshot["sourcecraft"]["role"]["name"] == "sourcecraft"
    assert snapshot["modules"]["sourcecraft"]["status"] == "ready"



def test_sourcecraft_repo_action_requires_mutation_opt_in(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    result = module.execute_repo_action("push_branch", repo_path=".", branch="feature/test")

    assert result["status"] == "rejected"
    assert "allow_mutation" in result["reason"]


def test_sourcecraft_repo_action_dry_run_exposes_git_command(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    result = module.execute_repo_action(
        "merge_branch",
        repo_path=".",
        branch="feature/mimo",
        target_branch="main",
        allow_mutation=True,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["command"] == ["git", "merge", "--no-ff", "feature/mimo"]
    assert result["target_branch"] == "main"


def test_api_bridge_sourcecraft_repo_operation_preview(tmp_path, monkeypatch):
    from core.core.api_bridge_module import APIBridgeModule, SourceCraftRepoRequest

    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    class _API:
        def __init__(self):
            self.sourcecraft = SourceCraftModule()
            self.sourcecraft.on_load(_FakeAPI())

        def get_module(self, name):
            return self.sourcecraft if name == "sourcecraft" else None

    module = APIBridgeModule()
    module._api = _API()
    response = module._sourcecraft_repo(SourceCraftRepoRequest(action="create_branch", repo_path=".", branch="feature/sourcecraft", allow_mutation=True, dry_run=True))

    assert response["status"] == "dry_run"
    assert response["operation"]["command"] == ["git", "checkout", "-b", "feature/sourcecraft"]
    assert response["sourcecraft"]["execution"]["tools"]["src"] is True



def test_sourcecraft_push_branch_uses_dh_runner_in_dry_run(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    result = module.execute_repo_action(
        "push_branch",
        repo_path=".",
        branch="feature/sourcecraft",
        allow_mutation=True,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["runner"] == "dh"
    assert result["command"] == ["dh", "git", "push", "-u", "origin", "feature/sourcecraft"]


def test_sourcecraft_create_pr_uses_src_runner_in_dry_run(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    monkeypatch.setattr(module, "_resolve_repo_slug", lambda repo_path: "Userbash/sourcevcode-orchestrator")
    result = module.execute_repo_action(
        "create_pr",
        repo_path=".",
        branch="feature/sourcecraft",
        target_branch="main",
        allow_mutation=True,
        dry_run=True,
        title="Wire SourceCraft publish flow",
        description="Connect SourceCraft repo actions to kernel execution",
        reviewers=["alice", "bob"],
        draft=True,
    )

    assert result["status"] == "dry_run"
    assert result["runner"] == "src"
    assert result["repo_slug"] == "Userbash/sourcevcode-orchestrator"
    assert result["command"] == [
        "pr",
        "create",
        "-R",
        "Userbash/sourcevcode-orchestrator",
        "--title",
        "Wire SourceCraft publish flow",
        "--base",
        "main",
        "--head",
        "feature/sourcecraft",
        "--description",
        "Connect SourceCraft repo actions to kernel execution",
        "--draft",
        "--reviewer",
        "alice",
        "--reviewer",
        "bob",
    ]



def test_sourcecraft_ensure_ready_reports_runtime_health(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    def fake_run(command, *, repo_path=".", timeout_sec=None):
        joined = " ".join(command)
        if joined == "dh sh -lc command -v gh >/dev/null 2>&1":
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "dh gh auth status":
            return {"ok": True, "stdout": "logged in", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "dh git config --global user.name":
            return {"ok": True, "stdout": "Userbash", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "dh git config --global user.email":
            return {"ok": True, "stdout": "wairuste@gmail.com", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "git remote get-url origin":
            return {"ok": True, "stdout": "https://github.com/Userbash/sourcevcode-orchestrator.git", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "git symbolic-ref --short HEAD":
            return {"ok": True, "stdout": "feat/sourcecraft-runtime-health", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        if joined == "dh gh repo view Userbash/sourcevcode-orchestrator":
            return {"ok": True, "stdout": "repo ok", "stderr": "", "returncode": 0, "command": command, "repo_path": repo_path}
        raise AssertionError(joined)

    monkeypatch.setattr(module, "_run_command", fake_run)
    report = module.ensure_ready(repo_path=".")

    assert report["status"] == "ready"
    assert report["src_ready"] is True
    assert report["ghbox_ready"] is True
    assert report["gh_auth_ready"] is True
    assert report["git_identity"]["name"] == "Userbash"
    assert report["repo_slug"] == "Userbash/sourcevcode-orchestrator"
    assert module.finalize()["runtime"]["status"] == "ready"


def test_sourcecraft_push_branch_requires_preview_token(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    result = module.execute_repo_action(
        "push_branch",
        repo_path=".",
        branch="feat/sourcecraft-runtime-health",
        allow_mutation=True,
        allow_production_repo=True,
    )

    assert result["status"] == "rejected"
    assert "preview_token" in result["reason"]


def test_sourcecraft_prepare_feature_branch_dry_run_builds_safe_workflow(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())

    result = module.execute_repo_action(
        "prepare_feature_branch",
        repo_path=".",
        branch="feat/sourcecraft-runtime-health",
        dry_run=True,
        allow_mutation=True,
    )

    assert result["status"] == "dry_run"
    assert result["runner"] == "workflow"
    assert result["workflow"][0] == ["git", "status", "--short"]
    assert ["git", "fetch", "origin", "main"] in result["workflow"]
    assert result["branch_policy"]["valid"] is True


def test_sourcecraft_build_execution_plan_creates_parallel_repo_and_policy_steps(tmp_path, monkeypatch):
    from core.core.models import Task, TaskContext, TaskInput, TaskType

    src = tmp_path / "src"
    _make_src_script(src, 'echo "Version: 0.1.2"')
    monkeypatch.setenv("SOURCECRAFT_CLI_BIN", str(src))

    module = SourceCraftModule()
    module.on_load(_FakeAPI())
    task = Task(TaskType.PLAN, TaskInput("Prepare repository governance report and PR workflow"), TaskContext("demo", ".", "main"), required_capability="sourcecraft")

    plan = module.build_execution_plan(task)

    assert len(plan.atomic_tasks) >= 4
    assert plan.atomic_tasks[0].required_capability == "sourcecraft"
    capabilities = {item.required_capability for item in plan.atomic_tasks}
    assert "sourcecraft" in capabilities
    assert plan.draft_layers[1]["parallel"] is True


def test_orchestrator_prefers_sourcecraft_execution_plan(monkeypatch):
    from core.core.models import ExecutionPlan, Task, TaskContext, TaskInput, TaskType

    orchestrator = Orchestrator()
    task = Task(TaskType.PLAN, TaskInput("Prepare repository governance report and PR workflow"), TaskContext("demo", ".", "main"), required_capability="sourcecraft")
    expected = ExecutionPlan(root_task_id=task.task_id, atomic_tasks=[task], draft_layers=[{"name": "sourcecraft_runtime"}])

    class _SourceCraft:
        def build_execution_plan(self, task_obj, context=None):
            return expected

    monkeypatch.setattr(orchestrator, "module_manager", SimpleNamespace(get_module=lambda name: _SourceCraft() if name == "sourcecraft" else None))

    plan = orchestrator.create_execution_plan(task)

    assert plan is expected
