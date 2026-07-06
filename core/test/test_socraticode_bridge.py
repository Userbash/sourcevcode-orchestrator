from __future__ import annotations

from core.core.models import Complexity, Task, TaskContext, TaskInput, TaskType
from core.core.socraticode_bridge import SocratiCodeBridge, SocratiCodeBridgeUnavailable


def _task() -> Task:
    task = Task(
        TaskType.CODE,
        TaskInput("Refactor auth flow and update dependency graph", files=["core/auth.py", "core/session.py"]),
        TaskContext("demo", ".", "main"),
    )
    task.complexity = Complexity.MEDIUM
    task.session_id = "sess-socraticode"
    return task


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.notifications: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.tools = {
            "codebase_status",
            "codebase_search",
            "codebase_context",
            "codebase_context_search",
            "codebase_graph_status",
            "codebase_impact",
            "codebase_symbols",
        }

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        payload = dict(params or {})
        self.requests.append((method, payload))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "socraticode"}}
        if method == "tools/list":
            return {"tools": [{"name": name} for name in sorted(self.tools)]}
        if method != "tools/call":
            raise AssertionError(f"unexpected method {method}")
        name = str(payload.get("name"))
        arguments = payload.get("arguments") or {}
        if name == "codebase_status":
            return {"content": [{"type": "text", "text": "clean worktree on main"}]}
        if name == "codebase_context":
            return {"content": [{"type": "text", "text": "Context Artifacts for: .\n━━━ auth-schema ━━━\n  Status: indexed"}]}
        if name == "codebase_graph_status":
            return {"structuredContent": {"indexed": True, "nodes": 42}, "content": [{"type": "text", "text": "graph indexed with 42 nodes"}]}
        if name == "codebase_search":
            query = str(arguments.get("query") or "")
            return {"content": [{"type": "text", "text": f'Search results for "{query}" (2 matches):\n--- core/auth.py ---\nmatch\n--- core/session.py ---\nmatch'}]}
        if name == "codebase_context_search":
            query = str(arguments.get("query") or "")
            return {"content": [{"type": "text", "text": f'Context search results for "{query}" (1 match):\n--- docs/auth.md ---\ncontext'}]}
        if name == "codebase_impact":
            target = str(arguments.get("target") or "")
            return {"content": [{"type": "text", "text": f"impact touches {target}"}]}
        if name == "codebase_symbols":
            query = str(arguments.get("query") or "")
            return {"content": [{"type": "text", "text": f"symbols for {query}"}]}
        raise AssertionError(f"unexpected tool {name}")

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        self.notifications.append((method, dict(params or {})))

    def close(self) -> None:
        self.closed = True


def test_runtime_env_passes_provider_and_embedding_configuration(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-token")
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-token")

    env = SocratiCodeBridge.runtime_env({"EMBEDDING_PROVIDER": "voyage", "CUSTOM_FLAG": "1"})

    assert env["OPENAI_API_KEY"] == "openai-token"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-token"
    assert env["VOYAGE_API_KEY"] == "voyage-token"
    assert env["EMBEDDING_PROVIDER"] == "voyage"
    assert env["CUSTOM_FLAG"] == "1"


def test_bridge_initializes_and_lists_supported_tools():
    transport = _FakeTransport()
    bridge = SocratiCodeBridge(transport=transport, repo_path=".")

    tools = bridge.available_tools()

    assert tools == sorted(transport.tools)
    assert transport.requests[0][0] == "initialize"
    assert transport.requests[1] == ("tools/list", {})
    assert transport.notifications == [("notifications/initialized", {})]


def test_bridge_dispatches_tool_calls():
    bridge = SocratiCodeBridge(transport=_FakeTransport(), repo_path=".")

    result = bridge.codebase_search(query="auth flow", limit=3)

    assert result.name == "codebase_search"
    assert "Search results" in result.text


def test_bridge_rejects_unsupported_tools():
    transport = _FakeTransport()
    transport.tools.remove("codebase_symbols")
    bridge = SocratiCodeBridge(transport=transport, repo_path=".")

    bridge.connect()

    try:
        bridge.codebase_symbols(query="auth")
    except SocratiCodeBridgeUnavailable as exc:
        assert "unsupported_tool:codebase_symbols" in str(exc)
    else:
        raise AssertionError("expected unsupported tool error")


def test_build_compact_context_assembles_summary():
    bridge = SocratiCodeBridge(transport=_FakeTransport(), repo_path=".")

    context = bridge.build_compact_context(_task(), search_limit=2)

    assert context["repo_path"] == "."
    assert context["query"] == "Refactor auth flow and update dependency graph"
    assert context["tools_used"] == [
        "codebase_status",
        "codebase_graph_status",
        "codebase_search",
        "codebase_context",
        "codebase_context_search",
        "codebase_impact",
        "codebase_impact",
        "codebase_symbols",
    ]
    assert "Task: Refactor auth flow and update dependency graph" in context["text"]
    assert "Status: clean worktree on main" in context["text"]
    assert "Impact[core/auth.py]: impact touches core/auth.py" in context["text"]
    assert "Symbols: symbols for Refactor auth flow and update dependency graph" in context["text"]


def test_analyze_task_derives_cost_and_parallelism_hints():
    bridge = SocratiCodeBridge(transport=_FakeTransport(), repo_path=".")
    task = _task()
    task.routing_hints = {"parallel_branches": 6}

    advisory = bridge.analyze_task(task=task, context={}, description=task.input.description, task_type="code", routing_hints=task.routing_hints)

    assert advisory["context_coverage"]["status"] in {"good", "strong"}
    assert advisory["context_coverage"]["score"] >= 0.72
    assert advisory["cost_downgrade"]["eligible"] is True
    assert advisory["cost_downgrade"]["preferred_provider"] in {"local", "mistral"}
    assert advisory["parallelism"]["recommended_parallel_branches"] == 2
    assert advisory["routing_recommendations"]["prefer_low_cost_lanes"] is True


def test_bridge_closes_transport():
    transport = _FakeTransport()
    bridge = SocratiCodeBridge(transport=transport, repo_path=".")
    bridge.connect()

    bridge.close()

    assert transport.closed is True
    assert bridge.initialized is False
