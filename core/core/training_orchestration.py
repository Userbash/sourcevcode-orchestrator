from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import Complexity, ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType


EXPERIENCE_TRAINING_TASK_BOARD_SCHEMA_VERSION = "experience-training-wave.v1"


_GLOBAL_CONSTRAINTS = [
    "Do not treat runtime memory reuse as weight training.",
    "Keep operational KPI observations separated from prompt-learning examples.",
    "Do not prefer a model/provider below the configured sample and evidence thresholds.",
    "Semantic retrieval must degrade gracefully when embeddings or the preferred training AI are unavailable.",
    "Every training-stage change must preserve deterministic fallback behavior for offline/local-only environments.",
]


_VALIDATION_COMMANDS = [
    "python3 -m pytest core/test/test_experience_training_pipeline.py core/test/test_experience_policy_learner.py core/test/test_training_orchestration.py -q",
    "python3 -m pytest core/test/test_hybrid_memory.py -k \"trained_memory or fast_retrieve\" -q",
    "python3 -m core.scripts.generate_training_task_board --json",
]


def _task(
    *,
    task_id: str,
    owner: str,
    task_type: TaskType,
    required_capability: str,
    description: str,
    files: list[str],
    constraints: list[str],
    acceptance_criteria: list[str],
    repo_path: str | None,
    branch: str | None,
    dependencies: list[str] | None = None,
    preferred_provider: str = "local",
) -> Task:
    return Task(
        task_id=task_id,
        type=task_type,
        priority=Priority.HIGH,
        complexity=Complexity.HIGH,
        required_capability=required_capability,
        input=TaskInput(
            description=description,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        ),
        context=TaskContext(project="core", repo_path=repo_path, branch=branch),
        dependencies=list(dependencies or []),
        routing_hints={
            "worker_role": owner,
            "parallel_group": "experience-training-wave-1",
            "preferred_provider": preferred_provider,
            "preferred_agent_id": "orchestrator",
            "source": "experience_training_orchestration",
        },
    )


