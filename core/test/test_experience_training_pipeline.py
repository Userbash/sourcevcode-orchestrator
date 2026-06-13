from __future__ import annotations

import json

from core.core.experience_training_pipeline import ExperienceTrainingPipeline
from core.core.local_llm_module import LocalLLMModule


class DummyRecord:
    def __init__(self, *, task_type: str, memory_domain: str, summary: str, model_name: str, provider: str, quality_score: float) -> None:
        self.memory_domain = memory_domain
        self.content = {"task_type": task_type, "summary": summary}
        self.metadata = {"task_type": task_type, "model_name": model_name, "provider": provider}
        self.quality_score = quality_score
        self.source_memory_ids = [1, 2]


class DummyPersistent:
    def __init__(self, records):
        self._records = records

    def list_trained_memories(self, limit: int = 200):
        return self._records[:limit]


def test_experience_training_pipeline_writes_dataset_and_adapter_state(tmp_path):
    records = [
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="prefer smaller safe refactors with tests", model_name="deepseek-r1:14b", provider="local", quality_score=0.96),
        DummyRecord(task_type="code", memory_domain="prompt:code", summary="update tests after refactor", model_name="deepseek-r1:14b", provider="local", quality_score=0.93),
        DummyRecord(task_type="docs", memory_domain="prompt:docs", summary="keep the summary concise and user-facing", model_name="qwen-2.5-7b-instruct", provider="local", quality_score=0.91),
    ]
    pipeline = ExperienceTrainingPipeline(
        dataset_path=tmp_path / "experience_sft_dataset.jsonl",
        adapter_state_path=tmp_path / "experience_adapter_state.json",
        policy_weights_path=tmp_path / "experience_policy_weights.json",
    )

    result = pipeline.train(persistent=DummyPersistent(records), rolling_kpi_path=tmp_path / "missing_kpi.json")

    assert result["status"] == "trained"
    assert result["records"] == 3
    rows = (tmp_path / "experience_sft_dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    adapter = json.loads((tmp_path / "experience_adapter_state.json").read_text(encoding="utf-8"))
    assert adapter["task_profiles"]["code"]["preferred_model"] == "deepseek-r1:14b"
    assert adapter["task_profiles"]["docs"]["task_family"] == "docs_workflow"


def test_local_llm_module_uses_experience_adapter_state(tmp_path):
    adapter_path = tmp_path / "experience_adapter_state.json"
    adapter_path.write_text(json.dumps({
        "task_profiles": {
            "code": {
                "preferred_model": "deepseek-r1:14b",
                "recommended_model": "deepseek-r1:14b",
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

    assert advisory["preferred_model"] == "deepseek-r1:14b"
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


def test_experience_training_pipeline_uses_kpi_task_memories_when_trained_memories_absent(tmp_path):
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

    result = pipeline.train(persistent=persistent, rolling_kpi_path=tmp_path / "missing_kpi.json")

    assert result["kpi_observations"] == 1
    adapter = json.loads((tmp_path / "experience_adapter_state.json").read_text(encoding="utf-8"))
    assert adapter["task_profiles"]["test"]["samples"] == 1
    assert adapter["task_profiles"]["test"]["delegate"] is False
    assert adapter["task_profiles"]["test"]["dominant_model"] == "qwen2.5:32b-instruct-q4_k_m"
