from __future__ import annotations

from core.core.local_llm_module import LocalLLMModule


class _Response:
    def __init__(self, status_code: int = 200, payload: dict[str, object] | None = None, content: bytes = b"{}"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self) -> dict[str, object]:
        return self._payload


class _Api:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def log(self, level: str, message: str) -> None:
        self.messages.append((level, message))

    def get_context(self, key: str):
        return None

    def emit_event(self, event_name: str, payload: dict[str, object]) -> None:
        return None

    def query_module_state(self, module_name: str, key: str):
        return None

    def get_memory(self):
        return None


def test_local_llm_module_reports_ready_when_model_is_available(monkeypatch):
    def fake_get(url: str, timeout: float):
        assert url == "http://host.containers.internal:11434/api/tags"
        assert timeout == 1.0
        return _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)

    module = LocalLLMModule()
    api = _Api()
    module.on_load(api)

    assert module.finalize()["status"] == "ready"
    assert module.finalize()["service_reachable"] is True
    assert module.finalize()["model_present"] is True
    assert any("reachable and ready" in msg for _, msg in api.messages)




def test_local_llm_module_can_use_model_reports_readiness(monkeypatch):
    def fake_get(url: str, timeout: float):
        assert url == "http://host.containers.internal:11434/api/tags"
        return _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)

    module = LocalLLMModule()
    probe = module.can_use_model()

    assert probe["ok"] is True
    assert probe["service_reachable"] is True
    assert probe["model_present"] is True
    assert probe["model_name"] == "qwen2.5:32b-instruct-q4_k_m"
def test_local_llm_module_reports_degraded_when_model_missing(monkeypatch):
    monkeypatch.setattr(
        "core.core.local_llm_module.requests.get",
        lambda url, timeout: _Response(payload={"models": [{"name": "llama3:latest"}]}),
    )

    module = LocalLLMModule()
    result = module.check_health()

    assert result["ok"] is True
    assert result["model_present"] is False
    assert module.finalize()["status"] == "degraded"


def test_local_llm_module_builds_advisory_and_uses_query(monkeypatch):
    from core.core.models import Task, TaskContext, TaskInput, TaskType

    def fake_get(url: str, timeout: float):
        assert url == "http://host.containers.internal:11434/api/tags"
        return _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]})

    def fake_post(url: str, json: dict[str, object], timeout: float):
        assert url == "http://host.containers.internal:11434/api/generate"
        assert json["model"] == "qwen2.5:32b-instruct-q4_k_m"
        return _Response(payload={"response": '{"summary": "condensed", "context_digest": "short", "next_steps": ["step 1"], "model_hint": "local-small"}'})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)
    monkeypatch.setattr("core.core.local_llm_module.requests.post", fake_post)

    module = LocalLLMModule()
    task = Task(TaskType.DOCS, TaskInput("Draft the release notes and summarize the docs changes"), TaskContext("demo", ".", "main"))

    advisory = module.build_advisory(task, {"description": task.input.description})

    assert advisory["ready"] is True
    assert advisory["should_delegate"] is True
    assert advisory["recommended_owner"] == "local_llm"
    assert advisory["summary"] == "condensed"
    assert advisory["context_digest"] == "short"
    assert advisory["next_steps"] == ["step 1"]


def test_local_llm_module_builds_layered_decomposition_draft(monkeypatch):
    from core.core.models import Task, TaskContext, TaskInput, TaskType

    def fake_get(url: str, timeout: float):
        assert url == "http://host.containers.internal:11434/api/tags"
        return _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]})

    def fake_post(url: str, json: dict[str, object], timeout: float):
        assert url == "http://host.containers.internal:11434/api/generate"
        return _Response(payload={"response": '{"summary": "layered", "context_digest": "layered short", "next_steps": ["intake", "analysis"], "model_hint": "local-small", "layers": [{"name": "intake", "objective": "Normalize the request", "capability": "plan", "task_type": "plan", "dependencies": []}, {"name": "analysis", "objective": "Map implementation surfaces", "capability": "research", "task_type": "research", "dependencies": ["intake"]}], "agent_map": {"planner": ["intake"], "research": ["analysis"]}, "sub_agents": ["planner", "research"]}'})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)
    monkeypatch.setattr("core.core.local_llm_module.requests.post", fake_post)

    module = LocalLLMModule()
    task = Task(TaskType.PLAN, TaskInput("Add Telegram authorization with backend, frontend, and tests"), TaskContext("demo", ".", "main"))

    advisory = module.build_decomposition_draft(task, {"description": task.input.description})

    assert advisory["ready"] is True
    assert advisory["decomposition"]["status"] == "model"
    assert [layer["name"] for layer in advisory["decomposition"]["layers"]] == ["intake", "analysis"]
    assert advisory["decomposition"]["agent_map"]["planner"] == ["intake"]


def test_local_llm_query_supports_model_and_system_contract(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get(url: str, timeout: float):
        return _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]})

    def fake_post(url: str, json: dict[str, object], timeout: float):
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response(payload={"response": "ok"})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)
    monkeypatch.setattr("core.core.local_llm_module.requests.post", fake_post)

    module = LocalLLMModule()
    result = module.query(
        "summarize changes",
        "qwen2.5:32b-instruct-q4_k_m",
        system="kernel helper",
        options={"temperature": 0.1},
        timeout_sec=12.0,
    )

    assert result == "ok"
    assert captured["json"] == {
        "model": "qwen2.5:32b-instruct-q4_k_m",
        "prompt": "summarize changes",
        "stream": False,
        "system": "kernel helper",
        "options": {"temperature": 0.1},
    }
    assert captured["timeout"] == 12.0


def test_local_llm_module_query_uses_requested_model(monkeypatch):
    calls = {}

    def fake_get(url: str, timeout: float):
        return _Response(payload={"models": [{"name": "custom-local"}]})

    def fake_post(url: str, json: dict[str, object], timeout: float):
        calls["model"] = json["model"]
        calls["system"] = json["system"]
        return _Response(payload={"response": "ok"})

    monkeypatch.setattr("core.core.local_llm_module.requests.get", fake_get)
    monkeypatch.setattr("core.core.local_llm_module.requests.post", fake_post)

    module = LocalLLMModule(model_name="custom-local")
    response = module.query("ping", model_name="custom-local", system="sys")

    assert response == "ok"
    assert calls["model"] == "custom-local"
    assert calls["system"] == "sys"


def test_local_llm_advisory_exposes_preferred_model(monkeypatch):
    monkeypatch.setattr("core.core.local_llm_module.requests.get", lambda url, timeout: _Response(payload={"models": [{"name": "qwen2.5:32b-instruct-q4_k_m"}]}))

    module = LocalLLMModule()
    from core.core.models import Task, TaskContext, TaskInput, TaskType
    task = Task(TaskType.DOCS, TaskInput("Draft docs summary"), TaskContext("demo", ".", "main"))

    advisory = module.build_advisory(task, {"description": task.input.description})

    assert advisory["preferred_model"] == module.model_name
    assert advisory["recommended_model"] == module.model_name

