from __future__ import annotations

import json

from core.core.experience_training_pipeline import ExperienceTrainingPipeline
from core.core.local_llm_module import LocalLLMModule


class DummyRecord:
    def __init__(
        self,
        *,
        task_type: str,
        memory_domain: str,
        summary: str,
        model_name: str,
        provider: str,
        quality_score: float,
        problem: str = "Stabilize routing for a medium-sized repository change.",
        constraints: list[str] | None = None,
        files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        failure_mode: str = "skipping verification after edits",
        reuse_hint: str = "Reuse when files and constraints overlap materially.",
    ) -> None:
        self.memory_domain = memory_domain
        self.content = {
            "task_type": task_type,
            "summary": summary,
            "problem": problem,
            "constraints": constraints or ["Preserve existing behavior", "Keep diff reviewable"],
            "files": files or ["core/core/orchestrator.py", "core/core/hybrid_memory.py"],
            "acceptance_criteria": acceptance_criteria or ["Tests pass", "No regression in routing"],
            "failure_mode": failure_mode,
            "reuse_hint": reuse_hint,
            "outcome": "done",
        }
        self.metadata = {
            "task_type": task_type,
            "model_name": model_name,
            "provider": provider,
            "source": "consolidate_successful_task",
        }
        self.quality_score = quality_score
        self.source_memory_ids = [1, 2]


class DummyPersistent:
    def __init__(self, records):
        self._records = records

    def list_trained_memories(self, limit: int = 200):
        return self._records[:limit]


def _set_training_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_SUMMARY_CHARS", "48")
    monkeypatch.setenv("AI_BRIDGE_TRAINED_MEMORY_MIN_QUALITY", "0.55")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_SAMPLES", "8")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_SIGNAL_SCORE", "0.60")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_UNIQUE_TERMS", "6")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_DISTINCT_PATTERNS", "4")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_EFFECTIVE_SAMPLES", "5.6")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_SUCCESS_RATE", "0.70")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_TASK_QUALITY", "0.72")
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_MODEL_SUPPORT", "4")


