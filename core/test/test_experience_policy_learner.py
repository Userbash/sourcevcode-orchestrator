from __future__ import annotations

import json

from core.core.experience_policy_learner import ExperiencePolicyLearner


class DummyRecord:
    def __init__(self, *, metadata: dict[str, object], content: dict[str, object], quality_score: float) -> None:
        self.metadata = metadata
        self.content = content
        self.quality_score = quality_score


class DummyPersistent:
    def __init__(self, records: list[DummyRecord]) -> None:
        self._records = records

    def list_trained_memories(self, limit: int = 200):
        return self._records[:limit]


def test_experience_policy_recommends_best_local_model_from_kpi_and_memory(tmp_path):
    rolling_kpi_path = tmp_path / "rolling_kpi_store.json"
    rolling_kpi_path.write_text(
        json.dumps(
            {
                "code::qwen2.5:32b-instruct-q4_k_m": {
                    "successes": [True, True, False],
                    "latencies": [1.2, 1.1, 1.3],
                    "quality_scores": [0.84, 0.86, 0.52],
                },
                "code::deepseek-r1:14b": {
                    "successes": [True, True, True],
                    "latencies": [0.9, 1.0, 0.8],
                    "quality_scores": [0.93, 0.95, 0.94],
                },
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    learner = ExperiencePolicyLearner(weights_path=tmp_path / "experience_policy_weights.json")
    persistent = DummyPersistent(
        [
            DummyRecord(
                metadata={"model_name": "deepseek-r1:14b", "provider": "local"},
                content={"task_type": "code"},
                quality_score=0.97,
            )
        ]
    )

    learner.refresh(persistent=persistent, rolling_kpi_path=rolling_kpi_path)
    recommendation = learner.recommend_model(task_type="code", allowed_providers={"local"})

    assert recommendation is not None
    assert recommendation["model_name"] == "deepseek-r1:14b"
    assert recommendation["provider"] == "local"
    assert recommendation["samples"] >= 4
    assert recommendation["score"] >= 0.65
