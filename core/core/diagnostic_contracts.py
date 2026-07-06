from __future__ import annotations

import os
import traceback
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from pydantic import Field

from .data_plane_monitor import (
    build_data_plane_snapshot,
    postgres_operator_hint,
    postgres_recovery_code,
    postgres_status_summary,
)
from .external_ai_bridge import ExternalAIBridge
from .antigravity_runtime_router import AntigravityRuntimeRouter
from .model_selector import ModelChoice
from .models import (
    CompatModel,
    Complexity,
    ExecutionPlan,
    Priority,
    Task,
    TaskAcceptance,
    TaskContext,
    TaskEnvelope,
    TaskInput,
    TaskStatus,
    TaskType,
)
from .provider_budget_router import ProviderBudgetRouter
from .provider_credentials import credential_snapshot
from .session_memory import MemoryScope, SessionMemory
from .transport_audit import build_transport_audit


DIAGNOSTIC_LAYER_ORDER: tuple[str, ...] = (
    "boot",
    "planning",
    "routing",
    "execution",
    "transport",
    "memory",
    "providers",
    "observability",
)
DIAGNOSTIC_SCHEMA_VERSION = "diagnostics.v1"

_DEFAULT_CONTEXT = TaskContext(project="diagnostics", repo_path=".", branch="main")
_MEMORY_PROBE_SESSION = "diagnostic-contracts"
_MEMORY_PROBE_KEY = "layer-probe"
_LIVE_PROVIDER_PROBE_ENV = "AI_BRIDGE_LIVE_MODEL_PROBE"
_PROVIDER_STRUCTURAL_HINT = "Check credential snapshot, CLI binary resolution, suppression snapshot, cached report, or runtime router model plan."
_PROVIDER_LIVE_HINT = "Trigger OpenAI registry refresh, Mistral /models fetch, Antigravity auth/models/generation probes, or MIMO runtime sweep."
_VFS_HINT = "Verify in-memory node roundtrip, file backend checksum validation, or artifact restoration."
_DATA_PLANE_HINT = "Check Postgres table snapshot, read/write probe status, RabbitMQ socket connectivity, or run a recovery dry-run summary."


class DiagnosticContractMetadata(CompatModel):
    summary: str
    entry_points: list[str]
    dependencies: list[str]
    inputs: list[str]
    outputs: list[str]
    invariants: list[str]
    failure_signatures: list[str]
    command_examples: list[str]
    test_targets: list[str]
    covered_modules: list[str]


class DiagnosticContract(CompatModel):
    layer: str
    metadata: DiagnosticContractMetadata


class DiagnosticCheckResult(CompatModel):
    layer: str
    ok: bool
    summary: str
    failures: list[str] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)


def _deep_probe(status: str, ok: bool, failure_code: str | None, recovery_hint: str | None, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(status),
        "ok": bool(ok),
        "failure_code": failure_code,
        "recovery_hint": recovery_hint,
        "details": details,
    }


def _contract(
    layer: str,
    *,
    summary: str,
    entry_points: list[str],
    dependencies: list[str],
    inputs: list[str],
    outputs: list[str],
    invariants: list[str],
    failure_signatures: list[str],
    command_examples: list[str],
    test_targets: list[str],
    covered_modules: list[str],
) -> DiagnosticContract:
    return DiagnosticContract(
        layer=layer,
        metadata=DiagnosticContractMetadata(
            summary=summary,
            entry_points=entry_points,
            dependencies=dependencies,
            inputs=inputs,
            outputs=outputs,
            invariants=invariants,
            failure_signatures=failure_signatures,
            command_examples=command_examples,
            test_targets=test_targets,
            covered_modules=covered_modules,
        ),
    )