def test_experience_training_pipeline_writes_dataset_and_adapter_state(tmp_path, monkeypatch):
    _set_training_env(monkeypatch)
    records = [
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Prefer smaller safe refactors, preserve public interfaces, and expand regression tests before touching the parser entrypoints.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.96),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Capture constraints first, split risky edits into phases, and keep a validation checklist tied to the touched files.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.93),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Update tests immediately after a behavior-preserving refactor so edge cases stay pinned while internal names move.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.95),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Stage the rollout with explicit acceptance criteria and keep each commit narrowly scoped to one intent.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.92),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="When the task spans orchestrator and memory layers, verify the same scenario from both routing and retrieval entrypoints.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.94),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Document invariants before editing shared code paths, then validate the same user flow after each phase of cleanup.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.95),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Treat risky edits as staged changes with focused regression coverage so contracts stay visible while internals evolve.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.94),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Use test-first checks to confirm contract behavior before cleanup and keep each refactor tied to one acceptance path.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.93),
        DummyRecord(task_type="docs", memory_domain="prompt:docs", summary="Keep the user-facing documentation concise, concrete, and synchronized with the exact command surface that changed.", model_name="qwen-2.5-7b-instruct", provider="local", quality_score=0.91),
    ]
    pipeline = ExperienceTrainingPipeline(
        dataset_path=tmp_path / "experience_sft_dataset.jsonl",
        adapter_state_path=tmp_path / "experience_adapter_state.json",
        policy_weights_path=tmp_path / "experience_policy_weights.json",
    )

    result = pipeline.train(
        persistent=DummyPersistent(records),
        rolling_kpi_path=tmp_path / "missing_kpi.json",
        runtime_snapshot={"local_llm_ready": True, "ai_kernel_enabled": True, "provider_inventory_ready": True},
        repo_path="/repo",
        branch="main",
    )

    assert result["status"] == "trained"
    assert result["records"] == 9
    rows = [json.loads(row) for row in (tmp_path / "experience_sft_dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 9
    assert rows[0]["signal_score"] >= 0.60
    assert rows[0]["evidence_weight"] > 0.0
    assert rows[0]["task_family"] == "implementation"
    assert rows[0]["constraint_count"] >= 1
    assert rows[0]["acceptance_criteria_count"] >= 1
    assert "Acceptance criteria:" in rows[0]["prompt"]
    adapter = json.loads((tmp_path / "experience_adapter_state.json").read_text(encoding="utf-8"))
    task_board = json.loads((tmp_path / "experience_training_task_board.json").read_text(encoding="utf-8"))
    assert adapter["task_profiles"]["code"]["training_ready"] is True
    assert adapter["task_profiles"]["code"]["preferred_model"] == "qwen2.5:32b-instruct-q4_k_m"
    assert adapter["task_profiles"]["code"]["learning_samples"] == 8
    assert adapter["task_profiles"]["docs"]["task_family"] == "docs_workflow"
    assert adapter["task_profiles"]["docs"]["training_ready"] is False
    assert adapter["task_profiles"]["docs"]["training_stage"] == "collecting"
    assert adapter["training_supervisor"]["primary"]["owner"] == "local_llm"
    assert adapter["training_datasets"]["separated"] is True
    assert task_board["training_supervisor"]["primary"]["owner"] == "local_llm"
    assert result["training_task_board_path"].endswith("experience_training_task_board.json")


def test_experience_training_pipeline_defers_preferred_model_until_min_samples(tmp_path, monkeypatch):
    _set_training_env(monkeypatch)
    records = [
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Prefer smaller safe refactors, preserve public interfaces, and expand regression tests before touching the parser entrypoints.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.96),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Capture constraints first, split risky edits into phases, and keep a validation checklist tied to the touched files.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.93),
    ]
    pipeline = ExperienceTrainingPipeline(
        dataset_path=tmp_path / "experience_sft_dataset.jsonl",
        adapter_state_path=tmp_path / "experience_adapter_state.json",
        policy_weights_path=tmp_path / "experience_policy_weights.json",
    )

    pipeline.train(persistent=DummyPersistent(records), rolling_kpi_path=tmp_path / "missing_kpi.json", runtime_snapshot={"local_llm_ready": False, "ai_kernel_enabled": False, "provider_inventory_ready": False})

    adapter = json.loads((tmp_path / "experience_adapter_state.json").read_text(encoding="utf-8"))
    profile = adapter["task_profiles"]["code"]
    assert profile["learning_samples"] == 2
    assert profile["training_ready"] is False
    assert profile["preferred_model"] == ""
    assert profile["recommended_model"] == ""
    assert profile["samples_needed"] == 6


