from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import Complexity, ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType


DIAGNOSTIC_TASK_BOARD_SCHEMA_VERSION = "diagnostic-task-board.v1"
ANTIGRAVITY_CLI_BRIDGE_TASK_BOARD_SCHEMA_VERSION = "antigravity-cli-bridge-task-board.v1"


_GLOBAL_CONSTRAINTS = [
    "Do not break the existing diagnostics.v1 payload shape.",
    "Do not refactor unrelated modules.",
    "Default diagnostics must work in TESTING=true without live network access.",
    "Every new probe must emit machine-readable status, ok, failure_code, recovery_hint, and details.",
]


_VALIDATION_COMMANDS = [
    "python3 -m pytest core/test/test_diagnostic_contracts.py core/test/test_self_diagnostic_module.py -q",
    "python3 -m core.scripts.run_self_diagnostic --matrix-only --json",
    "python3 -m core.scripts.run_self_diagnostic --layer memory --json",
    "python3 -m core.scripts.run_self_diagnostic --layer providers --json",
]


_ANTIGRAVITY_GLOBAL_CONSTRAINTS = [
    "Do not refactor unrelated provider/runtime code.",
    "Do not hide upstream CLI/API failures behind generic timeout or auth labels.",
    "Preserve backward-compatible Antigravity status payload keys unless an integrator explicitly widens tests first.",
    "Keep live probes optional and keep structural probes deterministic where possible.",
]


_ANTIGRAVITY_VALIDATION_COMMANDS = [
    "python3 -m pytest core/test/test_external_ai_bridge.py core/test/test_antigravity_manager.py -q",
    "python3 -m core.scripts.generate_diagnostic_task_board --wave antigravity-cli-bridge --json",
]


def _worker_prompt(owner: str) -> str:
    prompts = {
        "lead": """
Контекст:
- В проекте уже есть diagnostic contract `diagnostics.v1`.
- Диагностика boot/planning/routing/execution/memory/providers/observability проходит в testing mode.

Задача:
- Зафиксируй orchestration contract для следующей волны работ.
- Не пиши runtime fixes.
- Определи freeze по payload shape, failure_code catalog, merge-order и validation gates.

Результат:
- machine-readable task board constraints
- единый catalog failure_code/recovery_hint
- merge sequencing для остальных ИИ
""".strip(),
        "http": """
Задача:
- Добавить HTTP endpoint `/diagnostics` как thin adapter.
- Не дублировать diagnostic logic.
- Использовать existing self_diagnostic_module / diagnostic_contracts.

Нужно:
- JSON response
- поддержка выбора layer
- matrix-only mode при уместности
- корректные HTTP status codes
- минимальные HTTP tests
""".strip(),
        "vfs": """
Задача:
- Углубить diagnostics для `unified_vfs`, `data_plane_monitor`, `persistent_memory`.
- Не трогать provider и HTTP code.

Нужно:
- поля `read_write_ok`, `integrity_ok`, `data_present`, `recovery_ok`
- normalized `failure_code`
- normalized `recovery_hint`
- no live-network dependency by default
""".strip(),
        "providers": """
Задача:
- Углубить provider diagnostics без обязательных live network calls по умолчанию.
- Не ломать existing diagnostics.v1.

Нужно:
- поля `reachable`, `authenticated`, `inventory_ok`, `live_ok`
- optional live probe
- normalized `failure_code`
- normalized `recovery_hint`
""".strip(),
        "tests": """
Задача:
- Добавить pytest и automation coverage под новые diagnostics probes.
- Не менять production logic кроме тестовых hooks.

Нужно:
- contract tests
- CLI smoke probes
- backward compatibility checks
- automation runner coverage
""".strip(),
        "integrator": """
Задача:
- Свести изменения VFS/providers/HTTP/tests без shape drift.
- Не изобретать новый contract.

Проверь:
- `status`, `ok`, `failure_code`, `recovery_hint`, `details`
- selected_layers / results / failures / matrix
- CLI exit codes
- legacy compatibility
""".strip(),
        "validator": """
Задача:
- Выполнить финальную сборку diagnostic wave.
- Прогнать validation commands.
- Сформировать краткий regression summary и unresolved risk list.
""".strip(),
    }
    return prompts[owner]