_CONTRACTS: tuple[DiagnosticContract, ...] = (
    _contract(
        "boot",
        summary="Kernel boot contract verifies baseline module loading and state publication.",
        entry_points=[
            "core.core.orchestrator.Orchestrator.loaded_kernel_modules",
            "core.core.orchestrator.Orchestrator.module_state",
            "core.core.kernel_module_manager.KernelModuleManager.loaded_modules",
        ],
        dependencies=[
            "KernelModuleManager",
            "Orchestrator.module_state",
            "SelfDiagnosticModule.finalize",
        ],
        inputs=[
            "loaded kernel module names",
            "finalized module state",
            "orchestrator API bindings",
        ],
        outputs=[
            "ordered loaded module list",
            "missing baseline modules",
            "module state publication summary",
        ],
        invariants=[
            "Baseline modules load in deterministic order-independent checks.",
            "Boot contract does not trigger live bootstrap or network probes.",
            "Published module state is machine-readable dict data.",
        ],
        failure_signatures=[
            "module_manager_missing",
            "loaded_modules_missing",
            "baseline_modules_missing",
            "module_state_not_dict",
        ],
        command_examples=[
            "python -c \"from core.core.diagnostic_contracts import list_diagnostic_contracts; print(list_diagnostic_contracts()[0])\"",
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('boot', o))\"",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_contract_matrix_contains_required_metadata",
            "core/test/test_orchestrator.py::test_distribution_trace_shows_pipeline_and_agent_assignment",
        ],
        covered_modules=[
            "ai_activity",
            "orchestrator_control",
            "memory_control",
            "model_usage",
            "local_model_manager",
            "unified_vfs",
            "smart_decomposer",
            "prompt_optimizer",
            "chat_bus",
            "trigger_dispatcher",
            "cold_boot",
            "tdd_policy",
            "qwen_code",
            "readability_policy",
            "dev_toolkit",
            "self_diagnostic",
            "risk_advisor",
            "orchestrator_advisor",
            "intelligence",
            "security_sentinel",
        ],
    ),
    _contract(
        "planning",
        summary="Planning contract validates deterministic task decomposition over existing orchestrator models.",
        entry_points=[
            "core.core.task_decomposer.TaskDecomposer.decompose",
            "core.core.orchestrator.Orchestrator.decomposer",
            "core.core.model_selector.ModelSelector.select",
        ],
        dependencies=[
            "Task",
            "TaskInput",
            "TaskContext",
            "ExecutionPlan",
            "ModelSelector",
        ],
        inputs=[
            "root planning task",
            "task context",
            "routing hints and acceptance criteria",
        ],
        outputs=[
            "ExecutionPlan payload",
            "atomic task list",
            "assigned capabilities and models",
        ],
        invariants=[
            "ExecutionPlan.root_task_id matches the input task id.",
            "At least one atomic task is produced for a valid planning request.",
            "Atomic tasks publish required_capability values for downstream routing.",
        ],
        failure_signatures=[
            "decomposer_missing",
            "plan_type_mismatch",
            "plan_root_mismatch",
            "plan_atomic_tasks_empty",
            "plan_capability_missing",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('planning', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k planning -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_run_diagnostic_checks_with_real_orchestrator",
            "core/test/test_orchestrator.py::test_code_task_decomposition_can_fan_out_across_multiple_ai_agents",
        ],
        covered_modules=[
            "smart_decomposer",
            "prompt_optimizer",
            "tdd_policy",
            "qwen_code",
            "readability_policy",
        ],
    ),
    _contract(
        "routing",
        summary="Routing contract verifies task acceptance behavior and sourcecraft fallback without live providers.",
        entry_points=[
            "core.core.task_router.TaskRouter.route",
            "core.core.task_router.TaskRouter.route_envelope",
            "core.core.orchestrator.Orchestrator.router",
        ],
        dependencies=[
            "TaskRouter",
            "TaskAcceptance",
            "AgentRegistry",
            "LoadBalancer",
        ],
        inputs=[
            "decorated task",
            "capability hints",
            "agent registry snapshot",
        ],
        outputs=[
            "TaskAcceptance decision",
            "assigned agent id",
            "routing message and complexity class",
        ],
        invariants=[
            "Routing returns TaskAcceptance for every dry-run task.",
            "Sourcecraft-like tasks resolve deterministically even without specialist network agents.",
            "Diagnostic routing does not execute the task body.",
        ],
        failure_signatures=[
            "router_missing",
            "routing_acceptance_invalid",
            "routing_sourcecraft_not_accepted",
            "routing_assigned_agent_missing",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('routing', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k routing -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_run_diagnostic_checks_subset_preserves_order",
            "core/test/test_orchestrator.py::test_dependency_handoff_dispatches_p2p_context_to_next_agent",
        ],
        covered_modules=[
            "task_router",
            "load_balancer",
            "tdd_policy",
            "sourcecraft",
        ],
    ),
    _contract(
        "execution",
        summary="Execution contract validates envelope generation and delivery-health introspection without running agents.",
        entry_points=[
            "core.core.orchestrator.Orchestrator._delivery_envelope_for_task",
            "core.core.orchestrator.Orchestrator.delivery_health_snapshot",
            "core.core.orchestrator.Orchestrator.dispatch_envelope",
        ],
        dependencies=[
            "TaskEnvelope",
            "DeliverySupervisor",
            "MessageBus",
            "TaskPayload",
        ],
        inputs=[
            "task payload",
            "target agent id",
            "required capability",
        ],
        outputs=[
            "TaskEnvelope",
            "delivery snapshot",
            "retry/max-hop metadata",
        ],
        invariants=[
            "Envelope generation is deterministic for the same input task.",
            "Execution diagnostics do not submit work to live agents.",
            "Delivery-health snapshots are JSON-serializable dict structures.",
        ],
        failure_signatures=[
            "delivery_envelope_builder_missing",
            "delivery_envelope_invalid",
            "delivery_snapshot_not_dict",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('execution', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k execution -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_run_diagnostic_checks_with_real_orchestrator",
            "core/test/test_orchestrator_bus.py",
        ],
        covered_modules=[
            "delivery_supervisor",
            "message_bus",
            "orchestrator",
        ],
    ),

_contract(
    "transport",
    summary="Transport contract audits which orchestration paths are WS, HTTP, MessageBus, or direct in-process calls.",
    entry_points=[
        "core.core.transport_audit.build_transport_audit",
        "core.core.orchestrator.Orchestrator.build_transport_audit",
        "core.scripts.orchestrator_daemon._build_http_app",
    ],
    dependencies=[
        "FastAPI",
        "WebSocket",
        "MessageBus",
        "InventoryStreamHub",
    ],
    inputs=[
        "orchestrator runtime bindings",
        "message bus backend",
        "inventory stream hub presence",
    ],
    outputs=[
        "transport summary",
        "subsystem classification",
        "safe ws migration plan",
    ],
    invariants=[
        "Audit is descriptive and must not require live network access.",
        "Control-plane HTTP endpoints stay explicitly classified as HTTP.",
        "WS-only readiness is reported as a fact, not forced as a passing condition.",
    ],
    failure_signatures=[
        "transport_audit_invalid",
        "transport_summary_missing",
        "transport_subsystems_invalid",
        "transport_migration_plan_missing",
    ],
    command_examples=[
        "python -c \"from core.core.orchestrator import Orchestrator; o=Orchestrator(); print(o.build_transport_audit())\"",
        "pytest core/test/test_self_diagnostic_module.py -k transport -q",
    ],
    test_targets=[
        "core/test/test_self_diagnostic_module.py::test_run_diagnostics_includes_transport_audit_when_requested",
        "core/test/test_orchestrator_daemon_diagnostics.py::test_transport_audit_route_returns_payload",
    ],
    covered_modules=[
        "orchestrator",
        "message_bus",
        "rabbitmq_bus",
        "inventory_stream_hub",
        "self_diagnostic",
    ],
),
    _contract(
        "memory",
        summary="Memory contract performs a local roundtrip probe against SessionMemory and validates cleanup.",
        entry_points=[
            "core.core.session_memory.SessionMemory.roundtrip_check",
            "core.core.session_memory.SessionMemory.diagnostic_snapshot",
            "core.core.orchestrator.Orchestrator.get_memory",
        ],
        dependencies=[
            "SessionMemory",
            "MemoryScope",
            "HybridMemory",
            "MemoryPolicy",
        ],
        inputs=[
            "session id",
            "probe key",
            "probe payload",
        ],
        outputs=[
            "roundtrip verification payload",
            "memory topology snapshot",
            "cleanup result",
        ],
        invariants=[
            "Probe writes and reads stay local to SessionMemory.",
            "Probe key is removed after the diagnostic finishes.",
            "Diagnostic snapshot exposes machine-readable counts and sample keys.",
        ],
        failure_signatures=[
            "memory_missing",
            "memory_roundtrip_failed",
            "memory_snapshot_not_dict",
            "memory_probe_cleanup_failed",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('memory', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k memory -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_memory_layer_cleans_up_probe_key",
            "core/test/test_core_integrity.py::test_session_memory_basic_contract",
        ],
        covered_modules=[
            "session_memory",
            "memory_control",
            "layered_context_memory",
        ],
    ),
    _contract(
        "providers",
        summary="Providers contract inspects provider fallback policy and cached provider state without live network calls.",
        entry_points=[
            "core.core.provider_budget_router.ProviderBudgetRouter.preferred_providers",
            "core.core.provider_budget_router.ProviderBudgetRouter.suppression_snapshot",
            "core.core.availability.ModelAvailability.cached_report",
        ],
        dependencies=[
            "ProviderBudgetRouter",
            "ModelChoice",
            "ModelSelector",
            "HealthChecker",
        ],
        inputs=[
            "sample task",
            "selected model choice",
            "cached provider health",
        ],
        outputs=[
            "ordered fallback chain",
            "suppression snapshot",
            "cached provider report keys",
        ],
        invariants=[
            "Provider chain is non-empty and duplicate-free.",
            "Diagnostics rely on cached provider state only.",
            "No live TCP, HTTP, CLI, or SDK probe is required.",
        ],
        failure_signatures=[
            "provider_router_missing",
            "provider_chain_empty",
            "provider_chain_duplicated",
            "provider_cached_report_invalid",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('providers', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k providers -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_run_diagnostic_checks_avoids_live_provider_calls",
            "core/test/test_orchestrator_policy.py",
        ],
        covered_modules=[
            "provider_budget_router",
            "availability",
            "model_selector",
            "antigravity_status",
        ],
    ),
    _contract(
        "observability",
        summary="Observability contract validates metrics, module-state, and KPI log bindings for machine-readable diagnostics.",
        entry_points=[
            "core.core.metrics.MetricsCollector.snapshot",
            "core.core.orchestrator.Orchestrator.module_state",
            "core.core.kpi_event_logger.KPIEventLogger.from_env",
        ],
        dependencies=[
            "MetricsCollector",
            "KPIEventLogger",
            "UserConsole",
            "SelfDiagnosticModule",
        ],
        inputs=[
            "metrics collector state",
            "module_state payload",
            "console and KPI logger bindings",
        ],
        outputs=[
            "metrics snapshot",
            "module_state size",
            "log target paths",
        ],
        invariants=[
            "Observability diagnostics emit plain dict payloads.",
            "Metrics snapshots include counters and agent metrics keys.",
            "KPI logger bindings resolve to deterministic filesystem paths.",
        ],
        failure_signatures=[
            "metrics_missing",
            "metrics_snapshot_invalid",
            "observability_module_state_invalid",
            "kpi_logger_missing",
        ],
        command_examples=[
            "python -c \"from core.core.orchestrator import Orchestrator; from core.core.diagnostic_contracts import run_layer_diagnostic_check; o=Orchestrator(); print(run_layer_diagnostic_check('observability', o))\"",
            "pytest core/test/test_diagnostic_contracts.py -k observability -q",
        ],
        test_targets=[
            "core/test/test_diagnostic_contracts.py::test_run_diagnostic_checks_with_real_orchestrator",
            "core/test/test_self_diagnostic_module.py::test_run_diagnostics_structure",
        ],
        covered_modules=[
            "metrics",
            "self_diagnostic",
            "model_usage",
            "local_model_manager",
            "ai_activity",
        ],
    ),
)

_CONTRACT_BY_LAYER = {contract.layer: contract for contract in _CONTRACTS}


def build_diagnostic_contract_matrix() -> dict[str, dict[str, Any]]:
    return {contract.layer: contract.metadata.as_dict() for contract in _CONTRACTS}


def available_layers() -> list[str]:
    return list(DIAGNOSTIC_LAYER_ORDER)


def diagnostic_matrix() -> dict[str, Any]:
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "layers": build_diagnostic_contract_matrix(),
        "order": list(DIAGNOSTIC_LAYER_ORDER),
    }


def list_diagnostic_contracts() -> list[dict[str, Any]]:
    return [contract.as_dict() for contract in _CONTRACTS]


def get_diagnostic_contract(layer: str) -> dict[str, Any]:
    contract = _CONTRACT_BY_LAYER.get(str(layer).strip().lower())
    if contract is None:
        raise ValueError(f"Unknown diagnostic layer: {layer}")
    return contract.as_dict()


def _normalize_layers(layers: Iterable[str] | None) -> list[str]:
    if layers is None:
        return list(DIAGNOSTIC_LAYER_ORDER)
    normalized: list[str] = []
    seen: set[str] = set()
    for layer in layers:
        name = str(layer).strip().lower()
        if name not in _CONTRACT_BY_LAYER:
            raise ValueError(f"Unknown diagnostic layer: {layer}")
        if name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


def _get_context(api: Any, key: str, default: Any = None) -> Any:
    if api is None:
        return default
    getter = getattr(api, "get_context", None)
    if callable(getter):
        try:
            value = getter(key)
            return default if value is None else value
        except Exception:
            return default
    return getattr(api, key, default)


def _resolve_module(api: Any, module_name: str) -> Any | None:
    if api is None:
        return None
    getter = getattr(api, "get_module", None)
    if callable(getter):
        try:
            module = getter(module_name)
            if module is not None:
                return module
        except Exception:
            pass
    manager = getattr(api, "module_manager", None)
    getter = getattr(manager, "get_module", None)
    if callable(getter):
        try:
            return getter(module_name)
        except Exception:
            return None
    return _get_context(api, module_name)


def _module_state(api: Any) -> dict[str, Any]:
    if api is None:
        return {}
    state_fn = getattr(api, "module_state", None)
    if callable(state_fn):
        try:
            state = state_fn()
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}
    state = _get_context(api, "module_state", {})
    return state if isinstance(state, dict) else {}


def _loaded_modules(api: Any) -> list[str]:
    if api is None:
        return []
    for attr_name in ("loaded_kernel_modules", "loaded_modules"):
        method = getattr(api, attr_name, None)
        if callable(method):
            try:
                loaded = method()
                if isinstance(loaded, list):
                    return sorted(str(item) for item in loaded)
            except Exception:
                continue
    manager = getattr(api, "module_manager", None)
    method = getattr(manager, "loaded_modules", None)
    if callable(method):
        try:
            loaded = method()
            if isinstance(loaded, list):
                return sorted(str(item) for item in loaded)
        except Exception:
            return []
    return []


def _sample_task(task_type: TaskType, description: str, *, priority: Priority = Priority.NORMAL) -> Task:
    return Task(
        task_type,
        TaskInput(
            description,
            files=["core/core/orchestrator.py"],
            acceptance_criteria=["machine-readable diagnostics available"],
        ),
        _DEFAULT_CONTEXT,
        priority,
    )


def _boot_check(api: Any) -> DiagnosticCheckResult:
    if api is None:
        return DiagnosticCheckResult(
            layer="boot",
            ok=False,
            summary="Boot diagnostics require an orchestrator-like API.",
            failures=["module_manager_missing", "loaded_modules_missing"],
            observed={"loaded_modules": [], "module_state_keys": []},
        )
    loaded = _loaded_modules(api)
    module_state = _module_state(api)
    expected = list(_CONTRACT_BY_LAYER["boot"].metadata.covered_modules)
    missing = [name for name in expected if name not in loaded]
    failures: list[str] = []
    if not loaded:
        failures.append("loaded_modules_missing")
    if missing:
        failures.append("baseline_modules_missing")
    if not isinstance(module_state, dict):
        failures.append("module_state_not_dict")
    ok = not failures
    return DiagnosticCheckResult(
        layer="boot",
        ok=ok,
        summary="Boot baseline modules loaded and module_state published." if ok else "Boot baseline modules are incomplete.",
        failures=failures,
        observed={
            "loaded_modules": loaded,
            "loaded_count": len(loaded),
            "missing_modules": missing,
            "module_state_keys": sorted(module_state),
        },
    )


def _planning_check(api: Any) -> DiagnosticCheckResult:
    decomposer = getattr(api, "decomposer", None) if api is not None else None
    if decomposer is None or not hasattr(decomposer, "decompose"):
        return DiagnosticCheckResult(
            layer="planning",
            ok=False,
            summary="Planning diagnostics require a TaskDecomposer binding.",
            failures=["decomposer_missing"],
            observed={},
        )
    task = _sample_task(TaskType.PLAN, "Prepare a deterministic diagnostics rollout plan.")
    failures: list[str] = []
    plan = decomposer.decompose(task)
    if not isinstance(plan, ExecutionPlan):
        failures.append("plan_type_mismatch")
    atomic_tasks = list(getattr(plan, "atomic_tasks", []) or [])
    if getattr(plan, "root_task_id", None) != task.task_id:
        failures.append("plan_root_mismatch")
    if not atomic_tasks:
        failures.append("plan_atomic_tasks_empty")
    missing_caps = [item.task_id for item in atomic_tasks if not getattr(item, "required_capability", None)]
    if missing_caps:
        failures.append("plan_capability_missing")
    ok = not failures
    return DiagnosticCheckResult(
        layer="planning",
        ok=ok,
        summary="Planning dry-run produced a stable execution plan." if ok else "Planning dry-run violated execution-plan invariants.",
        failures=failures,
        observed={
            "root_task_id": getattr(plan, "root_task_id", None),
            "atomic_task_count": len(atomic_tasks),
            "atomic_task_types": [item.type.value for item in atomic_tasks],
            "required_capabilities": [str(item.required_capability or "") for item in atomic_tasks],
            "assigned_models": [str(item.assigned_model or "") for item in atomic_tasks],
            "missing_capability_task_ids": missing_caps,
        },
    )


def _routing_check(api: Any) -> DiagnosticCheckResult:
    router = getattr(api, "router", None) if api is not None else None
    registry = getattr(api, "registry", None) if api is not None else None
    if router is None or not hasattr(router, "route_envelope"):
        return DiagnosticCheckResult(
            layer="routing",
            ok=False,
            summary="Routing diagnostics require a TaskRouter binding.",
            failures=["router_missing"],
            observed={},
        )
    task = _sample_task(TaskType.RESEARCH, "Inspect repository branch status and PR routing.")
    task.required_capability = "sourcecraft"
    if api is not None and hasattr(api, "_delivery_envelope_for_task"):
        envelope = api._delivery_envelope_for_task(task, "orchestrator", "sourcecraft")
    else:
        envelope = TaskEnvelope(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            trace_id=task.task_id,
            target_capability="sourcecraft",
            payload={
                "objective": task.input.description,
                "context": {"project": task.context.project, "repo_path": task.context.repo_path, "branch": task.context.branch},
                "acceptance_criteria": list(task.input.acceptance_criteria),
                "artifacts": list(task.input.files),
            },
        )
    acceptance = router.route_envelope(envelope)
    if registry is not None and getattr(acceptance, "assigned_agent", None):
        assigned = registry.get(acceptance.assigned_agent)
        if assigned is not None:
            assigned.metrics.queue_depth = max(0, int(getattr(assigned.metrics, "queue_depth", 0)) - 1)
    failures: list[str] = []
    if not isinstance(acceptance, TaskAcceptance):
        failures.append("routing_acceptance_invalid")
    if getattr(acceptance, "status", None) != TaskStatus.ACCEPTED:
        failures.append("routing_sourcecraft_not_accepted")
    if not getattr(acceptance, "assigned_agent", None):
        failures.append("routing_assigned_agent_missing")
    ok = not failures
    return DiagnosticCheckResult(
        layer="routing",
        ok=ok,
        summary="Routing dry-run produced a deterministic acceptance decision." if ok else "Routing dry-run violated acceptance invariants.",
        failures=failures,
        observed={
            "task_id": task.task_id,
            "target_capability": getattr(envelope, "target_capability", None),
            "assigned_agent": getattr(acceptance, "assigned_agent", None),
            "status": getattr(getattr(acceptance, "status", None), "value", getattr(acceptance, "status", None)),
            "complexity": getattr(acceptance, "complexity", None),
            "message": getattr(acceptance, "message", ""),
        },
    )


def _execution_check(api: Any) -> DiagnosticCheckResult:
    if api is None or not hasattr(api, "_delivery_envelope_for_task"):
        return DiagnosticCheckResult(
            layer="execution",
            ok=False,
            summary="Execution diagnostics require a delivery-envelope builder.",
            failures=["delivery_envelope_builder_missing"],
            observed={},
        )
    task = _sample_task(TaskType.CODE, "Prepare an execution envelope without dispatching work.")
    envelope = api._delivery_envelope_for_task(task, "orchestrator", "sourcecraft")
    snapshot_fn = getattr(api, "delivery_health_snapshot", None)
    delivery_snapshot = snapshot_fn() if callable(snapshot_fn) else {}
    failures: list[str] = []
    if not isinstance(envelope, TaskEnvelope):
        failures.append("delivery_envelope_invalid")
    if not isinstance(delivery_snapshot, dict):
        failures.append("delivery_snapshot_not_dict")
    ok = not failures
    return DiagnosticCheckResult(
        layer="execution",
        ok=ok,
        summary="Execution dry-run built a stable delivery envelope." if ok else "Execution dry-run violated envelope invariants.",
        failures=failures,
        observed={
            "task_id": task.task_id,
            "target_agent": getattr(envelope, "target_agent", None),
            "target_capability": getattr(envelope, "target_capability", None),
            "payload_objective": getattr(getattr(envelope, "payload", None), "objective", None),
            "max_hops": getattr(envelope, "max_hops", None),
            "max_retries": getattr(envelope, "max_retries", None),
            "delivery_snapshot_keys": sorted(delivery_snapshot) if isinstance(delivery_snapshot, dict) else [],
        },
    )


def _resolve_memory(api: Any) -> SessionMemory | None:
    if api is None:
        return None
    getter = getattr(api, "get_memory", None)
    if callable(getter):
        try:
            memory = getter()
            if isinstance(memory, SessionMemory):
                return memory
        except Exception:
            return None
    memory = _get_context(api, "session_memory")
    return memory if isinstance(memory, SessionMemory) else None


def _transport_check(api: Any) -> DiagnosticCheckResult:
    audit = build_transport_audit(api)
    failures: list[str] = []
    summary = audit.get('summary') if isinstance(audit, dict) else None
    subsystems = audit.get('subsystems') if isinstance(audit, dict) else None
    migration_plan = audit.get('migration_plan') if isinstance(audit, dict) else None
    message_bus = audit.get('message_bus') if isinstance(audit, dict) else None
    if not isinstance(summary, dict):
        failures.append('transport_summary_missing')
    if not isinstance(subsystems, list):
        failures.append('transport_subsystems_invalid')
    if not isinstance(migration_plan, list) or not migration_plan:
        failures.append('transport_migration_plan_missing')
    if not isinstance(message_bus, dict):
        failures.append('transport_audit_invalid')
    ok = not failures
    return DiagnosticCheckResult(
        layer='transport',
        ok=ok,
        summary='Transport audit completed and classified runtime paths.' if ok else 'Transport audit payload is incomplete.',
        failures=failures,
        observed=audit if isinstance(audit, dict) else {},
    )


def _memory_check(api: Any) -> DiagnosticCheckResult:
    memory = _resolve_memory(api)
    if memory is None:
        return DiagnosticCheckResult(
            layer="memory",
            ok=False,
            summary="Memory diagnostics require SessionMemory.",
            failures=["memory_missing"],
            observed={},
        )
    failures: list[str] = []
    probe = memory.roundtrip_check(
        session_id=_MEMORY_PROBE_SESSION,
        key=_MEMORY_PROBE_KEY,
        value={"layer": "memory", "sequence": 1},
        scope=MemoryScope.SESSION,
    )
    snapshot = memory.diagnostic_snapshot(session_id=_MEMORY_PROBE_SESSION)
    if not probe.get("ok"):
        failures.append("memory_roundtrip_failed")
    if not isinstance(snapshot, dict):
        failures.append("memory_snapshot_not_dict")
    memory.delete(MemoryScope.SESSION, _MEMORY_PROBE_SESSION, _MEMORY_PROBE_KEY)
    residual = memory.list_keys(MemoryScope.SESSION, _MEMORY_PROBE_SESSION)
    if any(key.endswith(f":{_MEMORY_PROBE_KEY}") for key in residual):
        failures.append("memory_probe_cleanup_failed")
    vfs_probe = _vfs_probe(api)
    if not vfs_probe["ok"]:
        failures.append(str(vfs_probe.get("failure_code") or "vfs_read_write_failed").lower())
    data_plane_probe = _data_plane_probe()
    if not data_plane_probe["ok"] and bool(data_plane_probe["details"].get("configured")):
        failures.append(str(data_plane_probe.get("failure_code") or "data_plane_read_write_failed").lower())
    ok = not failures
    return DiagnosticCheckResult(
        layer="memory",
        ok=ok,
        summary="Memory roundtrip probe completed and cleaned up." if ok else "Memory roundtrip probe failed or leaked state.",
        failures=failures,
        observed={
            "probe": probe,
            "snapshot": snapshot,
            "residual_keys": residual,
            "vfs_probe": vfs_probe,
            "data_plane_probe": data_plane_probe,
        },
    )


def _vfs_probe(api: Any) -> dict[str, Any]:
    vfs = _resolve_module(api, "unified_vfs")
    if vfs is None or not hasattr(vfs, "write_state") or not hasattr(vfs, "read_state"):
        return _deep_probe("skipped", True, None, None, {"available": False})

    path = "diagnostics/contracts/vfs_probe"
    content = {"probe": True, "layer": "memory", "path": path}
    details: dict[str, Any] = {"available": True, "path": path, "storage": "unknown"}
    try:
        finalized = vfs.finalize() if hasattr(vfs, "finalize") else {}
        if isinstance(finalized, dict):
            details["storage"] = finalized.get("storage", "unknown")
            details["node_count"] = finalized.get("node_count")
        write_ok = bool(vfs.write_state(path, content, "diagnostic-contracts", metadata={"probe": True}))
        node = vfs.read_state(path)
        read_write_ok = bool(write_ok and node is not None and getattr(node, "content", None) == content)
        checksum_fn = getattr(vfs, "_calculate_checksum", None)
        integrity_ok = bool(node is not None)
        if callable(checksum_fn) and node is not None:
            integrity_ok = checksum_fn(node.content) == getattr(node, "checksum", None)
        recovered = node
        nodes = getattr(vfs, "_nodes", None)
        if isinstance(nodes, dict):
            nodes.pop(path, None)
            recovered = vfs.read_state(path)
        data_present = bool(recovered is not None)
        recovery_ok = bool(recovered is not None and getattr(recovered, "content", None) == content)
        details.update(
            {
                "backend": "postgresql" if bool(getattr(vfs, "_pg_enabled", False)) else "filesystem",
                "read_write_ok": read_write_ok,
                "integrity_ok": integrity_ok,
                "data_present": data_present,
                "recovery_ok": recovery_ok,
                "integrity": getattr(getattr(recovered, "integrity", None), "value", getattr(recovered, "integrity", None)),
            }
        )
        if not write_ok or not read_write_ok:
            return _deep_probe("error", False, "VFS_READ_WRITE_FAILED", _VFS_HINT, details)
        if not integrity_ok:
            return _deep_probe("error", False, "VFS_INTEGRITY_MISMATCH", _VFS_HINT, details)
        if not recovery_ok:
            return _deep_probe("degraded", False, "VFS_READ_WRITE_FAILED", _VFS_HINT, details)
        return _deep_probe("ok", True, None, None, details)
    except Exception as exc:
        details["error"] = str(exc)
        return _deep_probe("error", False, "VFS_READ_WRITE_FAILED", _VFS_HINT, details)


def _data_plane_probe() -> dict[str, Any]:
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    rabbitmq_url = os.getenv("AI_BRIDGE_RABBITMQ_URL", "").strip() or None
    if not database_url:
        return _deep_probe(
            "skipped",
            True,
            None,
            None,
            {
                "configured": False,
                "read_write_ok": None,
                "integrity_ok": None,
                "data_present": None,
                "recovery_ok": None,
            },
        )
    snapshot = build_data_plane_snapshot(database_url=database_url, rabbitmq_url=rabbitmq_url)
    summary = postgres_status_summary(snapshot)
    row_counts = dict(summary.get("row_counts") or {})
    read_write_ok = bool(summary.get("probe_ok"))
    integrity_ok = snapshot.postgres_state not in {"unavailable", "read_write_failed", "missing"}
    data_present = any(int(value) > 0 for value in row_counts.values())
    recovery_code = str(summary.get("recovery_code") or "")
    recovery_ok = recovery_code in {"OK", "POSTGRES_EMPTY_SCHEMA"}
    details = {
        "configured": True,
        "postgres_state": snapshot.postgres_state,
        "postgres_error": snapshot.postgres_error,
        "rabbitmq_ok": snapshot.rabbitmq_ok,
        "rabbitmq_target": snapshot.rabbitmq_target,
        "read_write_ok": read_write_ok,
        "integrity_ok": integrity_ok,
        "data_present": data_present,
        "recovery_ok": recovery_ok,
        "summary": summary.get("summary"),
        "row_counts": row_counts,
        "recovery_code": recovery_code,
        "operator_hint": postgres_operator_hint(recovery_code),
        "probe": snapshot.probe,
        "recovery": summary.get("recovery"),
    }
    if not bool(snapshot.rabbitmq_ok):
        return _deep_probe("degraded", False, "RABBITMQ_UNREACHABLE", _DATA_PLANE_HINT, details)
    if not read_write_ok or snapshot.postgres_state in {"unavailable", "missing", "read_write_failed"}:
        return _deep_probe("error", False, "DATA_PLANE_READ_WRITE_FAILED", _DATA_PLANE_HINT, details)
    if snapshot.postgres_state == "empty":
        return _deep_probe("degraded", False, "DATA_PLANE_EMPTY", _DATA_PLANE_HINT, details)
    if not recovery_ok:
        return _deep_probe("degraded", False, "DATA_PLANE_RECOVERY_FAILED", _DATA_PLANE_HINT, details)
    return _deep_probe("ok", True, None, None, details)


def _provider_choice(api: Any) -> ModelChoice:
    selector = getattr(api, "model_selector", None) if api is not None else None
    task = _sample_task(TaskType.CODE, "Rename a local symbol with deterministic routing.")
    if selector is not None and hasattr(selector, "select"):
        return selector.select(task)
    return ModelChoice(model_name="diagnostic-local", provider="local", complexity=Complexity.LOW)


def _provider_structural_probe(
    provider: str,
    cached: dict[str, Any],
    antigravity_state: dict[str, Any],
    router_plan: dict[str, Any],
    choice: ModelChoice,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "provider": provider,
        "selected": choice.provider == provider,
        "cached_keys": sorted(cached) if isinstance(cached, dict) else [],
    }
    failure_code: str | None = None
    if provider == "local":
        model = os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "").strip()
        details.update(
            {
                "reachable": True,
                "authenticated": True,
                "inventory_ok": bool(model or choice.provider == "local"),
                "configured_model": model or choice.model_name,
            }
        )
        if not details["inventory_ok"]:
            failure_code = "PROVIDER_INVENTORY_EMPTY"
    elif provider == "antigravity":
        credential = credential_snapshot(("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
        cli_cmd = ExternalAIBridge.resolve_antigravity_cli_command()
        auth_diag = ExternalAIBridge.antigravity_auth_diagnostics()
        cached_models = antigravity_state.get("models", []) if isinstance(antigravity_state, dict) else []
        inventory_ok = bool(antigravity_state.get("inventory_ok")) if isinstance(antigravity_state, dict) else False
        inventory_source = str(antigravity_state.get("inventory_source") or "unavailable") if isinstance(antigravity_state, dict) else "unavailable"
        inventory_probe_kind = str(antigravity_state.get("inventory_probe_kind") or "unknown") if isinstance(antigravity_state, dict) else "unknown"
        details.update(
            {
                "reachable": bool(cli_cmd or os.getenv("AI_BRIDGE_ANTIGRAVITY_PROXY_URL", "").strip()),
                "authenticated": bool(credential.get("usable") or auth_diag.get("settings_present")),
                "inventory_ok": inventory_ok,
                "inventory_source": inventory_source,
                "inventory_probe_kind": inventory_probe_kind,
                "cached_models": cached_models,
                "credential": credential,
                "cli_command": cli_cmd,
                "auth_diagnostics": auth_diag,
                "router_plan": router_plan,
            }
        )
        if not details["reachable"]:
            failure_code = "PROVIDER_UNREACHABLE"
        elif not details["authenticated"]:
            failure_code = "PROVIDER_AUTH_FAILED"
        elif not inventory_ok:
            failure_code = "PROVIDER_INVENTORY_EMPTY"
    elif provider == "mistral":
        credential = credential_snapshot(("MISTRAL_API_KEY",))
        base_url = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()
        cached_diag = cached.get("diagnostics", {}) if isinstance(cached.get("diagnostics"), dict) else {}
        details.update(
            {
                "reachable": bool(base_url),
                "authenticated": bool(credential.get("usable")),
                "inventory_ok": bool(cached_diag.get("models", [])),
                "credential": credential,
                "base_url": base_url,
            }
        )
        if not details["authenticated"]:
            failure_code = "PROVIDER_AUTH_FAILED"
        elif not details["inventory_ok"] and cached:
            failure_code = "PROVIDER_INVENTORY_EMPTY"
    else:
        credential = credential_snapshot(("OPENAI_API_KEY",))
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
        cached_diag = cached.get("diagnostics", {}) if isinstance(cached.get("diagnostics"), dict) else {}
        configured_models = cached_diag.get("configured_models", []) if isinstance(cached_diag, dict) else []
        inventory_ok = bool(cached_diag.get("models", [])) if isinstance(cached_diag, dict) else False
        details.update(
            {
                "reachable": bool(base_url),
                "authenticated": bool(credential.get("usable")),
                "inventory_ok": inventory_ok or bool(configured_models),
                "credential": credential,
                "base_url": base_url,
                "registry": cached_diag.get("registry", {}) if isinstance(cached_diag, dict) else {},
                "configured_models": configured_models,
            }
        )
        if not details["authenticated"]:
            failure_code = "PROVIDER_AUTH_FAILED"
        elif not details["inventory_ok"] and cached:
            failure_code = "PROVIDER_INVENTORY_EMPTY"

    status = "ok" if failure_code is None else "degraded"
    return _deep_probe(status, failure_code is None, failure_code, _PROVIDER_STRUCTURAL_HINT if failure_code else None, details)


def _provider_live_probe(provider: str, availability: Any, live_enabled: bool) -> dict[str, Any]:
    if not live_enabled:
        return _deep_probe("skipped", True, None, None, {"live_ok": None, "enabled": False})
    if availability is None or not hasattr(availability, "check_provider"):
        return _deep_probe("error", False, "PROVIDER_LIVE_PROBE_FAILED", _PROVIDER_LIVE_HINT, {"live_ok": False, "enabled": True, "error": "availability_missing"})
    try:
        health = availability.check_provider(provider, live=True)
        payload = health.as_dict() if hasattr(health, "as_dict") else {}
        status = str(payload.get("status") or "unknown")
        live_ok = status == "healthy"
        failure_code = None if live_ok else "PROVIDER_LIVE_PROBE_FAILED"
        return _deep_probe("ok" if live_ok else "degraded", live_ok, failure_code, _PROVIDER_LIVE_HINT if failure_code else None, {"live_ok": live_ok, "enabled": True, "health": payload})
    except Exception as exc:
        return _deep_probe("error", False, "PROVIDER_LIVE_PROBE_FAILED", _PROVIDER_LIVE_HINT, {"live_ok": False, "enabled": True, "error": str(exc)})


def _provider_probe_details(
    api: Any,
    task: Task,
    choice: ModelChoice,
    cached_report: dict[str, Any],
    antigravity_state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    live_enabled = os.getenv(_LIVE_PROVIDER_PROBE_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}
    availability = getattr(api, "availability", None) if api is not None else None
    try:
        raw_router_plan = AntigravityRuntimeRouter().build_plan(task, task.input.description)
        if hasattr(raw_router_plan, "as_dict"):
            router_plan = raw_router_plan.as_dict()
        elif is_dataclass(raw_router_plan):
            router_plan = asdict(raw_router_plan)
        elif hasattr(raw_router_plan, "__dict__"):
            router_plan = dict(vars(raw_router_plan))
        else:
            router_plan = {"value": str(raw_router_plan)}
    except Exception as exc:
        router_plan = {"error": str(exc)}

    providers: dict[str, dict[str, Any]] = {}
    for provider in ("local", "antigravity", "mistral", "openai"):
        cached = cached_report.get(provider, {}) if isinstance(cached_report, dict) else {}
        structural = _provider_structural_probe(provider, cached, antigravity_state, router_plan, choice)
        live_probe = _provider_live_probe(provider, availability, live_enabled)
        providers[provider] = {
            "reachable": structural["details"].get("reachable"),
            "authenticated": structural["details"].get("authenticated"),
            "inventory_ok": structural["details"].get("inventory_ok"),
            "live_ok": live_probe["details"].get("live_ok"),
            "structural_probe": structural,
            "live_probe": live_probe,
            "cached_status": cached.get("status") if isinstance(cached, dict) else None,
        }
    return providers


def _providers_check(api: Any) -> DiagnosticCheckResult:
    router = getattr(api, "provider_budget_router", None) if api is not None else ProviderBudgetRouter()
    if router is None or not hasattr(router, "preferred_providers"):
        return DiagnosticCheckResult(
            layer="providers",
            ok=False,
            summary="Provider diagnostics require ProviderBudgetRouter.",
            failures=["provider_router_missing"],
            observed={},
        )
    task = _sample_task(TaskType.CODE, "Select a deterministic provider fallback chain.")
    choice = _provider_choice(api)
    chain = list(router.preferred_providers(task, choice))
    cached_report: dict[str, Any] = {}
    availability = getattr(api, "availability", None) if api is not None else None
    if availability is not None and hasattr(availability, "cached_report"):
        report = availability.cached_report()
        cached_report = report if isinstance(report, dict) else {}
    healthcheck = getattr(api, "healthcheck", None) if api is not None else None
    antigravity_state: dict[str, Any] = {}
    if healthcheck is not None and hasattr(healthcheck, "antigravity_state"):
        state = healthcheck.antigravity_state()
        antigravity_state = state if isinstance(state, dict) else {}
    provider_details = _provider_probe_details(api, task, choice, cached_report, antigravity_state)
    failures: list[str] = []
    if not chain:
        failures.append("provider_chain_empty")
    if len(chain) != len(set(chain)):
        failures.append("provider_chain_duplicated")
    if not isinstance(cached_report, dict):
        failures.append("provider_cached_report_invalid")
    for provider_name, payload in provider_details.items():
        if not isinstance(payload, dict):
            failures.append(f"provider_payload_invalid:{provider_name}")
            continue
        for key in ("structural_probe", "live_probe"):
            if not isinstance(payload.get(key), dict):
                failures.append(f"provider_payload_invalid:{provider_name}:{key}")
    ok = not failures
    return DiagnosticCheckResult(
        layer="providers",
        ok=ok,
        summary="Provider diagnostics validated cached fallback policy." if ok else "Provider diagnostics found invalid fallback-policy state.",
        failures=failures,
        observed={
            "selected_provider": choice.provider,
            "selected_model": choice.model_name,
            "preferred_providers": chain,
            "suppressed_providers": router.suppression_snapshot(),
            "cached_provider_keys": sorted(cached_report),
            "antigravity_cached_keys": sorted(antigravity_state),
            "providers": provider_details,
        },
    )


def _observability_check(api: Any) -> DiagnosticCheckResult:
    metrics = getattr(api, "metrics", None) if api is not None else None
    kpi_events = getattr(api, "kpi_events", None) if api is not None else None
    if metrics is None or not hasattr(metrics, "snapshot"):
        return DiagnosticCheckResult(
            layer="observability",
            ok=False,
            summary="Observability diagnostics require MetricsCollector.",
            failures=["metrics_missing"],
            observed={},
        )
    snapshot = metrics.snapshot()
    module_state = _module_state(api)
    failures: list[str] = []
    if not isinstance(snapshot, dict):
        failures.append("metrics_snapshot_invalid")
    if not isinstance(module_state, dict):
        failures.append("observability_module_state_invalid")
    if kpi_events is None:
        failures.append("kpi_logger_missing")
    ok = not failures
    return DiagnosticCheckResult(
        layer="observability",
        ok=ok,
        summary="Observability bindings are machine-readable." if ok else "Observability bindings are incomplete.",
        failures=failures,
        observed={
            "metric_keys": sorted(snapshot) if isinstance(snapshot, dict) else [],
            "agent_metric_count": len(snapshot.get("agents", {})) if isinstance(snapshot, dict) else 0,
            "module_state_keys": sorted(module_state),
            "console_json_mode": bool(getattr(api, "json_console", False)) if api is not None else False,
            "console_verbose": bool(getattr(api, "verbose_orchestrator", False)) if api is not None else False,
            "kpi_log_path": str(getattr(kpi_events, "file_path", "")) if kpi_events is not None else "",
            "kpi_summary_path": str(getattr(kpi_events, "summary_path", "")) if kpi_events is not None else "",
        },
    )


_CHECKS = {
    "boot": _boot_check,
    "planning": _planning_check,
    "routing": _routing_check,
    "execution": _execution_check,
    "transport": _transport_check,
    "memory": _memory_check,
    "providers": _providers_check,
    "observability": _observability_check,
}


def _check_exception_result(layer: str, exc: Exception) -> DiagnosticCheckResult:
    return DiagnosticCheckResult(
        layer=layer,
        ok=False,
        summary=f"Diagnostic check for layer '{layer}' crashed.",
        failures=[f"{layer}_check_exception"],
        observed={
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        },
    )


def _run_check(layer: str, api: Any | None = None) -> DiagnosticCheckResult:
    try:
        return _CHECKS[layer](api)
    except Exception as exc:
        return _check_exception_result(layer, exc)


def run_layer_diagnostic_check(layer: str, api: Any | None = None) -> dict[str, Any]:
    name = str(layer).strip().lower()
    if name not in _CHECKS:
        raise ValueError(f"Unknown diagnostic layer: {layer}")
    return _run_check(name, api).as_dict()


def run_diagnostic_checks(api: Any | None = None, *, layers: Iterable[str] | None = None) -> dict[str, Any]:
    selected = _normalize_layers(layers)
    results = [_run_check(layer, api) for layer in selected]
    failures = {
        result.layer: list(result.failures)
        for result in results
        if result.failures
    }
    return {
        "status": "ok" if all(result.ok for result in results) else "degraded",
        "layers": selected,
        "contract_count": len(_CONTRACTS),
        "results": [result.as_dict() for result in results],
        "failures": failures,
        "matrix": build_diagnostic_contract_matrix(),
    }


async def run_diagnostic_matrix(*, layers: Iterable[str] | None = None, api: Any | None = None) -> dict[str, Any]:
    report = run_diagnostic_checks(api, layers=layers)
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "ok": report["status"] == "ok",
        **report,
    }
