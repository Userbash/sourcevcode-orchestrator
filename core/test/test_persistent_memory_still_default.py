from core.core.persistent_memory import PersistentMemoryManager


def test_persistent_memory_manager_init():
    m = PersistentMemoryManager()
    assert m is not None



def test_consolidate_episodic_uses_trained_memory_layer(monkeypatch):
    manager = PersistentMemoryManager()
    stored = []

    monkeypatch.setattr(manager, "upsert_session", lambda session_id, agent_id: f"norm-{session_id}")
    monkeypatch.setattr(
        manager,
        "retrieve_memories",
        lambda **kwargs: [
            type("R", (), {"memory_id": 11, "content": {"step": "a"}})(),
            type("R", (), {"memory_id": 12, "content": {"step": "b"}})(),
        ],
    )
    monkeypatch.setattr(
        manager,
        "store_trained_memory",
        lambda **kwargs: stored.append(kwargs) or 99,
    )

    summary = manager.consolidate_episodic(session_id="s1", agent_id="a1", chunk_size=2)

    assert summary is not None
    assert '"memory_count": 2' in summary
    assert stored and stored[0]["memory_domain"] == "episodic_summary"
    assert stored[0]["source_memory_ids"] == [11, 12]
    assert stored[0]["metadata"]["source"] == "consolidate_episodic"



def test_consolidate_successful_task_uses_task_specific_domain(monkeypatch):
    manager = PersistentMemoryManager()
    stored = []

    monkeypatch.setattr(manager, "upsert_session", lambda session_id, agent_id: f"norm-{session_id}")
    monkeypatch.setattr(
        manager,
        "store_trained_memory",
        lambda **kwargs: stored.append(kwargs) or 123,
    )

    summary = manager.consolidate_successful_task(
        session_id="s1",
        agent_id="a1",
        task_type="review",
        summary="review passed",
        source_memory_ids=[7, 8],
        quality_score=0.91,
        metadata={"task_id": "t-1"},
    )

    assert summary is not None
    assert stored and stored[0]["memory_domain"] == "prompt:review"
    assert stored[0]["metadata"]["source"] == "consolidate_successful_task"
    assert stored[0]["metadata"]["task_type"] == "review"
    assert stored[0]["source_memory_ids"] == [7, 8]


from core.core.models import AgentResult, Priority, Task, TaskContext, TaskInput, TaskType, TaskStatus
from core.core.orchestrator import Orchestrator
from core.core.orchestration_config import OrchestrationConfig


def _orch_stub():
    orch = Orchestrator.__new__(Orchestrator)
    orch._training_consolidation_lock = __import__("threading").Lock()
    orch._training_consolidation_queue = []
    orch._training_consolidation_task = None
    orch._training_consolidation_stop = __import__("threading").Event()
    orch.log = lambda *args, **kwargs: None
    orch.orchestration_config = OrchestrationConfig(training_consolidation_interval_sec=300)
    return orch


def test_orchestrator_enqueue_and_flush_training_consolidation(monkeypatch):
    orch = _orch_stub()
    calls = []

    class _MemoryConsolidator:
        def consolidate_successful_task(self, **kwargs):
            calls.append(kwargs)
            return "ok"

    orch.memory_consolidator = _MemoryConsolidator()
    task = Task(
        TaskType.REVIEW,
        TaskInput("review architecture"),
        TaskContext("demo", "/repo/demo", "main"),
        priority=Priority.NORMAL,
        session_id="sess-1",
    )
    result = AgentResult("task-1", "agent-1", TaskStatus.DONE, {"summary": "reviewed"}, 0.95, [], [], None, None)

    orch._enqueue_training_consolidation(task, result)
    assert len(orch._training_consolidation_queue) == 0
    assert calls and calls[0]["task_type"] == "review"
    assert calls[0]["summary"] == "reviewed"
    assert calls[0]["metadata"]["memory_domain"] == "prompt:review"


def test_orchestrator_training_memory_domain_expanded():
    orch = _orch_stub()

    assert orch._training_memory_domain(Task(TaskType.CODE, TaskInput("code"), TaskContext("demo"))) == "prompt:code"
    assert orch._training_memory_domain(Task(TaskType.DOCS, TaskInput("docs"), TaskContext("demo"))) == "prompt:docs"
    assert orch._training_memory_domain(Task(TaskType.RESEARCH, TaskInput("research"), TaskContext("demo"))) == "prompt:research"