def choose_training_supervisor(*, runtime_snapshot: dict[str, Any] | None = None, adapter_state: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_snapshot = dict(runtime_snapshot or {})
    adapter_state = dict(adapter_state or {})
    profiles = dict(adapter_state.get("task_profiles") or {})
    ready_profiles = sum(1 for item in profiles.values() if isinstance(item, dict) and item.get("training_ready") is True)
    collecting_profiles = sum(1 for item in profiles.values() if isinstance(item, dict) and str(item.get("training_stage") or "") == "collecting")
    local_ready = bool(runtime_snapshot.get("local_llm_ready"))
    ai_kernel_enabled = bool(runtime_snapshot.get("ai_kernel_enabled"))
    provider_inventory_ready = bool(runtime_snapshot.get("provider_inventory_ready", True))

    if local_ready:
        primary = {
            "owner": "local_llm",
            "responsibility": "Curate records, normalize labels, compress summaries, and synthesize reusable training patterns.",
            "why": "Local LLM gives the cheapest always-on semantic curation lane for dataset enrichment and fallback-safe retrieval prep.",
        }
        fallback_chain = ["ai_kernel" if ai_kernel_enabled else "orchestrator", "orchestrator"]
    elif ai_kernel_enabled:
        primary = {
            "owner": "ai_kernel",
            "responsibility": "Perform semantic curation and retrieval/indexing when the local LLM lane is unavailable.",
            "why": "AI kernel provides the strongest local fallback for richer semantic labeling and vector-oriented prep.",
        }
        fallback_chain = ["orchestrator"]
    else:
        primary = {
            "owner": "orchestrator",
            "responsibility": "Run deterministic rule-based filtering, thresholding, and plan generation without semantic helper models.",
            "why": "Core orchestrator is always available and preserves training continuity when semantic helpers are offline.",
        }
        fallback_chain = []

    support_roles = {
        "policy_analyst": "orchestrator" if provider_inventory_ready else primary["owner"],
        "retrieval_indexer": "ai_kernel" if ai_kernel_enabled else primary["owner"],
        "validator": "orchestrator",
    }
    return {
        "primary": primary,
        "fallback_chain": fallback_chain,
        "support_roles": support_roles,
        "ready_profiles": ready_profiles,
        "collecting_profiles": collecting_profiles,
    }


def _worker_prompt(owner: str) -> str:
    prompts = {
        "lead": """
Контекст:
- Опыт обучения строится не через fine-tune весов, а через enriched memory, policy learning и semantic retrieval.
- Нужно уменьшить шум, не завершать накопление преждевременно и разделить operational KPI от learning dataset.

Задача:
- Зафиксируй orchestration contract для волны улучшения обучения.
- Не меняй unrelated runtime.
- Сохрани деградацию до deterministic fallback при недоступности semantic AI.
""".strip(),
        "curator": """
Задача:
- Обогатить training records: problem, outcome, constraints, files, failure_mode, reuse_hint, provenance.
- Отфильтровать generic/шумные summaries.
- Подготовить clean SFT-ready rows без смешивания KPI-only сигналов.
""".strip(),
        "labeler": """
Задача:
- Нормализовать quality_score, success/failure taxonomy и reuse usefulness labels.
- Ввести понятные weak/usable/high-signal классы.
- Не допускать placeholder model/provider names в обучающий слой.
""".strip(),
        "policy_analyst": """
Задача:
- Сделать routing/policy устойчивым к малым sample sizes.
- Усилить minimum thresholds и confidence weighting.
- Разделить evidence из trained_memories и operational KPI.
""".strip(),
        "retrieval_indexer": """
Задача:
- Усилить semantic retrieval для trained memories.
- Использовать embeddings/vector-style representation с fallback на deterministic hashed vectors.
- Гарантировать, что retrieval выдерживает перефразировки лучше keyword overlap.
""".strip(),
        "integrator": """
Задача:
- Свести curator/labeler/policy/retrieval изменения без contract drift.
- Сохранить совместимость adapter_state, dataset artifacts и persistent memory формата.
""".strip(),
        "validator": """
Задача:
- Подтвердить, что обучение не заканчивается рано, noisy records режутся, а fallback-chain работает.
- Прогнать targeted tests и выписать residual risks.
""".strip(),
    }
    return prompts[owner]


def _worker_roles(supervisor: dict[str, Any]) -> list[dict[str, str]]:
    support_roles = dict(supervisor.get("support_roles") or {})
    primary_owner = str(((supervisor.get("primary") or {}).get("owner") or "orchestrator"))
    retrieval_owner = str(support_roles.get("retrieval_indexer") or primary_owner)
    policy_owner = str(support_roles.get("policy_analyst") or "orchestrator")
    validator_owner = str(support_roles.get("validator") or "orchestrator")
    return [
        {"owner": "lead", "executor": primary_owner, "phase": "serial", "responsibility": "Freeze training-wave contract, constraints, and merge order."},
        {"owner": "curator", "executor": primary_owner, "phase": "parallel", "responsibility": "Clean and enrich training examples."},
        {"owner": "labeler", "executor": primary_owner, "phase": "parallel", "responsibility": "Normalize labels and quality taxonomy."},
        {"owner": "policy_analyst", "executor": policy_owner, "phase": "parallel", "responsibility": "Harden policy learning against low-sample noise."},
        {"owner": "retrieval_indexer", "executor": retrieval_owner, "phase": "parallel", "responsibility": "Improve semantic retrieval and vector fallback."},
        {"owner": "integrator", "executor": "orchestrator", "phase": "serial", "responsibility": "Merge worker outputs without contract drift."},
        {"owner": "validator", "executor": validator_owner, "phase": "serial", "responsibility": "Validate thresholds, fallback behavior, and residual risks."},
    ]


def build_experience_training_execution_plan(
    *,
    adapter_state: dict[str, Any] | None = None,
    runtime_snapshot: dict[str, Any] | None = None,
    repo_path: str | None = None,
    branch: str | None = None,
) -> ExecutionPlan:
    supervisor = choose_training_supervisor(runtime_snapshot=runtime_snapshot, adapter_state=adapter_state)
    roles = _worker_roles(supervisor)
    lead_id = f"training-lead-{uuid4().hex[:8]}"
    curator_id = f"training-curator-{uuid4().hex[:8]}"
    labeler_id = f"training-labeler-{uuid4().hex[:8]}"
    policy_id = f"training-policy-{uuid4().hex[:8]}"
    retrieval_id = f"training-retrieval-{uuid4().hex[:8]}"
    integrator_id = f"training-integrator-{uuid4().hex[:8]}"
    validator_id = f"training-validator-{uuid4().hex[:8]}"
    role_map = {item["owner"]: item for item in roles}

    tasks = [
        _task(
            task_id=lead_id,
            owner="lead",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Freeze the experience-training improvement wave: dataset enrichment, label normalization, semantic retrieval, and fallback-safe training continuity.",
            files=["core/core/experience_training_pipeline.py", "core/core/experience_policy_learner.py", "core/core/hybrid_memory.py"],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Supervisor and fallback chain are explicit.",
                "Merge order is defined.",
                "Noise-reduction and accumulation constraints are locked.",
            ],
            repo_path=repo_path,
            branch=branch,
            preferred_provider=role_map["lead"]["executor"],
        ),
        _task(
            task_id=curator_id,
            owner="curator",
            task_type=TaskType.DOCS,
            required_capability="docs",
            description="Enrich trained memories into higher-signal learning examples with problem/outcome/constraints/files/failure metadata.",
            files=["core/core/persistent_memory.py", "core/core/orchestrator.py", "memory_store/training/experience_sft_dataset.jsonl"],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not mix KPI-only rows into prompt-learning dataset examples."],
            acceptance_criteria=[
                "Learning rows carry rich context fields.",
                "Generic summaries are rejected or down-weighted.",
                "Dataset artifacts remain machine-readable JSONL.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[lead_id],
            preferred_provider=role_map["curator"]["executor"],
        ),
        _task(
            task_id=labeler_id,
            owner="labeler",
            task_type=TaskType.REVIEW,
            required_capability="review",
            description="Normalize quality, failure taxonomy, and reuse usefulness labels so noisy memories do not dominate training decisions.",
            files=["core/core/experience_training_pipeline.py", "core/core/experience_policy_learner.py"],
            constraints=_GLOBAL_CONSTRAINTS + ["Reject placeholder model/provider labels from the learning set."],
            acceptance_criteria=[
                "Signal classes are explicit.",
                "Weak examples are filtered or heavily down-weighted.",
                "Taxonomy is deterministic in fallback mode.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[lead_id],
            preferred_provider=role_map["labeler"]["executor"],
        ),
        _task(
            task_id=policy_id,
            owner="policy_analyst",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Harden policy learning with confidence weighting, minimum support thresholds, and explicit separation between operational evidence and learning evidence.",
            files=["core/core/experience_policy_learner.py", "memory_store/experience_policy_weights.json"],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not recommend preferred models below minimum evidence thresholds."],
            acceptance_criteria=[
                "Low-sample wins cannot immediately dominate routing.",
                "Policy weights expose effective sample support.",
                "Fallback recommendations stay deterministic.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[lead_id],
            preferred_provider=role_map["policy_analyst"]["executor"],
        ),
        _task(
            task_id=retrieval_id,
            owner="retrieval_indexer",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Improve semantic retrieval for trained memories with vector-style ranking, richer signals, and deterministic fallback when embeddings are unavailable.",
            files=["core/core/hybrid_memory.py", "core/core/memory_settings.py"],
            constraints=_GLOBAL_CONSTRAINTS + ["Keep retrieval operational without external vector DB dependencies by default."],
            acceptance_criteria=[
                "Performs better on paraphrases than plain keyword overlap.",
                "Falls back to deterministic local vectorization when semantic AI is offline.",
                "Ranking stays query-aware and thresholded.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[lead_id],
            preferred_provider=role_map["retrieval_indexer"]["executor"],
        ),
        _task(
            task_id=integrator_id,
            owner="integrator",
            task_type=TaskType.REVIEW,
            required_capability="review",
            description="Integrate training-wave changes across dataset, policy, and retrieval without breaking adapter_state or persistent memory contracts.",
            files=["core/core/experience_training_pipeline.py", "core/core/experience_policy_learner.py", "core/core/hybrid_memory.py"],
            constraints=_GLOBAL_CONSTRAINTS + ["Preserve backward-compatible artifact structure where possible."],
            acceptance_criteria=[
                "Merged output preserves artifact paths and contract shape.",
                "Worker changes do not overlap destructively.",
                "Fallback chain remains explicit.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[curator_id, labeler_id, policy_id, retrieval_id],
            preferred_provider=role_map["integrator"]["executor"],
        ),
        _task(
            task_id=validator_id,
            owner="validator",
            task_type=TaskType.TEST,
            required_capability="test",
            description="Validate that noisy records are filtered, training does not terminate early, and fallback ownership works when semantic helper AIs are unavailable.",
            files=["core/test/test_experience_training_pipeline.py", "core/test/test_experience_policy_learner.py", "core/test/test_hybrid_memory.py"],
            constraints=_GLOBAL_CONSTRAINTS + ["Use focused tests that prove accumulation and fallback semantics."],
            acceptance_criteria=[
                "Training-ready thresholds are enforced correctly.",
                "Fallback owner is explicit when local semantic AI is unavailable.",
                "Residual risks are documented.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[integrator_id],
            preferred_provider=role_map["validator"]["executor"],
        ),
    ]
    return ExecutionPlan(
        root_task_id=lead_id,
        atomic_tasks=tasks,
        draft_layers=[
            {
                "name": "experience_training_wave",
                "parallel": True,
                "objective": "Upgrade the learning loop with lower noise, richer memory records, stronger retrieval, and explicit fallback ownership.",
            }
        ],
    )


def build_experience_training_task_board(
    *,
    adapter_state: dict[str, Any] | None = None,
    runtime_snapshot: dict[str, Any] | None = None,
    repo_path: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    adapter_state = dict(adapter_state or {})
    runtime_snapshot = dict(runtime_snapshot or {})
    supervisor = choose_training_supervisor(runtime_snapshot=runtime_snapshot, adapter_state=adapter_state)
    roles = _worker_roles(supervisor)
    training_stats = {
        "total_records": int(adapter_state.get("total_records", 0) or 0),
        "usable_records": int(adapter_state.get("usable_records", 0) or 0),
        "task_profiles": len(dict(adapter_state.get("task_profiles") or {})),
        "min_samples": int(adapter_state.get("min_samples", 0) or 0),
        "min_effective_samples": float(adapter_state.get("min_effective_samples", 0.0) or 0.0),
        "min_signal_score": float(adapter_state.get("min_signal_score", 0.0) or 0.0),
        "ready_profiles": int(supervisor.get("ready_profiles", 0) or 0),
        "collecting_profiles": int(supervisor.get("collecting_profiles", 0) or 0),
    }
    return {
        "schema_version": EXPERIENCE_TRAINING_TASK_BOARD_SCHEMA_VERSION,
        "objective": "Coordinate the learning-system upgrade across enrichment, labeling, retrieval, policy hardening, and validation without relying on weight training.",
        "context": {
            "repo_path": repo_path,
            "branch": branch,
            "training_stats": training_stats,
            "runtime_snapshot": runtime_snapshot,
        },
        "training_supervisor": supervisor,
        "merge_order": ["curator", "labeler", "policy_analyst", "retrieval_indexer", "integrator", "validator"],
        "worker_roles": roles,
        "tasks": [
            {
                "owner": item["owner"],
                "executor": item["executor"],
                "phase": item["phase"],
                "parallelizable": item["phase"] == "parallel",
                "responsibility": item["responsibility"],
                "prompt": _worker_prompt(item["owner"]),
            }
            for item in roles
        ],
        "constraints": list(_GLOBAL_CONSTRAINTS),
        "validation_commands": list(_VALIDATION_COMMANDS),
    }
