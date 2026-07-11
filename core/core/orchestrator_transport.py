from __future__ import annotations

from collections.abc import AsyncIterator
import os
from datetime import UTC, datetime
from typing import Any


def _usage_snapshot(orchestrator: Any) -> dict[str, Any]:
    usage_mod = orchestrator.module_manager.get_module("model_usage") if hasattr(orchestrator, "module_manager") else None
    return usage_mod.finalize() if usage_mod and hasattr(usage_mod, "finalize") else {}


def _suppression_snapshot(orchestrator: Any) -> dict[str, Any]:
    return orchestrator.provider_budget_router.suppression_snapshot() if hasattr(orchestrator, "provider_budget_router") else {}


def _local_llm_module(orchestrator: Any) -> Any | None:
    if hasattr(orchestrator, "module_manager") and hasattr(orchestrator.module_manager, "get_module"):
        return orchestrator.module_manager.get_module("local_llm")
    if hasattr(orchestrator, "get_module"):
        return orchestrator.get_module("local_llm")
    return None


def _socraticode_module(orchestrator: Any) -> Any | None:
    if hasattr(orchestrator, "module_manager") and hasattr(orchestrator.module_manager, "get_module"):
        return orchestrator.module_manager.get_module("socraticode")
    if hasattr(orchestrator, "get_module"):
        return orchestrator.get_module("socraticode")
    return None


def _sourcecraft_module(orchestrator: Any) -> Any | None:
    if hasattr(orchestrator, "get_module"):
        return orchestrator.get_module("sourcecraft")
    if hasattr(orchestrator, "module_manager") and hasattr(orchestrator.module_manager, "get_module"):
        return orchestrator.module_manager.get_module("sourcecraft")
    return None


def _resident_rows(module: Any) -> list[dict[str, Any]]:
    runtime = getattr(module, "runtime", None)
    if runtime is None or not hasattr(runtime, "list_resident_models_sync"):
        return []
    rows: list[dict[str, Any]] = []
    for item in runtime.list_resident_models_sync() or []:
        rows.append(
            {
                "name": str(getattr(item, "name", "") or ""),
                "size": getattr(item, "size", None),
                "size_vram": getattr(item, "size_vram", None),
                "expires_at": getattr(item, "expires_at", None),
                "digest": getattr(item, "digest", None),
                "details": getattr(item, "details", {}) or {},
            }
        )
    return rows


