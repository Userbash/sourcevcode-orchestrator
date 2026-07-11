from __future__ import annotations

from typing import Any

from .models import Complexity, ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType

ANALYTICS_MATRIX_MULTI_AGENT_WAVE = "analytics-matrix-multi-agent-wave.v1"

_DOMAIN_KEYWORDS = (
    "analytics",
    "matrix",
    "data science",
    "keywords",
    "слов",
    "словосочет",
    "предложен",
    "генератив",
    "knowledge pool",
    "retrieval",
    "search",
    "search engine",
    "prompt pool",
    "data intelligence",
)

_PARALLELIZATION_KEYWORDS = (
    "multi-agent",
    "parallel",
    "between ai agents",
    "между ии агентами",
    "распарал",
    "раздели задачу",
    "раздели код",
)


def _normalized_task_text(task: Task, advisory_context: dict[str, Any] | None = None) -> str:
    parts = [
        task.input.description or "",
        " ".join(task.input.files or []),
        " ".join(task.input.constraints or []),
        " ".join(task.input.acceptance_criteria or []),
        " ".join(str(value) for value in (task.routing_hints or {}).values()),
        " ".join(str(value) for value in (advisory_context or {}).values()),
    ]
    return " ".join(parts).lower()


def matches_analytics_matrix_multi_agent_request(task: Task, advisory_context: dict[str, Any] | None = None) -> bool:
    if task.type not in {TaskType.PLAN, TaskType.CODE, TaskType.FIX, TaskType.TEST, TaskType.RESEARCH}:
        return False
    if bool((task.routing_hints or {}).get("analytics_matrix_multi_agent")):
        return True
    text = _normalized_task_text(task, advisory_context=advisory_context)
    return any(keyword in text for keyword in _DOMAIN_KEYWORDS) and any(keyword in text for keyword in _PARALLELIZATION_KEYWORDS)


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
) -> Task:
    return Task(
        task_id=task_id,
        type=task_type,
        priority=Priority.HIGH,
        complexity=Complexity.MEDIUM,
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
            "parallel_group": "analytics_matrix_wave_1",
            "source": "analytics_matrix_orchestration",
            "orchestration_wave": ANALYTICS_MATRIX_MULTI_AGENT_WAVE,
        },
    )


def build_analytics_matrix_multi_agent_execution_plan(task: Task) -> ExecutionPlan:
    lead_id = f"{task.task_id}-matrix-lead"
    matrix_id = f"{task.task_id}-matrix-core"
    retrieval_id = f"{task.task_id}-matrix-retrieval"
    generator_id = f"{task.task_id}-matrix-generator"
    agent_id = f"{task.task_id}-matrix-agent"
    tests_id = f"{task.task_id}-matrix-tests"
    integrator_id = f"{task.task_id}-matrix-integrator"

    constraints = [
        "Keep existing analytics modules backward compatible.",
        "Prefer additive files and contracts over destructive refactors.",
        "Keep retrieval and generation interfaces explicit.",
        "Use deterministic fallback generation when no model adapter is present.",
    ]

    tasks = [
        _task(
            task_id=lead_id,
            owner="matrix_lead",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Freeze analytics matrix interfaces and split implementation lanes for parallel AI agents.",
            files=[
                "core/core/analytics_matrix_engine.py",
                "core/agents/data_analytics_matrix_agent.py",
                "core/core/analytics_matrix_orchestration.py",
            ],
            constraints=constraints,
            acceptance_criteria=[
                "Parallel lane ownership is explicit.",
                "Interfaces for matrix engine and generator adapter are frozen.",
                "Integration checkpoints are documented.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
        ),
        _task(
            task_id=matrix_id,
            owner="matrix_engine_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Implement matrices for letters, words, phrases, templates, and sentence graphs.",
            files=["core/core/analytics_matrix_engine.py"],
            constraints=constraints,
            acceptance_criteria=[
                "Keyword, phrase, sentence, and character matrices are persisted in one report.",
                "Template extraction works for key-value, table, and bullet patterns.",
                "Reports are serializable for reuse.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=retrieval_id,
            owner="matrix_retrieval_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Build shared knowledge pool ingestion and retrieval ranking for analytics reports.",
            files=["core/core/analytics_matrix_engine.py"],
            constraints=constraints,
            acceptance_criteria=[
                "Knowledge pool supports ingest and top-k query.",
                "Related analytics records can be reused in prompt pools.",
                "Retrieval remains deterministic without external services.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=generator_id,
            owner="matrix_generation_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Implement the generator adapter and fallback narrative synthesis path for analytics matrices.",
            files=["core/core/analytics_matrix_engine.py"],
            constraints=constraints,
            acceptance_criteria=[
                "Generated narrative can be produced without an LLM.",
                "Optional model adapter can enrich keywords and summaries.",
                "Narrative summarizes keywords, structure, and retrieval signals.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=agent_id,
            owner="matrix_agent_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Create a dedicated analytics matrix agent that exposes matrix output and knowledge-pool state.",
            files=["core/agents/data_analytics_matrix_agent.py"],
            constraints=constraints,
            acceptance_criteria=[
                "Agent returns analytics matrix payload in result output.",
                "Agent ingests its reports into a reusable knowledge pool.",
                "Memory context can be folded into analysis input.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=tests_id,
            owner="matrix_test_owner",
            task_type=TaskType.TEST,
            required_capability="testing",
            description="Write focused tests for the analytics matrix engine, retrieval pool, agent output, and orchestration plan.",
            files=[
                "core/test/test_analytics_matrix_engine.py",
                "core/test/test_data_analytics_matrix_agent.py",
                "core/test/test_analytics_matrix_orchestration.py",
            ],
            constraints=constraints,
            acceptance_criteria=[
                "Matrix extraction is covered with deterministic fixtures.",
                "Knowledge pool retrieval is covered.",
                "Multi-agent plan dependencies are covered.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[matrix_id, retrieval_id, generator_id, agent_id],
        ),
        _task(
            task_id=integrator_id,
            owner="matrix_integrator",
            task_type=TaskType.REVIEW,
            required_capability="review",
            description="Review and integrate the analytics matrix wave deliverables into one coherent handoff.",
            files=[
                "core/core/analytics_matrix_engine.py",
                "core/agents/data_analytics_matrix_agent.py",
                "core/core/analytics_matrix_orchestration.py",
            ],
            constraints=constraints,
            acceptance_criteria=[
                "Final artifacts are consistent across engine, agent, and tests.",
                "No lane regresses deterministic fallback behavior.",
                "Integration handoff contains the final matrix contract.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[matrix_id, retrieval_id, generator_id, agent_id, tests_id],
        ),
    ]
    return ExecutionPlan(
        root_task_id=lead_id,
        atomic_tasks=tasks,
        draft_layers=[
            {
                "name": "analytics_matrix_parallel_wave",
                "parallel": True,
                "sub_agents": [
                    "matrix_engine_owner",
                    "matrix_retrieval_owner",
                    "matrix_generation_owner",
                    "matrix_agent_owner",
                ],
            }
        ],
        metadata={"orchestration_wave": ANALYTICS_MATRIX_MULTI_AGENT_WAVE},
    )