def _antigravity_worker_prompt(owner: str) -> str:
    prompts = {
        "lead": """
Контекст:
- Antigravity CLI resolves to `/var/home/sanya/.npm-packages/bin/gemini`.
- Direct generation probe returned upstream `429 RESOURCE_EXHAUSTED / MODEL_CAPACITY_EXHAUSTED`.
- `AntigravityManager.status()` currently reports `models_probe.ok=true`, `generation_probe.ok=false`, `api_probe.status_code=403`, `auth_mode=api_key`.

Задача:
- Зафиксируй failure catalog и merge sequencing для antigravity CLI bridge wave.
- Не чини runtime.
- Не выходи за пределы antigravity_cli_bridge failure domain.

Результат:
- machine-readable failure taxonomy
- readiness normalization rules
- merge order для `cli_bridge`, `manager`, `tests`, `integrator`, `validator`
""".strip(),
        "cli_bridge": """
Задача:
- Локализованно доработать `core/core/external_ai_bridge.py`.
- Не менять `AntigravityManager`.

Нужно:
- различать `cli_missing`
- различать upstream `429 RESOURCE_EXHAUSTED / MODEL_CAPACITY_EXHAUSTED`
- не маскировать upstream ошибки в generic timeout/error_type
- сохранить fallback semantics по runtime router
""".strip(),
        "manager": """
Задача:
- Локализованно доработать `core/core/integrations/antigravity_manager.py`.
- Не менять `ExternalAIBridge`.

Нужно:
- разделить `models_probe`, `generation_probe`, `auth_probe`, `api_probe`
- не считать `gemini --version` полноценной inventory/live готовностью
- различать `oauth/session issue` и `api_key permission issue`
- стабилизировать readiness aggregation без login-loop regression
""".strip(),
        "tests": """
Задача:
- Добавить точечные тесты для antigravity CLI bridge failure catalog.
- Не расширять охват на unrelated providers.

Нужно:
- покрыть `cli_missing`
- покрыть `model_capacity_exhausted`
- покрыть `timeout masking upstream error`
- покрыть `api_key permission issue`
- покрыть readiness aggregation contract
""".strip(),
        "integrator": """
Задача:
- Свести изменения `ExternalAIBridge`, `AntigravityManager` и тестов без payload drift.
- Не менять другие provider layers.

Проверь:
- error taxonomy
- status payload stability
- auth_mode semantics
- compatibility of live vs structural probe reporting
""".strip(),
        "validator": """
Задача:
- Прогнать финальную validation wave для antigravity CLI bridge.
- Подтвердить, что failure catalog воспроизводим и regression scope локализован.

Нужно:
- targeted pytest
- JSON task-board smoke
- краткий residual-risk summary по live probe limits
""".strip(),
    }
    return prompts[owner]