def test_experience_training_pipeline_filters_duplicate_and_low_signal_rows(tmp_path, monkeypatch):
    _set_training_env(monkeypatch)
    records = [
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Prefer smaller safe refactors, preserve public interfaces, and expand regression tests before touching the parser entrypoints.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.96),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="Prefer smaller safe refactors, preserve public interfaces, and expand regression tests before touching the parser entrypoints.", model_name="qwen2.5:32b-instruct-q4_k_m", provider="local", quality_score=0.97),
        DummyRecord(
            task_type="code",
            memory_domain="prompt:code",
            summary="Safe refactor done.",
            model_name="qwen2.5:32b-instruct-q4_k_m",
            provider="local",
            quality_score=0.94,
            problem="",
            constraints=[],
            files=[],
            acceptance_criteria=[],
            failure_mode="",
            reuse_hint="",
        ),
    ]
    pipeline = ExperienceTrainingPipeline(
        dataset_path=tmp_path / "experience_sft_dataset.jsonl",
        adapter_state_path=tmp_path / "experience_adapter_state.json",
        policy_weights_path=tmp_path / "experience_policy_weights.json",
    )

    result = pipeline.train(persistent=DummyPersistent(records), rolling_kpi_path=tmp_path / "missing_kpi.json")

    assert result["dataset_examples"] == 1
    rows = [json.loads(row) for row in (tmp_path / "experience_sft_dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["summary"].startswith("Prefer smaller safe refactors")


def test_local_llm_module_uses_experience_adapter_state(tmp_path):
    adapter_path = tmp_path / "experience_adapter_state.json"
    adapter_path.write_text(json.dumps({
        "task_profiles": {
            "code": {
                "preferred_model": "qwen2.5:32b-instruct-q4_k_m",
                "recommended_model": "qwen2.5:32b-instruct-q4_k_m",
                "delegate": True,
                "context_depth": 4,
                "profile_weights": {"quality": 1.3},
                "best_practices": ["always preserve tests"],
            }
        }
    }, ensure_ascii=True), encoding="utf-8")

    module = LocalLLMModule(model_name="qwen2.5:32b-instruct-q4_k_m")
    module.adapter_state_path = adapter_path
    module.can_use_model = lambda model_name=None: {"ok": True, "status": "ok", "model_present": True}
    task = type("Task", (), {
        "type": type("T", (), {"value": "code"})(),
        "complexity": type("C", (), {"value": "medium"})(),
        "priority": type("P", (), {"value": "normal"})(),
        "input": type("I", (), {"description": "refactor parser", "files": [], "constraints": []})(),
    })()

    advisory = module.build_decomposition_draft(task, {"description": "refactor parser"})

    assert advisory["preferred_model"] == "qwen2.5:32b-instruct-q4_k_m"
    assert advisory["context_depth"] == 4
    assert advisory["profile_weights"]["quality"] == 1.3
    assert "always preserve tests" in advisory["actions"]


class DummyMemoryRow:
    def __init__(self, *, memory_type: str, content: dict[str, object], agent_id: str = "qwen2.5:32b-instruct-q4_k_m") -> None:
        self.memory_type = memory_type
        self.content = content
        self.agent_id = agent_id


class DummyPersistentWithKPI(DummyPersistent):
    def __init__(self, records, memories):
        super().__init__(records)
        self._memories = memories

    def list_memories(self, *, limit: int = 200, memory_type_prefix: str | None = None):
        rows = self._memories[:limit]
        if memory_type_prefix:
            rows = [row for row in rows if row.memory_type.startswith(memory_type_prefix)]
        return rows


def test_experience_training_pipeline_uses_kpi_task_memories_when_trained_memories_absent(tmp_path, monkeypatch):
    _set_training_env(monkeypatch)
    pipeline = ExperienceTrainingPipeline(
        dataset_path=tmp_path / "experience_sft_dataset.jsonl",
        adapter_state_path=tmp_path / "experience_adapter_state.json",
        policy_weights_path=tmp_path / "experience_policy_weights.json",
    )
    persistent = DummyPersistentWithKPI(
        [],
        [
            DummyMemoryRow(
                memory_type="kpi_task:test",
                content={
                    "task_type": "test",
                    "model": "qwen2.5:32b-instruct-q4_k_m",
                    "success": False,
                    "quality_score": 0.32,
                    "profile_weights": {"quality": 0.95, "budget": 1.1},
                },
            )
        ],
    )

    result = pipeline.train(persistent=persistent, rolling_kpi_path=tmp_path / "missing_kpi.json", runtime_snapshot={"local_llm_ready": False, "ai_kernel_enabled": True, "provider_inventory_ready": True})

    assert result["kpi_observations"] == 1
    adapter = json.loads((tmp_path / "experience_adapter_state.json").read_text(encoding="utf-8"))
    assert adapter["task_profiles"]["test"]["samples"] == 1
    assert adapter["task_profiles"]["test"]["delegate"] is False
    assert adapter["task_profiles"]["test"]["training_ready"] is False
    assert adapter["task_profiles"]["test"]["dominant_model"] == "qwen2.5:32b-instruct-q4_k_m"
