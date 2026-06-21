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


def _set_policy_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_BRIDGE_TRAINING_MIN_SAMPLES", "8")
    monkeypatch.setenv("AI_BRIDGE_TRAINED_MEMORY_MIN_QUALITY", "0.55")


def test_experience_policy_recommends_best_local_model_from_kpi_and_memory(tmp_path, monkeypatch):
    _set_policy_env(monkeypatch)
    rolling_kpi_path = tmp_path / "rolling_kpi_store.json"
    rolling_kpi_path.write_text(
        json.dumps(
            {
                "code::qwen2.5:32b-instruct-q4_k_m": {
                    "successes": [True, True, True, True, True, True, True, False],
                    "latencies": [0.9, 1.0, 0.8, 0.95, 0.88, 0.92, 0.97, 1.02],
                    "quality_scores": [0.93, 0.95, 0.94, 0.96, 0.91, 0.92, 0.95, 0.82],
                }
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    learner = ExperiencePolicyLearner(weights_path=tmp_path / "experience_policy_weights.json")
    persistent = DummyPersistent(
        [
            DummyRecord(
                metadata={"model_name": "qwen2.5:32b-instruct-q4_k_m", "provider": "local", "signal_score": 0.86},
                content={
                    "task_type": "code",
                    "summary": "Prefer smaller safe refactors, preserve public interfaces, and expand regression tests before touching the parser entrypoints.",
                    "problem": "Refactor parser without breaking external API behavior.",
                    "files": ["core/core/orchestrator.py"],
                    "constraints": ["Keep diff reviewable"],
                },
                quality_score=0.97,
            )
        ]
    )

    learner.refresh(persistent=persistent, rolling_kpi_path=rolling_kpi_path)
    recommendation = learner.recommend_model(task_type="code", allowed_providers={"local"})

    assert recommendation is not None
    assert recommendation["model_name"] == "qwen2.5:32b-instruct-q4_k_m"
    assert recommendation["provider"] == "local"
    assert recommendation["samples"] >= 8
    assert recommendation["effective_samples"] >= 6.4
    assert recommendation["consistency"] >= 0.45
    assert recommendation["score"] >= 0.65


def test_experience_policy_withholds_recommendation_below_min_samples(tmp_path, monkeypatch):
    _set_policy_env(monkeypatch)
    rolling_kpi_path = tmp_path / "rolling_kpi_store.json"
    rolling_kpi_path.write_text(
        json.dumps(
            {
                "code::qwen2.5:32b-instruct-q4_k_m": {
                    "successes": [True, True, True],
                    "latencies": [0.9, 1.0, 0.8],
                    "quality_scores": [0.93, 0.95, 0.94],
                }
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    learner = ExperiencePolicyLearner(weights_path=tmp_path / "experience_policy_weights.json")

    learner.refresh(persistent=None, rolling_kpi_path=rolling_kpi_path)
    recommendation = learner.recommend_model(task_type="code", allowed_providers={"local"})

    assert recommendation is None


def test_experience_policy_withholds_noisy_model_despite_enough_raw_samples(tmp_path, monkeypatch):
    _set_policy_env(monkeypatch)
    rolling_kpi_path = tmp_path / "rolling_kpi_store.json"
    rolling_kpi_path.write_text(
        json.dumps(
            {
                "code::qwen2.5:32b-instruct-q4_k_m": {
                    "successes": [True, False, True, False, True, False, True, False],
                    "latencies": [0.9, 1.4, 0.8, 1.5, 0.85, 1.6, 0.82, 1.45],
                    "quality_scores": [0.95, 0.42, 0.91, 0.38, 0.94, 0.41, 0.92, 0.39],
                }
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    learner = ExperiencePolicyLearner(weights_path=tmp_path / "experience_policy_weights.json")

    learner.refresh(persistent=None, rolling_kpi_path=rolling_kpi_path)
    recommendation = learner.recommend_model(task_type="code", allowed_providers={"local"})

    assert recommendation is None