def _failure_code_catalog() -> list[dict[str, str]]:
    return [
        {
            "failure_code": "PROVIDER_UNREACHABLE",
            "scope": "providers",
            "trigger_condition": "Cached or live transport probe cannot reach provider endpoint or CLI target.",
            "observable_symptom": "Provider entry is degraded or unavailable and no reachable endpoint is confirmed.",
            "next_probe": "Inspect provider bridge transport and provider suppression snapshot.",
            "recovery_hint": "Verify endpoint/CLI path, network route, and provider suppression policy before retrying.",
        },
        {
            "failure_code": "PROVIDER_AUTH_FAILED",
            "scope": "providers",
            "trigger_condition": "Provider credentials are missing, rejected, or runtime auth mode mismatches the current bridge.",
            "observable_symptom": "Provider appears reachable but authenticated=false.",
            "next_probe": "Check auth mode, token source, and runtime router credential selection.",
            "recovery_hint": "Refresh provider credentials or align runtime auth configuration with the selected provider bridge.",
        },
        {
            "failure_code": "PROVIDER_INVENTORY_EMPTY",
            "scope": "providers",
            "trigger_condition": "Provider inventory/model list is empty or cannot be materialized from cache/runtime metadata.",
            "observable_symptom": "Fallback chain exists but inventory_ok=false for selected provider.",
            "next_probe": "Inspect cached availability report and model selector/provider inventory adapters.",
            "recovery_hint": "Refresh provider inventory cache or repair model discovery path before enabling the provider.",
        },
        {
            "failure_code": "PROVIDER_LIVE_PROBE_FAILED",
            "scope": "providers",
            "trigger_condition": "Optional live probe is enabled and direct request/CLI probe fails.",
            "observable_symptom": "Structural diagnostics pass but live_ok=false.",
            "next_probe": "Run provider-specific live probe with verbose transport logging.",
            "recovery_hint": "Debug provider bridge transport, API quota, or CLI runtime before promoting provider readiness.",
        },
        {
            "failure_code": "VFS_READ_WRITE_FAILED",
            "scope": "vfs_data_plane",
            "trigger_condition": "Probe payload cannot be written or read back from the selected VFS/data plane path.",
            "observable_symptom": "read_write_ok=false and probe roundtrip is missing or mismatched.",
            "next_probe": "Inspect backend selection, storage path permissions, and roundtrip probe logs.",
            "recovery_hint": "Repair backend configuration or storage permissions so diagnostic roundtrip can complete.",
        },
        {
            "failure_code": "VFS_INTEGRITY_MISMATCH",
            "scope": "vfs_data_plane",
            "trigger_condition": "Recovered snapshot differs from what was written or integrity metadata is inconsistent.",
            "observable_symptom": "integrity_ok=false with checksum/state mismatch evidence.",
            "next_probe": "Compare stored snapshot, recovered snapshot, and integrity markers.",
            "recovery_hint": "Rebuild corrupted metadata/indexes or repair snapshot serialization consistency.",
        },
        {
            "failure_code": "DATA_PLANE_EMPTY",
            "scope": "vfs_data_plane",
            "trigger_condition": "Expected persisted state is absent when baseline data should exist.",
            "observable_symptom": "data_present=false with empty recovery snapshot or zero expected records.",
            "next_probe": "Check bootstrap expectations and persistence population path.",
            "recovery_hint": "Seed baseline state or restore persistence inputs before relying on recovery logic.",
        },
        {
            "failure_code": "DATA_PLANE_RECOVERY_FAILED",
            "scope": "vfs_data_plane",
            "trigger_condition": "Recovery or restore path raises, stalls, or returns incomplete reconstructed state.",
            "observable_symptom": "recovery_ok=false while storage backend is otherwise reachable.",
            "next_probe": "Inspect recovery sequence, watchdog signals, and restore checkpoints.",
            "recovery_hint": "Repair recovery sequence or checkpoint compatibility before enabling automatic restore.",
        },
        {
            "failure_code": "DIAGNOSTIC_PAYLOAD_INVALID",
            "scope": "orchestration",
            "trigger_condition": "A diagnostic layer emits a non-conforming payload shape or missing required machine-readable fields.",
            "observable_symptom": "Layer result exists but cannot be normalized into diagnostics.v1.",
            "next_probe": "Validate layer result serializer and normalization path.",
            "recovery_hint": "Restore the expected result schema and keep status/ok/failure_code/recovery_hint/details stable.",
        },
        {
            "failure_code": "DIAGNOSTIC_LAYER_EXCEPTION",
            "scope": "orchestration",
            "trigger_condition": "Layer probe throws unhandled exception during contract execution.",
            "observable_symptom": "Layer status is error and failures include exception path.",
            "next_probe": "Inspect the throwing probe, its dependencies, and diagnostic isolation boundaries.",
            "recovery_hint": "Harden the layer probe with bounded exceptions and normalized failure reporting.",
        },
    ]