def provider_inventory_payload(orchestrator: Any, *, force_refresh: bool = False) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.build_all_provider_endpoint_inventories(
        force_refresh=force_refresh,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": payload}, 200


def provider_inventory_single_payload(orchestrator: Any, provider: str, *, force_refresh: bool = False) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.build_provider_endpoint_inventory(
        provider,
        force_refresh=force_refresh,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": payload}, 200


def provider_runtime_inventory_all_payload(
    orchestrator: Any,
    *,
    force_refresh: bool = False,
    probe_limit: int | None = None,
) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.build_all_provider_runtime_inventories(
        force_refresh=force_refresh,
        probe_limit=probe_limit,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": payload}, 200


def provider_runtime_inventory_single_payload(
    orchestrator: Any,
    provider: str,
    *,
    force_refresh: bool = False,
    probe_limit: int | None = None,
) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.build_provider_runtime_inventory(
        provider,
        force_refresh=force_refresh,
        probe_limit=probe_limit,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": payload}, 200


async def provider_runtime_inventory_single_stream(
    orchestrator: Any,
    provider: str,
    *,
    force_refresh: bool = False,
    probe_limit: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    hub = getattr(orchestrator, "inventory_stream_hub", None)
    if hub is not None and hasattr(hub, "stream"):
        async for event in hub.stream("provider_runtime_inventory"):
            snapshot = event.get("snapshot", {}) if isinstance(event, dict) else {}
            providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), dict) else {}
            row = providers.get(provider, {})
            yield {
                "type": "snapshot" if str(event.get("kind") or "") == "snapshot" else "delta",
                "provider": provider,
                "data": row if isinstance(row, dict) else {},
                "summary": snapshot.get("summary", {}) if isinstance(snapshot, dict) else {},
                "published_at": event.get("published_at"),
                "version": event.get("version"),
            }
        return
    payload, _status = provider_runtime_inventory_single_payload(
        orchestrator,
        provider,
        force_refresh=force_refresh,
        probe_limit=probe_limit,
    )
    yield {"type": "snapshot", "provider": provider, "data": payload.get("data", {})}


def _socraticode_context_compaction_snapshot(orchestrator: Any) -> dict[str, Any]:
    module = _socraticode_module(orchestrator)
    module_state = module.finalize() if module and hasattr(module, "finalize") else {}
    last_annotation = module_state.get("last_annotation") if isinstance(module_state.get("last_annotation"), dict) else {}
    latest_frame = getattr(orchestrator, "_latest_frame_orchestrator", None)
    latest_socraticode = latest_frame.get("socraticode") if isinstance(latest_frame, dict) and isinstance(latest_frame.get("socraticode"), dict) else {}
    latest_compaction = latest_frame.get("socraticode_context_compaction") if isinstance(latest_frame, dict) and isinstance(latest_frame.get("socraticode_context_compaction"), dict) else {}

    coverage = latest_socraticode if latest_socraticode else last_annotation.get("context_coverage") if isinstance(last_annotation.get("context_coverage"), dict) else {}
    cost = latest_socraticode if latest_socraticode else last_annotation.get("cost_downgrade") if isinstance(last_annotation.get("cost_downgrade"), dict) else {}
    parallel = latest_socraticode if latest_socraticode else last_annotation.get("parallelism") if isinstance(last_annotation.get("parallelism"), dict) else {}

    try:
        coverage_score = float(coverage.get("coverage_score", coverage.get("score", coverage.get("coverage_ratio", coverage.get("ratio", 0.0)))) or 0.0)
    except (TypeError, ValueError):
        coverage_score = 0.0
    coverage_status = str(coverage.get("coverage_status", coverage.get("status", "low")) or "low").strip() or "low"
    missing_files = [str(item).strip() for item in list(coverage.get("missing_files") or []) if str(item).strip()]

    if latest_compaction:
        status = str(latest_compaction.get("status") or "disabled").strip() or "disabled"
        mode = str(latest_compaction.get("compaction_mode") or "raw_prompt").strip() or "raw_prompt"
        source = str(latest_compaction.get("prompt_context_source") or "task_description").strip() or "task_description"
        raw_allowed = bool(latest_compaction.get("raw_file_dump_allowed", True))
        reduction = str(latest_compaction.get("token_reduction_expected") or "low").strip() or "low"
        strategy = str(latest_compaction.get("recommended_prompt_strategy") or "use standard file-bounded prompting until SocratiCode context is available").strip()
        annotation_status = str(latest_socraticode.get("status") or "unavailable").strip() or "unavailable"
        target_cost_tier = "economy" if bool(latest_socraticode.get("prefer_low_cost_lanes")) else None
        recommended_parallel = latest_socraticode.get("recommended_parallel_branches")
    else:
        if str(last_annotation.get("status") or "").strip().lower() != "applied":
            status = "disabled"
            mode = "raw_prompt"
            source = "task_description"
            raw_allowed = True
            reduction = "low"
            strategy = "use standard file-bounded prompting until SocratiCode context is available"
        elif coverage_score >= 0.88 and not missing_files:
            status = "active"
            mode = "compact_context_first"
            source = "socraticode_compact_context"
            raw_allowed = False
            reduction = "high"
            strategy = "use compact context and impact summaries before any raw file expansion"
        elif coverage_score >= 0.72:
            status = "active"
            mode = "hybrid_context"
            source = "socraticode_compact_context_plus_file_refs"
            raw_allowed = False
            reduction = "medium"
            strategy = "prefer compact context and expand only unresolved files or symbols"
        else:
            status = "fallback"
            mode = "raw_prompt"
            source = "task_description_plus_file_refs"
            raw_allowed = True
            reduction = "low"
            strategy = "do not shrink prompt context aggressively because SocratiCode coverage is partial"
        annotation_status = last_annotation.get("status") or "unavailable"
        target_cost_tier = str((last_annotation.get("cost_downgrade") or {}).get("target_cost_tier") or "").strip() or None if isinstance(last_annotation.get("cost_downgrade"), dict) else None
        recommended_parallel = (last_annotation.get("parallelism") or {}).get("recommended_parallel_branches") if isinstance(last_annotation.get("parallelism"), dict) else None

    feature_enabled = str(os.getenv("SOCRATICODE_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"} or bool(str(os.getenv("SOCRATICODE_MCP_COMMAND") or "").strip())
    return {
        "feature_enabled": feature_enabled,
        "bridge_available": bool(module_state.get("bridge_available")) or bool(latest_socraticode),
        "bridge_source": module_state.get("bridge_source") or ("task_submission" if latest_socraticode else None),
        "module_status": module_state.get("status") or "unknown",
        "context_compaction": {
            "status": status,
            "compaction_mode": mode,
            "prompt_context_source": source,
            "raw_file_dump_allowed": raw_allowed,
            "token_reduction_expected": reduction,
            "recommended_prompt_strategy": strategy,
        },
        "last_annotation_status": annotation_status,
        "coverage_score": round(coverage_score, 4),
        "coverage_status": coverage_status,
        "missing_files": missing_files,
        "prefer_low_cost_lanes": bool(latest_socraticode.get("prefer_low_cost_lanes")) if latest_socraticode else bool((last_annotation.get("cost_downgrade") or {}).get("eligible")) if isinstance(last_annotation.get("cost_downgrade"), dict) else False,
        "target_cost_tier": target_cost_tier,
        "recommended_parallel_branches": recommended_parallel,
        "last_error": module_state.get("last_error"),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def socraticode_context_compaction_status_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    return {"status": "ok", "data": _socraticode_context_compaction_snapshot(orchestrator)}, 200


async def socraticode_context_compaction_status_stream(orchestrator: Any) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "snapshot", "status": "ok", "data": _socraticode_context_compaction_snapshot(orchestrator)}
    runtime_hub = getattr(orchestrator, "runtime_event_stream_hub", None)
    if runtime_hub is not None and hasattr(runtime_hub, "stream"):
        async for event in runtime_hub.stream():
            yield {
                "type": "delta",
                "status": "ok",
                "data": _socraticode_context_compaction_snapshot(orchestrator),
                "runtime_event": event.get("delta") if isinstance(event, dict) else None,
                "published_at": event.get("published_at") if isinstance(event, dict) else None,
                "version": event.get("version") if isinstance(event, dict) else None,
            }


def provider_models_index_payload(orchestrator: Any, *, force_refresh: bool = False) -> tuple[dict[str, Any], int]:
    if force_refresh:
        if hasattr(orchestrator, "_refresh_hot_provider_inventory_snapshot"):
            orchestrator._refresh_hot_provider_inventory_snapshot(force_refresh=True)
        else:
            orchestrator.provider_inventory.refresh(force_refresh=True)
    return {"status": "ok", "data": orchestrator.provider_inventory.model_index_summary()}, 200


def provider_model_lookup_payload(
    orchestrator: Any,
    model_name: str,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], int]:
    if force_refresh:
        if hasattr(orchestrator, "_refresh_hot_provider_inventory_snapshot"):
            orchestrator._refresh_hot_provider_inventory_snapshot(force_refresh=True)
        else:
            orchestrator.provider_inventory.refresh(force_refresh=True)
    row = orchestrator.provider_inventory.find_model(model_name)
    if not row:
        return {"status": "error", "error": "model_not_found", "model_name": model_name}, 404
    return {"status": "ok", "data": row}, 200


async def provider_models_index_stream(orchestrator: Any) -> AsyncIterator[dict[str, Any]]:
    hub = getattr(orchestrator, "inventory_stream_hub", None)
    if hub is not None and hasattr(hub, "stream"):
        async for event in hub.stream():
            yield {"status": "ok", "data": event.get("model_index", {})}
        return
    yield {"status": "ok", "data": orchestrator.provider_inventory.model_index_summary()}


async def provider_inventory_stream(orchestrator: Any) -> AsyncIterator[dict[str, Any]]:
    hub = getattr(orchestrator, "inventory_stream_hub", None)
    if hub is not None and hasattr(hub, "stream"):
        async for _event in hub.stream():
            payload, _status = provider_inventory_payload(orchestrator, force_refresh=False)
            yield {"status": "ok", "data": payload.get("data", {})}
        return
    payload, _status = provider_inventory_payload(orchestrator, force_refresh=False)
    yield {"status": "ok", "data": payload.get("data", {})}


async def provider_runtime_inventory_stream(orchestrator: Any) -> AsyncIterator[dict[str, Any]]:
    hub = getattr(orchestrator, "inventory_stream_hub", None)
    if hub is not None and hasattr(hub, "stream"):
        async for event in hub.stream():
            snapshot = event.get("snapshot", {}) if isinstance(event, dict) else {}
            yield {"status": "ok", "data": snapshot.get("runtime_inventory", {})}
        return
    payload, _status = provider_runtime_inventory_all_payload(orchestrator, force_refresh=False)
    yield {"status": "ok", "data": payload.get("data", {})}


def local_llm_residents_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    module = _local_llm_module(orchestrator)
    if not module:
        return {"status": "error", "error": "local_llm module not loaded"}, 503
    try:
        resident_models = _resident_rows(module)
    except Exception as exc:
        return {
            "status": "error",
            "error": "local_llm residents unavailable",
            "details": {"reason": str(exc)},
            "data": {"resident_models": []},
        }, 503
    return {"status": "ok", "data": {"resident_models": resident_models}}, 200


def stats_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    usage_mod = orchestrator.module_manager.get_module("model_usage") if hasattr(orchestrator, "module_manager") else None
    local_model_manager = orchestrator.module_manager.get_module("local_model_manager") if hasattr(orchestrator, "module_manager") else None
    module_state = orchestrator.module_state() if hasattr(orchestrator, "module_state") else {}
    return {
        "status": "success",
        "data": {
            "model_usage": usage_mod.finalize() if usage_mod and hasattr(usage_mod, "finalize") else {},
            "local_model_manager": local_model_manager.finalize() if local_model_manager and hasattr(local_model_manager, "finalize") else {},
            "provider_inventory": module_state.get("provider_inventory", {}) if isinstance(module_state, dict) else {},
        },
    }, 200


def openai_runtime_inventory_payload(
    orchestrator: Any,
    *,
    force_refresh: bool = False,
    probe_limit: int | None = None,
) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.refresh_openai_runtime_inventory(
        force_refresh=force_refresh,
        probe_limit=probe_limit,
    )
    return {"status": "ok", "data": payload}, 200


def openai_discovery_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    from core.core.openai_bazzite_endpoint import load_openai_endpoint_discovery

    return {"status": "ok", "data": load_openai_endpoint_discovery()}, 200


def openai_model_templates_payload(
    orchestrator: Any,
    *,
    force_refresh: bool = False,
    probe_limit: int | None = None,
) -> tuple[dict[str, Any], int]:
    payload = orchestrator.provider_inventory.refresh_openai_runtime_inventory(
        force_refresh=force_refresh,
        probe_limit=probe_limit,
    )
    return {"status": "ok", "data": payload.get("model_templates", {})}, 200


def local_model_health_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    local_model_manager = orchestrator.module_manager.get_module("local_model_manager") if hasattr(orchestrator, "module_manager") else None
    if local_model_manager and hasattr(local_model_manager, "finalize"):
        state = local_model_manager.finalize()
        return {
            "status": "ok",
            "resident_models": state.get("resident_models", []),
            "blocked_models": state.get("blocked_models", []),
            "memory_pressure": state.get("memory_pressure", {}),
            "evictions": state.get("evictions", 0),
            "warmups": state.get("warmups", 0),
        }, 200
    return {"status": "error", "error": "local_model_manager not loaded"}, 503


def antigravity_status_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    from core.core.antigravity_status_module import shared_antigravity_snapshot

    return shared_antigravity_snapshot(force=False), 200


def dump_memory_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    modules = orchestrator.module_manager.loaded_modules() if hasattr(orchestrator, "module_manager") and hasattr(orchestrator.module_manager, "loaded_modules") else []
    return {"status": "ok", "modules": modules}, 200


def local_llm_connect_payload(orchestrator: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    module = _local_llm_module(orchestrator)
    if not module:
        return {"status": "error", "error": "local_llm module not loaded"}, 503
    model_name = str(payload.get("model_name") or getattr(module, "model_name", "") or "").strip()
    ok = bool(getattr(module, "hot_reload")(model_name)) if model_name and hasattr(module, "hot_reload") else False
    if not ok:
        return {"status": "error", "error": "local_llm_connect_failed", "model_name": model_name}, 503
    inventory = orchestrator.provider_inventory.build_provider_runtime_inventory(
        "local_llm",
        force_refresh=True,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": {"connected": True, "model_name": model_name, "inventory": inventory}}, 200


def local_llm_disconnect_payload(orchestrator: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    module = _local_llm_module(orchestrator)
    if not module:
        return {"status": "error", "error": "local_llm module not loaded"}, 503
    model_name = str(payload.get("model_name") or getattr(module, "model_name", "") or "").strip() or None
    ok = bool(getattr(module, "unload_model")(model_name)) if hasattr(module, "unload_model") else False
    if not ok:
        return {"status": "error", "error": "local_llm_disconnect_failed", "model_name": model_name}, 503
    inventory = orchestrator.provider_inventory.build_provider_runtime_inventory(
        "local_llm",
        force_refresh=True,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {"status": "ok", "data": {"disconnected": True, "model_name": model_name, "inventory": inventory}}, 200


def local_llm_warm_payload(orchestrator: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    module = _local_llm_module(orchestrator)
    if not module or not hasattr(module, "runtime"):
        return {"status": "error", "error": "local_llm runtime not loaded"}, 503
    runtime = module.runtime
    model_name = str(payload.get("model_name") or getattr(module, "model_name", "") or "").strip() or None
    keep_alive = payload.get("keep_alive")
    timeout_sec = payload.get("timeout_sec")
    result = runtime.warm_model_sync(model_name, keep_alive=keep_alive, timeout_sec=timeout_sec) if hasattr(runtime, "warm_model_sync") else None
    inventory = orchestrator.provider_inventory.build_provider_runtime_inventory(
        "local_llm",
        force_refresh=True,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    return {
        "status": "ok",
        "data": {
            "warmed": True,
            "model_name": model_name,
            "result": result.as_dict() if result and hasattr(result, "as_dict") else {},
            "inventory": inventory,
        },
    }, 200


def ai_kernel_gate_payload(orchestrator: Any, *, ensure_ready: bool = False, model_name: str | None = None) -> tuple[dict[str, Any], int]:
    bridge = getattr(orchestrator, "ai_kernel_bridge", None)
    inventory_service = getattr(orchestrator, "provider_inventory", None)
    if bridge is None or inventory_service is None:
        return {"status": "error", "error": "ai_kernel bridge not loaded"}, 503
    gate = bridge.gate(model_name=model_name, ensure_ready=ensure_ready)
    inventory = inventory_service.build_provider_runtime_inventory(
        "ai_kernel",
        force_refresh=True,
        usage_snapshot=_usage_snapshot(orchestrator),
        suppression_snapshot=_suppression_snapshot(orchestrator),
    )
    gate["inventory"] = inventory
    gate["orchestrator_usable_models"] = [
        str(row.get("model_name") or "").strip()
        for row in (inventory.get("models") or [])
        if str(row.get("model_name") or "").strip() and bool(row.get("kernel_eligible"))
    ]
    return {"status": "ok", "data": gate}, 200


def ai_kernel_ensure_payload(orchestrator: Any, payload: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    data = payload if isinstance(payload, dict) else {}
    model_name = str(data.get("model_name") or "").strip() or None
    return ai_kernel_gate_payload(orchestrator, ensure_ready=True, model_name=model_name)


def transport_audit_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    if hasattr(orchestrator, "build_transport_audit"):
        payload = orchestrator.build_transport_audit()
    else:
        from .transport_audit import build_transport_audit

        payload = build_transport_audit(orchestrator)
    return {"status": "ok", "data": payload}, 200


async def diagnostics_payload(
    orchestrator: Any,
    *,
    layers: list[str] | None = None,
    matrix_only: bool = False,
) -> tuple[dict[str, Any], int]:
    diag_module = orchestrator.get_module("self_diagnostic") if hasattr(orchestrator, "get_module") else None
    if not diag_module:
        return {
            "status": "error",
            "error": "self_diagnostic module not found",
            "failure_code": "SELF_DIAGNOSTIC_MODULE_UNAVAILABLE",
        }, 503
    payload = await diag_module.run_diagnostics(layers=layers or None, matrix_only=matrix_only)
    if not isinstance(payload, dict):
        return {
            "status": "error",
            "error": "diagnostics payload is not a JSON object",
            "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
        }, 500
    required = {"schema_version", "layers", "matrix"} if matrix_only else {"schema_version"}
    missing = sorted(field for field in required if field not in payload)
    if missing:
        return {
            "status": "error",
            "error": f"diagnostics payload missing required fields: {', '.join(missing)}",
            "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
        }, 500
    return payload, 200


async def diagnostics_stream(
    orchestrator: Any,
    *,
    layers: list[str] | None = None,
    matrix_only: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "event",
        "stage": "started",
        "layers": list(layers or []),
        "matrix_only": bool(matrix_only),
        "started_at": datetime.now(UTC).isoformat(),
    }
    payload, status_code = await diagnostics_payload(orchestrator, layers=layers, matrix_only=matrix_only)
    yield {
        "type": "event",
        "stage": "completed" if status_code == 200 else "failed",
        "status_code": status_code,
        "payload": payload,
        "final": True,
    }


def sourcecraft_status_payload(orchestrator: Any) -> tuple[dict[str, Any], int]:
    module = _sourcecraft_module(orchestrator)
    if not module:
        return {"status": "error", "error": "sourcecraft module not loaded"}, 503
    runtime_repo_path = module._default_repo_path() if hasattr(module, "_default_repo_path") else "."
    runtime = module.ensure_ready(repo_path=runtime_repo_path) if hasattr(module, "ensure_ready") else {"status": "unknown"}
    profile = module._role_profile().as_dict() if hasattr(module, "_role_profile") else {}
    final = module.finalize() if hasattr(module, "finalize") else {}
    return {
        "status": final.get("status", runtime.get("status", "unknown")),
        "runtime": runtime,
        "role": profile,
        "module": final,
    }, 200


def sourcecraft_delegate_payload(orchestrator: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    from core.core.task_submission_api import create_standard_task

    task = create_standard_task(payload)
    module = _sourcecraft_module(orchestrator)
    if not module:
        return {"status": "error", "error": "sourcecraft module not loaded"}, 503
    delegation = module.build_delegation_profile(task, payload) if hasattr(module, "build_delegation_profile") else {}
    acceptance = orchestrator.router.route(task)
    route = acceptance.as_dict() if hasattr(acceptance, "as_dict") else acceptance
    assigned_agent = route.get("assigned_agent") if isinstance(route, dict) else None
    route_mode = "orchestrator" if assigned_agent == "orchestrator" else "p2p"
    schedule = {
        "task_id": task.task_id,
        "route_mode": route_mode,
        "assigned_agent": assigned_agent,
        "requires_orchestrator": route_mode == "orchestrator",
        "reason": route.get("message") if isinstance(route, dict) else None,
    }
    return {
        "status": "ok",
        "delegation": delegation,
        "route": route,
        "schedule": schedule,
    }, 200


async def sourcecraft_delegate_stream(orchestrator: Any, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "event", "stage": "accepted", "payload_keys": sorted(payload)}
    result, status_code = sourcecraft_delegate_payload(orchestrator, payload)
    if status_code != 200:
        yield {"type": "event", "stage": "failed", "status_code": status_code, "payload": result, "final": True}
        return
    yield {"type": "event", "stage": "delegation_ready", "delegation": result.get("delegation", {})}
    yield {
        "type": "event",
        "stage": "route_ready",
        "route": result.get("route", {}),
        "schedule": result.get("schedule", {}),
        "final": True,
    }


def sourcecraft_parallel_delegate_payload(orchestrator: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    from core.core.frame_orchestrator import build_frame_orchestrator_package
    from core.core.task_submission_api import create_standard_task

    task = create_standard_task(payload)
    module = _sourcecraft_module(orchestrator)
    if not module:
        return {"status": "error", "error": "sourcecraft module not loaded"}, 503
    if not hasattr(module, "build_parallel_coding_brief"):
        return {"status": "error", "error": "sourcecraft parallel delegation unavailable"}, 503

    brief = module.build_parallel_coding_brief(task, payload)
    if not isinstance(task.routing_hints, dict):
        task.routing_hints = {}
    task.routing_hints.update(brief.get("orchestrator_payload") or {})
    task.routing_hints["source"] = "sourcecraft_parallel_delegate"
    task.routing_hints["sourcecraft_instruction"] = brief.get("orchestrator_instruction")
    task.routing_hints["frame_orchestrator"] = build_frame_orchestrator_package(task, payload).as_dict()

    plan = orchestrator.create_execution_plan(task)
    atomic_tasks = getattr(plan, "atomic_tasks", []) or []
    atomic_summary = [
        {
            "task_id": atomic.task_id,
            "type": str(getattr(getattr(atomic, "type", None), "value", getattr(atomic, "type", ""))),
            "draft_layer": atomic.draft_layer,
            "required_capability": atomic.required_capability,
            "preferred_agent_id": atomic.routing_hints.get("preferred_agent_id") if isinstance(atomic.routing_hints, dict) else None,
            "fanout_label": atomic.routing_hints.get("fanout_label") if isinstance(atomic.routing_hints, dict) else None,
            "dependencies": list(atomic.dependencies),
            "files": list(atomic.input.files),
        }
        for atomic in atomic_tasks
    ]
    return {
        "status": "ok",
        "brief": brief,
        "task": task.as_dict(),
        "plan": plan.as_dict() if hasattr(plan, "as_dict") else {},
        "atomic_task_summary": atomic_summary,
    }, 200


async def sourcecraft_parallel_delegate_stream(orchestrator: Any, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "event", "stage": "accepted", "payload_keys": sorted(payload)}
    result, status_code = sourcecraft_parallel_delegate_payload(orchestrator, payload)
    if status_code != 200:
        yield {"type": "event", "stage": "failed", "status_code": status_code, "payload": result, "final": True}
        return
    yield {"type": "event", "stage": "brief_ready", "brief": result.get("brief", {})}
    yield {
        "type": "event",
        "stage": "plan_ready",
        "plan": result.get("plan", {}),
        "atomic_task_summary": result.get("atomic_task_summary", []),
        "final": True,
    }


async def runtime_events_stream(orchestrator: Any) -> AsyncIterator[dict[str, Any]]:
    hub = getattr(orchestrator, "runtime_event_stream_hub", None)
    if hub is None:
        yield {"status": "error", "error": "runtime_event_stream_unavailable"}
        return
    if hasattr(hub, "stream"):
        async for event in hub.stream():
            yield {"status": "ok", "data": event.get("data", {})}
        return
    if hasattr(hub, "current_event"):
        event = hub.current_event()
        yield {"status": "ok", "data": event.get("data", {})}
        return
    yield {"status": "error", "error": "runtime_event_stream_unavailable"}


def health_payload() -> tuple[dict[str, Any], int]:
    return {"status": "ok", "service": "orchestrator", "ts": datetime.now(UTC).isoformat()}, 200