from core.agents.planner_agent import PlannerAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent


def test_specialized_agents_include_trusted_trained_memory_in_summary():
    task = Task(TaskType.PLAN, TaskInput("plan"), TaskContext("demo", "/repo/demo", "main"))
    memory_context = {"trained_memory_trusted": True, "trained_memory_brief": "--- TRAINED MEMORY BRIEF (prompt:plan, Top 1) ---\n[Quality: 0.95] [Domain: prompt:plan] [Sources: [1]] use short plans"}

    plan_summary = PlannerAgent().run(task, memory_context=memory_context).output.summary
    review_summary = ReviewerAgent().run(Task(TaskType.REVIEW, TaskInput("review"), TaskContext("demo", "/repo/demo", "main")), memory_context=memory_context).output.summary
    test_summary = TesterAgent().run(Task(TaskType.TEST, TaskInput("test"), TaskContext("demo", "/repo/demo", "main")), memory_context=memory_context).output.summary

    assert "Trained memory used" in plan_summary
    assert "Trained memory used" in review_summary
    assert "Trained memory used" in test_summary



def test_kpi_event_logger_writes_summary_json(tmp_path):
    from core.core.kpi_event_logger import KPIEventLogger

    logger = KPIEventLogger(file_path=tmp_path / "kpi_events.jsonl", summary_path=tmp_path / "kpi_summary.json")
    logger.write({"event_type": "trained_memory", "accepted": 1})
    logger.write_summary({"summary_type": "trained_memory_rejection_summary", "accepted": 3, "rejected": 1, "rejection_rate": 0.25})

    data = __import__("json").loads((tmp_path / "kpi_summary.json").read_text(encoding="utf-8"))
    assert data["summary_type"] == "trained_memory_rejection_summary"
    assert data["rejection_rate"] == 0.25


def test_orchestrator_smoke_writes_task_lifecycle_kpi(tmp_path, monkeypatch):
    import json
    from core.core.kpi_event_logger import KPIEventLogger
    from core.core.kpi_validation import validate_kpi

    monkeypatch.setenv("AI_BRIDGE_KPI_LOG_FILE", str(tmp_path / "kpi_events.jsonl"))
    monkeypatch.setenv("AI_BRIDGE_KPI_REJECTION_SUMMARY_PATH", str(tmp_path / "kpi_summary.json"))

    logger = KPIEventLogger.from_env()
    payload = {
        "event_type": "task_lifecycle",
        "task_id": "smoke-1",
        "task_type": "plan",
        "priority": "normal",
        "status": "done",
        "agent_id": "smoke-agent",
        "provider": "local",
        "model": "synthetic-smoke",
        "fallback_count": 0,
        "fallback_used": False,
        "started_at": "2026-06-12T10:00:00+00:00",
        "finished_at": "2026-06-12T10:00:01+00:00",
        "latency_ms": 1000.0,
        "tokens_used": 1,
        "errors_count": 0,
    }
    logger.write(payload)
    logger.append_fallback(payload)
    report = validate_kpi(kpi_log_path=tmp_path / "kpi_events.jsonl", fallback_path=tmp_path / "task_lifecycle_fallback.jsonl", days=1)

    assert report["task_lifecycle_events"] == 1
    rows = (tmp_path / "kpi_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(row).get("event_type") == "task_lifecycle" for row in rows)


def test_kpi_validation_detects_missing_task_telemetry(tmp_path):
    import json
    from core.core.kpi_validation import validate_kpi

    log = tmp_path / "kpi_events.jsonl"
    log.write_text(json.dumps({"type": "postgres_watchdog", "logged_at": "2026-06-12T10:00:00+00:00"}) + "\n", encoding="utf-8")
    report = validate_kpi(kpi_log_path=log, fallback_path=tmp_path / "task_lifecycle_fallback.jsonl", days=2)

    assert report["task_lifecycle_events"] == 0
    assert "no_task_lifecycle_events" in report["anomalies"]