def _antigravity_failure_code_catalog() -> list[dict[str, str]]:
    return [
        {
            "failure_code": "ANTIGRAVITY_CLI_MISSING",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "CLI binary cannot be resolved or is not executable.",
            "observable_symptom": "Resolution returns no command or local probe fails with not found/no such file.",
            "next_probe": "Inspect resolve_antigravity_cli_command() search order and runtime PATH assembly.",
            "recovery_hint": "Verify CLI installation path, executable bit, and PATH propagation before retrying Antigravity probes.",
        },
        {
            "failure_code": "ANTIGRAVITY_OAUTH_SESSION_ISSUE",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "OAuth-backed CLI session is missing, expired, or stuck in interactive authorization.",
            "observable_symptom": "auth_probe indicates auth_required/login_pending or CLI emits authentication prompt markers.",
            "next_probe": "Inspect verify_auth(), session store markers, and interactive session status.",
            "recovery_hint": "Re-verify the managed login session and confirm whether an existing browser/OAuth flow is already active.",
        },
        {
            "failure_code": "ANTIGRAVITY_API_KEY_PERMISSION_ISSUE",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "API-key fallback reaches the endpoint but receives forbidden or permission denied response.",
            "observable_symptom": "api_probe.status_code=403 with auth_mode=api_key and empty model inventory.",
            "next_probe": "Inspect probe_api_key_models(), API base URL, and project/API enablement for the current key.",
            "recovery_hint": "Verify API-key project permissions and Gemini API enablement before treating API fallback as usable.",
        },
        {
            "failure_code": "ANTIGRAVITY_MODEL_CAPACITY_EXHAUSTED",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "Live generation probe returns upstream 429/resource exhausted/model capacity exhausted.",
            "observable_symptom": "Direct CLI generation fails even though the CLI binary is present.",
            "next_probe": "Inspect generation probe stderr and router fallback model order before changing auth assumptions.",
            "recovery_hint": "Treat the provider as reachable but temporarily capacity-limited; retry later or use the next routed model.",
        },
        {
            "failure_code": "ANTIGRAVITY_TIMEOUT_MASKED_UPSTREAM_ERROR",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "Status/generation probe reports only timeout while the underlying CLI path can emit a more specific upstream failure.",
            "observable_symptom": "generation_probe shows timeout but direct CLI replay reveals 429/resource exhausted or another typed upstream error.",
            "next_probe": "Compare AntigravityManager.status() timeout handling with ExternalAIBridge.classify_error() and direct CLI replay.",
            "recovery_hint": "Preserve the upstream stderr classification in probe payloads so timeout wrappers do not erase the real failure domain.",
        },
        {
            "failure_code": "ANTIGRAVITY_READINESS_AGGREGATION_DRIFT",
            "scope": "antigravity_cli_bridge",
            "trigger_condition": "Status aggregation marks a weak structural probe as inventory/live readiness.",
            "observable_symptom": "models_probe.ok=true is based on `gemini --version` while ready=false and generation/live probes fail.",
            "next_probe": "Inspect status() aggregation rules and how `models_probe` semantics are documented for gemini-backed CLI mode.",
            "recovery_hint": "Separate binary presence, inventory, auth, and live generation readiness in the aggregated status payload.",
        },
    ]


def _task(
    *,
    task_id: str,
    description: str,
    owner: str,
    task_type: TaskType,
    required_capability: str,
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
            "parallel_group": "diagnostic-expansion-wave-1",
            "preferred_agent_id": "orchestrator",
        },
    )


def build_diagnostic_expansion_execution_plan(*, repo_path: str | None = None, branch: str | None = None) -> ExecutionPlan:
    freeze_id = f"diag-freeze-{uuid4().hex[:8]}"
    http_id = f"diag-http-{uuid4().hex[:8]}"
    vfs_id = f"diag-vfs-{uuid4().hex[:8]}"
    providers_id = f"diag-providers-{uuid4().hex[:8]}"
    tests_id = f"diag-tests-{uuid4().hex[:8]}"
    integrate_id = f"diag-integrate-{uuid4().hex[:8]}"
    validate_id = f"diag-validate-{uuid4().hex[:8]}"

    tasks = [
        _task(
            task_id=freeze_id,
            owner="lead",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Freeze diagnostics.v1 orchestration contract, failure_code catalog, merge-order, and validation gates.",
            files=[
                "core/core/diagnostic_contracts.py",
                "core/core/self_diagnostic_module.py",
                "core/scripts/run_self_diagnostic.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Defines stable payload constraints for diagnostics.v1.",
                "Publishes normalized failure_code catalog for providers and VFS/data plane.",
                "Unblocks parallel worker implementation without overlap on payload shape.",
            ],
            repo_path=repo_path,
            branch=branch,
        ),
        _task(
            task_id=http_id,
            owner="http",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Add HTTP /diagnostics endpoint as a thin adapter over existing diagnostics contract logic.",
            files=[
                "core/core/self_diagnostic_module.py",
                "core/scripts/run_self_diagnostic.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not duplicate diagnostic logic in the HTTP layer."],
            acceptance_criteria=[
                "Endpoint returns machine-readable JSON.",
                "Layer selection is supported without payload drift.",
                "HTTP adapter reuses self_diagnostic_module or diagnostic_contracts directly.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=vfs_id,
            owner="vfs",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Deepen VFS and data-plane probes with normalized machine-readable status and recovery hints.",
            files=[
                "core/core/unified_vfs.py",
                "core/core/data_plane_monitor.py",
                "core/core/persistent_memory.py",
                "core/core/diagnostic_contracts.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not introduce mandatory live network probes."],
            acceptance_criteria=[
                "Emits read_write_ok, integrity_ok, data_present, and recovery_ok.",
                "Every failure path has failure_code and recovery_hint.",
                "Testing-mode diagnostics remain local and deterministic.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=providers_id,
            owner="providers",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Deepen provider diagnostics with normalized readiness axes and optional live probe separation.",
            files=[
                "core/core/external_ai_bridge.py",
                "core/core/gemini_runtime_router.py",
                "core/core/diagnostic_contracts.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS + ["Live provider probes must stay optional and explicitly gated."],
            acceptance_criteria=[
                "Emits reachable, authenticated, inventory_ok, and live_ok.",
                "Separates cached/structural checks from optional live checks.",
                "Normalizes provider failure_code and recovery_hint values.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=tests_id,
            owner="tests",
            task_type=TaskType.TEST,
            required_capability="test",
            description="Add contract tests, CLI smoke probes, and automation coverage for the deeper diagnostics wave.",
            files=[
                "core/test/test_diagnostic_contracts.py",
                "core/test/test_self_diagnostic_module.py",
                "tests/run-all-tests.js",
            ],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not silently widen assertions that guard diagnostics.v1 compatibility."],
            acceptance_criteria=[
                "Covers new provider and VFS/data-plane fields.",
                "Adds CLI/automation probes for diagnostics matrix and selected layers.",
                "Preserves backward compatibility assertions for diagnostics.v1 shape.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=integrate_id,
            owner="integrator",
            task_type=TaskType.REVIEW,
            required_capability="review",
            description="Integrate parallel worker changes, preserve contract shape, and reconcile CLI/HTTP/runtime outputs.",
            files=[
                "core/core/diagnostic_contracts.py",
                "core/core/self_diagnostic_module.py",
                "core/scripts/run_self_diagnostic.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not merge any worker output that drifts diagnostics.v1 field names or semantics."],
            acceptance_criteria=[
                "Unified result shape across CLI, HTTP, and runtime API.",
                "Legacy/compatibility paths still resolve selected_layers and status correctly.",
                "No conflicting failure_code semantics remain after merge.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[http_id, vfs_id, providers_id, tests_id],
        ),
        _task(
            task_id=validate_id,
            owner="validator",
            task_type=TaskType.TEST,
            required_capability="test",
            description="Run the final validation suite and produce a regression summary for the diagnostics expansion wave.",
            files=[],
            constraints=_GLOBAL_CONSTRAINTS + ["Do not mark the wave complete without executing validation commands."],
            acceptance_criteria=[
                "Runs the declared validation commands.",
                "Summarizes unresolved risks and remaining optional probes.",
                "Confirms diagnostics.v1 stayed stable after merge.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[integrate_id],
        ),
    ]
    return ExecutionPlan(root_task_id=freeze_id, atomic_tasks=tasks, draft_layers=[{"name": "diagnostic_expansion_wave", "parallel": True, "objective": "Expand diagnostics for HTTP, providers, VFS, and data plane without payload drift."}])


def build_diagnostic_expansion_task_board(*, repo_path: str | None = None, branch: str | None = None) -> dict[str, Any]:
    plan = build_diagnostic_expansion_execution_plan(repo_path=repo_path, branch=branch)
    prompts = {
        "lead": _worker_prompt("lead"),
        "http": _worker_prompt("http"),
        "vfs": _worker_prompt("vfs"),
        "providers": _worker_prompt("providers"),
        "tests": _worker_prompt("tests"),
        "integrator": _worker_prompt("integrator"),
        "validator": _worker_prompt("validator"),
    }
    tasks = []
    for task in plan.atomic_tasks:
        owner = str(task.routing_hints.get("worker_role", ""))
        tasks.append(
            {
                "task_id": task.task_id,
                "title": task.input.description,
                "owner": owner,
                "task_type": task.type.value,
                "required_capability": task.required_capability,
                "depends_on": list(task.dependencies),
                "files": list(task.input.files),
                "constraints": list(task.input.constraints),
                "acceptance_criteria": list(task.input.acceptance_criteria),
                "parallelizable": owner in {"http", "vfs", "providers", "tests"},
                "prompt": prompts[owner],
            }
        )

    return {
        "schema_version": DIAGNOSTIC_TASK_BOARD_SCHEMA_VERSION,
        "objective": "Coordinate the next diagnostics expansion wave across multiple AI workers without overlapping failure domains.",
        "context": {
            "diagnostic_schema_version": "diagnostics.v1",
            "repo_path": repo_path,
            "branch": branch,
            "observed_layers": [
                "boot",
                "planning",
                "routing",
                "execution",
                "memory",
                "providers",
                "observability",
            ],
            "validated_commands": [
                "python3 -m pytest core/test/test_diagnostic_contracts.py core/test/test_self_diagnostic_module.py -q",
                "python3 -m core.scripts.run_self_diagnostic --matrix-only --json",
                "python3 -m core.scripts.run_self_diagnostic --layer memory --json",
            ],
        },
        "global_constraints": list(_GLOBAL_CONSTRAINTS),
        "worker_roles": [
            {
                "owner": "lead",
                "responsibility": "Freeze contract, failure_code catalog, and merge sequencing.",
                "phase": "serial",
            },
            {
                "owner": "http",
                "responsibility": "HTTP /diagnostics adapter only.",
                "phase": "parallel",
            },
            {
                "owner": "vfs",
                "responsibility": "VFS and data-plane probes.",
                "phase": "parallel",
            },
            {
                "owner": "providers",
                "responsibility": "Provider readiness probes and normalization.",
                "phase": "parallel",
            },
            {
                "owner": "tests",
                "responsibility": "pytest, CLI smoke, and automation coverage.",
                "phase": "parallel",
            },
            {
                "owner": "integrator",
                "responsibility": "Merge worker outputs and preserve diagnostics.v1 shape.",
                "phase": "serial",
            },
            {
                "owner": "validator",
                "responsibility": "Run final validation commands and publish regression summary.",
                "phase": "serial",
            },
        ],
        "failure_code_catalog": _failure_code_catalog(),
        "validation_commands": list(_VALIDATION_COMMANDS),
        "merge_order": [
            "vfs",
            "providers",
            "http",
            "tests",
            "integrator",
            "validator",
        ],
        "tasks": tasks,
        "execution_plan": plan.as_dict(),
    }


def build_antigravity_cli_bridge_execution_plan(*, repo_path: str | None = None, branch: str | None = None) -> ExecutionPlan:
    freeze_id = f"antigravity-freeze-{uuid4().hex[:8]}"
    cli_bridge_id = f"antigravity-cli-bridge-{uuid4().hex[:8]}"
    manager_id = f"antigravity-manager-{uuid4().hex[:8]}"
    tests_id = f"antigravity-tests-{uuid4().hex[:8]}"
    integrate_id = f"antigravity-integrate-{uuid4().hex[:8]}"
    validate_id = f"antigravity-validate-{uuid4().hex[:8]}"

    tasks = [
        _task(
            task_id=freeze_id,
            owner="lead",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Freeze the antigravity CLI bridge failure catalog, readiness semantics, merge-order, and validation gates.",
            files=[
                "core/core/external_ai_bridge.py",
                "core/core/integrations/antigravity_manager.py",
                "core/test/test_external_ai_bridge.py",
                "core/test/test_antigravity_manager.py",
            ],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Publishes machine-readable failure codes for cli resolution, auth/session, api-key permission, capacity exhaustion, and timeout masking.",
                "Defines stable semantics for models_probe, generation_probe, auth_probe, and api_probe.",
                "Unblocks parallel worker implementation without overlapping file ownership.",
            ],
            repo_path=repo_path,
            branch=branch,
        ),
        _task(
            task_id=cli_bridge_id,
            owner="cli_bridge",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Implement localized fixes in ExternalAIBridge for CLI resolution and upstream error taxonomy without changing manager aggregation.",
            files=[
                "core/core/external_ai_bridge.py",
            ],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS + ["Do not modify AntigravityManager in this task."],
            acceptance_criteria=[
                "Distinguishes cli_missing from auth and timeout paths.",
                "Preserves upstream 429/resource exhausted classification instead of collapsing it into generic timeout labels.",
                "Keeps router fallback behavior intact while improving machine-readable error typing.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=manager_id,
            owner="manager",
            task_type=TaskType.CODE,
            required_capability="code",
            description="Implement localized fixes in AntigravityManager status aggregation for structural, auth, API fallback, and live generation probes.",
            files=[
                "core/core/integrations/antigravity_manager.py",
            ],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS + ["Do not modify ExternalAIBridge in this task."],
            acceptance_criteria=[
                "Keeps models_probe, generation_probe, auth_probe, and api_probe semantically separated.",
                "Distinguishes OAuth/session issues from api_key permission failures.",
                "Does not misreport gemini --version as full inventory/live readiness.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=tests_id,
            owner="tests",
            task_type=TaskType.TEST,
            required_capability="test",
            description="Add targeted tests for the antigravity CLI bridge failure catalog and readiness aggregation contract.",
            files=[
                "core/test/test_external_ai_bridge.py",
                "core/test/test_antigravity_manager.py",
            ],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS + ["Do not broaden tests into unrelated providers or diagnostics layers."],
            acceptance_criteria=[
                "Covers cli_missing, model_capacity_exhausted, timeout masking upstream error, and api_key permission issue.",
                "Guards stable auth_mode and readiness aggregation semantics.",
                "Provides regression coverage for the observed direct CLI 429 vs manager timeout mismatch.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[freeze_id],
        ),
        _task(
            task_id=integrate_id,
            owner="integrator",
            task_type=TaskType.REVIEW,
            required_capability="review",
            description="Merge antigravity CLI bridge worker outputs while preserving payload stability and localized failure domains.",
            files=[
                "core/core/external_ai_bridge.py",
                "core/core/integrations/antigravity_manager.py",
                "core/test/test_external_ai_bridge.py",
                "core/test/test_antigravity_manager.py",
            ],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS + ["Do not let merged output reintroduce generic timeout/auth masking."],
            acceptance_criteria=[
                "Unified error taxonomy between bridge classification and manager status payloads.",
                "No readiness aggregation drift remains between structural and live probes.",
                "Targeted antigravity tests pass without widening unrelated provider behavior.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[cli_bridge_id, manager_id, tests_id],
        ),
        _task(
            task_id=validate_id,
            owner="validator",
            task_type=TaskType.TEST,
            required_capability="test",
            description="Run the final validation suite for the antigravity CLI bridge wave and summarize residual live-probe risks.",
            files=[],
            constraints=_ANTIGRAVITY_GLOBAL_CONSTRAINTS + ["Do not mark the wave complete without running the declared validation commands."],
            acceptance_criteria=[
                "Runs targeted pytest and task-board JSON smoke.",
                "Summarizes residual risk around live capacity exhaustion and API permission issues.",
                "Confirms the failure catalog stays localized to antigravity_cli_bridge.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[integrate_id],
        ),
    ]

    return ExecutionPlan(
        root_task_id=freeze_id,
        atomic_tasks=tasks,
        draft_layers=[
            {
                "name": "antigravity_cli_bridge_wave",
                "parallel": True,
                "objective": "Localize and repair antigravity CLI bridge failures without broad provider-layer refactoring.",
            }
        ],
    )



def build_antigravity_cli_bridge_task_board(*, repo_path: str | None = None, branch: str | None = None) -> dict[str, Any]:
    plan = build_antigravity_cli_bridge_execution_plan(repo_path=repo_path, branch=branch)
    prompts = {
        "lead": _antigravity_worker_prompt("lead"),
        "cli_bridge": _antigravity_worker_prompt("cli_bridge"),
        "manager": _antigravity_worker_prompt("manager"),
        "tests": _antigravity_worker_prompt("tests"),
        "integrator": _antigravity_worker_prompt("integrator"),
        "validator": _antigravity_worker_prompt("validator"),
    }
    tasks = []
    for task in plan.atomic_tasks:
        owner = str(task.routing_hints.get("worker_role", ""))
        tasks.append(
            {
                "task_id": task.task_id,
                "title": task.input.description,
                "owner": owner,
                "task_type": task.type.value,
                "required_capability": task.required_capability,
                "depends_on": list(task.dependencies),
                "files": list(task.input.files),
                "constraints": list(task.input.constraints),
                "acceptance_criteria": list(task.input.acceptance_criteria),
                "parallelizable": owner in {"cli_bridge", "manager", "tests"},
                "prompt": prompts[owner],
            }
        )

    return {
        "schema_version": ANTIGRAVITY_CLI_BRIDGE_TASK_BOARD_SCHEMA_VERSION,
        "objective": "Coordinate localized antigravity CLI bridge fixes across multiple AI workers without drifting into unrelated provider layers.",
        "context": {
            "failure_domain": "antigravity_cli_bridge",
            "repo_path": repo_path,
            "branch": branch,
            "observed_cli_command": ["/var/home/sanya/.npm-packages/bin/gemini"],
            "observed_direct_generation_probe": {
                "command": ["gemini", "-p", "healthcheck: reply with ok", "--skip-trust"],
                "error_signature": "429 RESOURCE_EXHAUSTED MODEL_CAPACITY_EXHAUSTED",
            },
            "observed_manager_status": {
                "ready": False,
                "models_probe_ok": True,
                "generation_probe_ok": False,
                "auth_probe_present": False,
                "api_probe_status_code": 403,
                "auth_mode": "api_key",
            },
            "observed_diagnostic_gap": "models_probe for gemini-backed mode is currently a binary/version surrogate, not a true inventory/live probe.",
        },
        "global_constraints": list(_ANTIGRAVITY_GLOBAL_CONSTRAINTS),
        "worker_roles": [
            {
                "owner": "lead",
                "responsibility": "Freeze failure taxonomy, readiness semantics, and merge sequencing.",
                "phase": "serial",
            },
            {
                "owner": "cli_bridge",
                "responsibility": "ExternalAIBridge CLI resolution and upstream error typing.",
                "phase": "parallel",
            },
            {
                "owner": "manager",
                "responsibility": "AntigravityManager probe semantics and readiness aggregation.",
                "phase": "parallel",
            },
            {
                "owner": "tests",
                "responsibility": "Targeted pytest coverage for bridge and manager failure modes.",
                "phase": "parallel",
            },
            {
                "owner": "integrator",
                "responsibility": "Merge worker outputs without reintroducing masking or payload drift.",
                "phase": "serial",
            },
            {
                "owner": "validator",
                "responsibility": "Run validation commands and publish residual-risk summary.",
                "phase": "serial",
            },
        ],
        "failure_code_catalog": _antigravity_failure_code_catalog(),
        "validation_commands": list(_ANTIGRAVITY_VALIDATION_COMMANDS),
        "merge_order": [
            "cli_bridge",
            "manager",
            "tests",
            "integrator",
            "validator",
        ],
        "tasks": tasks,
        "execution_plan": plan.as_dict(),
    }

