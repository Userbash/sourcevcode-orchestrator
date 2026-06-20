from __future__ import annotations

import importlib
import inspect
import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from .kernel_api import KernelAPI
from .models import Task

logger = logging.getLogger("self_diagnostic")

_DEFAULT_SCHEMA_VERSION = "legacy-self-diagnostic/v1"
_LAYER_ALIASES = {
    "component": "components",
    "components": "components",
    "module": "components",
    "modules": "components",
    "memory": "memory",
    "mem": "memory",
    "ai": "ai_models",
    "ai_model": "ai_models",
    "ai_models": "ai_models",
    "model": "ai_models",
    "models": "ai_models",
    "provider": "providers",
    "providers": "providers",
    "matrix": "matrix",
    "layer": "matrix",
    "layers": "matrix",
}
_SUCCESS_STATUSES = {"healthy", "ok", "pass", "passed", "ready", "success"}


class SelfDiagnosticModule:
    """
    TDD-implemented module for automatic system-wide diagnostics.
    Verifies modules, memory, AI provider health, and unified layer diagnostics.
    """

    name: str = "self_diagnostic"

    def __init__(self):
        self._api: Optional[KernelAPI] = None
        self._is_active: bool = False

    async def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._is_active = True
        self._api.log("info", f"[DIAG] {self.name} module initialized.")

    async def on_unload(self) -> None:
        self._is_active = False

    def before_task(self, task: Task, context: Dict[str, Any]) -> None:
        """Hook: No-op for diagnostic module."""
        pass

    def after_task(self, task: Any, result: Any, context: Dict[str, Any]) -> None:
        """Hook: No-op for diagnostic module."""
        pass

    async def run_layer_diagnostics(self, layers: list[str] | None = None) -> Dict[str, Any]:
        selected_layers = self._normalize_layers(layers)
        report = self._build_report_skeleton(selected_layers)
        self._populate_legacy_report(report, selected_layers)
        contracts_module = self._load_diagnostic_contracts()
        return await self._build_layer_diagnostic_payload(report, selected_layers, contracts_module)

    async def run_diagnostics(
        self,
        layers: list[str] | tuple[str, ...] | set[str] | None = None,
        include_layer_matrix: bool = True,
        matrix_only: bool = False,
    ) -> Dict[str, Any]:
        selected_layers = self._normalize_layers(layers)
        contracts_module = self._load_diagnostic_contracts()
        if matrix_only:
            return self._build_matrix_only_payload(selected_layers, contracts_module)

        report = self._build_report_skeleton(selected_layers, contracts_module=contracts_module)
        self._populate_legacy_report(report, selected_layers)

        if include_layer_matrix:
            layer_payload = await self._build_layer_diagnostic_payload(report, selected_layers, contracts_module)
            report["layer_checks"] = layer_payload["checks"]
            report["diagnostic_matrix"] = layer_payload["diagnostic_matrix"]
            report["layer_check_status"] = {
                "ok": layer_payload["ok"],
                "status": layer_payload["status"],
                "source": layer_payload["source"],
            }
            if not layer_payload["ok"]:
                self._degrade(report)

        report["remediation_plan"] = self._build_remediation_plan(report)
        report["readiness"] = self._build_readiness_summary(report)
        return report

    def _build_matrix_only_payload(self, selected_layers: set[str] | None, contracts_module: Any | None = None) -> Dict[str, Any]:
        schema_version = getattr(contracts_module, "DIAGNOSTIC_SCHEMA_VERSION", _DEFAULT_SCHEMA_VERSION)
        layers = sorted(selected_layers) if selected_layers else []
        available = []
        if contracts_module is not None:
            available_fn = getattr(contracts_module, "available_layers", None)
            if callable(available_fn):
                try:
                    raw = available_fn()
                    if isinstance(raw, (list, tuple, set)):
                        available = [str(item).strip() for item in raw if str(item).strip()]
                except Exception:
                    available = []
        if not available:
            available = list(layers or ["components", "memory", "ai_models", "matrix"])

        matrix_payload: Dict[str, Any] = {"source": "legacy", "layers": {}, "selected_layers": layers or available}
        if contracts_module is not None:
            matrix_fn = getattr(contracts_module, "diagnostic_matrix", None)
            if callable(matrix_fn):
                try:
                    matrix_payload = matrix_fn()
                except Exception as exc:
                    matrix_payload = {"source": "diagnostic_contracts", "status": "error", "error": str(exc), "layers": {}}
        matrix_layers = matrix_payload.get("layers") if isinstance(matrix_payload, dict) else None
        if isinstance(matrix_layers, dict) and selected_layers:
            matrix_payload = dict(matrix_payload)
            matrix_payload["layers"] = {key: value for key, value in matrix_layers.items() if self._normalize_layer_name(key) in selected_layers}
            order = matrix_payload.get("order")
            if isinstance(order, list):
                matrix_payload["order"] = [item for item in order if self._normalize_layer_name(item) in selected_layers]
        return {
            "schema_version": schema_version,
            "layers": layers or available,
            "matrix": matrix_payload,
        }

    def _build_report_skeleton(self, selected_layers: set[str] | None, contracts_module: Any | None = None) -> Dict[str, Any]:
        requested_layers = sorted(selected_layers) if selected_layers else ["components", "memory", "ai_models", "matrix"]
        return {
            "schema_version": getattr(contracts_module, "DIAGNOSTIC_SCHEMA_VERSION", _DEFAULT_SCHEMA_VERSION),
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat(),
            "components": {},
            "memory": {},
            "ai_models": {},
            "antigravity_status": {},
            "requested_layers": requested_layers,
            "layer_checks": [],
            "diagnostic_matrix": {
                "source": "legacy",
                "selected_layers": requested_layers,
                "layers": {},
                "check_count": 0,
            },
            "remediation_plan": [],
            "readiness": {
                "core_ready": True,
                "provider_ready": True,
                "startup_ready": True,
                "blocking_issues": [],
            },
        }

    def _normalize_layers(self, layers: list[str] | tuple[str, ...] | set[str] | None) -> set[str] | None:
        if not layers:
            return None
        normalized: set[str] = set()
        for item in layers:
            for chunk in str(item).split(","):
                name = chunk.strip().lower()
                if not name:
                    continue
                normalized.add(_LAYER_ALIASES.get(name, name))
        return normalized or None

    @staticmethod
    def _layer_requested(selected_layers: set[str] | None, layer: str) -> bool:
        return selected_layers is None or layer in selected_layers

    @staticmethod
    def _degrade(report: Dict[str, Any]) -> None:
        if report.get("status") == "healthy":
            report["status"] = "degraded"

    def _module_state(self) -> Dict[str, Any]:
        if not self._api or not hasattr(self._api, "module_state"):
            return {}
        try:
            state = self._api.module_state()
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _cached_provider_reports(self) -> Dict[str, Any]:
        state = self._module_state()
        availability_state = state.get("model_availability", {}) if isinstance(state, dict) else {}
        if not isinstance(availability_state, dict):
            return {}
        providers = availability_state.get("cached_report")
        if isinstance(providers, dict):
            return {str(k): (dict(v) if isinstance(v, dict) else {"status": "unknown", "details": v}) for k, v in providers.items()}
        providers = availability_state.get("providers")
        if isinstance(providers, dict):
            return {str(k): (dict(v) if isinstance(v, dict) else {"status": "unknown", "details": v}) for k, v in providers.items()}
        return {}

    def _local_model_health(self) -> Dict[str, Any]:
        state = self._module_state()
        local_state = state.get("local_model_manager", {}) if isinstance(state, dict) else {}
        if not isinstance(local_state, dict) or not local_state:
            return {}
        memory_pressure = local_state.get("memory_pressure", {}) if isinstance(local_state.get("memory_pressure"), dict) else {}
        blocked_models = local_state.get("blocked_models", []) if isinstance(local_state.get("blocked_models"), list) else []
        pressure_state = str(memory_pressure.get("pressure_state") or "normal").strip().lower()
        status = "healthy"
        if blocked_models or pressure_state == "high":
            status = "degraded"
        elif pressure_state == "elevated":
            status = "degraded"
        return {
            "provider": "local",
            "status": status,
            "source": "local_model_manager",
            "blocked_models": blocked_models,
            "resident_models": local_state.get("resident_models", []),
            "memory_pressure": memory_pressure,
        }

    def _populate_legacy_report(self, report: Dict[str, Any], selected_layers: set[str] | None) -> None:
        if self._layer_requested(selected_layers, "components"):
            self._populate_component_report(report)
        if self._layer_requested(selected_layers, "memory"):
            self._populate_memory_report(report)
        if self._layer_requested(selected_layers, "ai_models"):
            self._populate_ai_model_report(report)
        report["layer_checks"] = self._build_legacy_layer_checks(report)
        self._refresh_matrix_from_checks(report, source="legacy")

    def _populate_component_report(self, report: Dict[str, Any]) -> None:
        if not self._api:
            return
        try:
            module_state = self._api.module_state() if hasattr(self._api, "module_state") else {}
        except Exception as exc:
            report["components"]["module_state"] = {"status": "error", "error": str(exc)}
            self._degrade(report)
            return
        module_manager = self._api.get_context("module_manager") if hasattr(self._api, "get_context") else None
        if not module_manager:
            return
        for mod_name in module_manager.loaded_modules():
            if mod_name == self.name:
                continue
            try:
                mod = module_manager.get_module(mod_name)
                mod_report = module_state.get(mod_name, {}) if isinstance(module_state, dict) else {}
                report["components"][mod_name] = {
                    "status": "ok",
                    "details": mod_report,
                }
            except Exception as exc:
                report["components"][mod_name] = {
                    "status": "error",
                    "error": str(exc),
                }
                self._degrade(report)

    def _populate_memory_report(self, report: Dict[str, Any]) -> None:
        if not self._api:
            return
        memory = self._api.get_memory()
        if not memory:
            report["memory"] = {"status": "missing"}
            self._degrade(report)
            return
        try:
            backend_type = type(memory.backend).__name__ if hasattr(memory, "backend") else "unknown"
            report["memory"] = {
                "status": "ok",
                "backend": backend_type,
                "session_count": len(memory._sessions) if hasattr(memory, "_sessions") else 0,
            }
        except Exception as exc:
            report["memory"] = {"status": "error", "error": str(exc)}
            self._degrade(report)

    def _populate_ai_model_report(self, report: Dict[str, Any]) -> None:
        try:
            state = self._module_state()
            ai_models = self._cached_provider_reports()
            local_health = self._local_model_health()
            if local_health:
                ai_models["local"] = local_health
            report["ai_models"] = ai_models
            antigravity_status = state.get("antigravity_status", {}) if isinstance(state, dict) else {}
            report["antigravity_status"] = antigravity_status if isinstance(antigravity_status, dict) else {}
            if any(
                str(health.get("status") or "").strip().lower() not in {"healthy", "ready"}
                for health in report["ai_models"].values()
                if isinstance(health, dict) and "status" in health
            ):
                self._degrade(report)
        except Exception as exc:
            report["ai_models"] = {"status": "error", "error": str(exc)}
            self._degrade(report)

    def _build_legacy_layer_checks(self, report: Dict[str, Any]) -> list[Dict[str, Any]]:
        checks: list[Dict[str, Any]] = []
        for name, component in report.get("components", {}).items():
            if not isinstance(component, dict):
                continue
            status = str(component.get("status") or "unknown")
            checks.append(
                {
                    "name": name,
                    "layer": "components",
                    "status": status,
                    "ok": status in _SUCCESS_STATUSES,
                    "details": component.get("details"),
                    "error": component.get("error"),
                    "source": "legacy",
                }
            )
        memory = report.get("memory")
        if isinstance(memory, dict) and memory:
            status = str(memory.get("status") or "unknown")
            checks.append(
                {
                    "name": "memory",
                    "layer": "memory",
                    "status": status,
                    "ok": status in _SUCCESS_STATUSES,
                    "details": {k: v for k, v in memory.items() if k not in {"status", "error"}},
                    "error": memory.get("error"),
                    "source": "legacy",
                }
            )
        for name, model in report.get("ai_models", {}).items():
            if not isinstance(model, dict):
                continue
            status = str(model.get("status") or "unknown")
            checks.append(
                {
                    "name": name,
                    "layer": "ai_models",
                    "status": status,
                    "ok": status in _SUCCESS_STATUSES,
                    "details": {k: v for k, v in model.items() if k not in {"status", "error"}},
                    "error": model.get("error"),
                    "source": "legacy",
                }
            )
        return checks

    async def _build_layer_diagnostic_payload(
        self,
        report: Dict[str, Any],
        selected_layers: set[str] | None,
        contracts_module: Any | None,
    ) -> Dict[str, Any]:
        legacy_checks = self._filter_checks(report.get("layer_checks", []), selected_layers)
        if contracts_module is not None:
            context = {
                "api": self._api,
                "module": self,
                "diagnostic_module": self,
                "availability": getattr(self._api, "availability", None),
                "cached_only": True,
                "report": report,
                "baseline_report": report,
                "selected_layers": sorted(selected_layers) if selected_layers else None,
                "layers": sorted(selected_layers) if selected_layers else None,
                "exclude_modules": {self.name},
                "skip_modules": {self.name},
                "skip_self_diagnostic": True,
                "self_name": self.name,
            }
            try:
                payload = await self._run_contract_payload(contracts_module, context)
            except Exception as exc:
                matrix = self._derive_matrix(legacy_checks, selected_layers, source="diagnostic_contracts")
                matrix["error"] = str(exc)
                return {
                    "ok": False,
                    "status": "degraded",
                    "checks": legacy_checks,
                    "diagnostic_matrix": matrix,
                    "source": "diagnostic_contracts",
                }
            if payload is not None:
                checks = self._extract_contract_checks(payload) or legacy_checks
                checks = self._filter_checks(checks, selected_layers)
                status = self._extract_contract_status(payload)
                ok_flag = self._extract_contract_ok(payload)
                matrix = self._extract_contract_matrix(payload, checks, selected_layers)
                matrix = self._merge_contract_matrix_template(contracts_module, matrix, context)
                if status is None:
                    status = "healthy" if all(check.get("ok", False) for check in checks) else "degraded"
                if ok_flag is None:
                    ok_flag = status in _SUCCESS_STATUSES and all(check.get("ok", False) for check in checks)
                return {
                    "ok": bool(ok_flag),
                    "status": status,
                    "checks": checks,
                    "diagnostic_matrix": matrix,
                    "source": "diagnostic_contracts",
                }

        return {
            "ok": all(check.get("ok", False) for check in legacy_checks),
            "status": "healthy" if all(check.get("ok", False) for check in legacy_checks) else "degraded",
            "checks": legacy_checks,
            "diagnostic_matrix": self._derive_matrix(legacy_checks, selected_layers, source="legacy"),
            "source": "legacy",
        }

    @staticmethod
    def _load_diagnostic_contracts() -> Any | None:
        try:
            return importlib.import_module("core.core.diagnostic_contracts")
        except ModuleNotFoundError as exc:
            if exc.name == "core.core.diagnostic_contracts":
                return None
            raise

    async def _run_contract_payload(self, contracts_module: Any, context: Dict[str, Any]) -> Any:
        for attr in (
            "run_diagnostic_matrix",
            "run_layer_checks",
            "run_diagnostic_layer_checks",
            "evaluate_diagnostic_matrix",
            "build_diagnostic_matrix",
        ):
            func = getattr(contracts_module, attr, None)
            if callable(func):
                return await self._call_diagnostic_callable(func, context)
        for attr in (
            "iter_diagnostic_checks",
            "get_diagnostic_checks",
            "build_diagnostic_checks",
        ):
            func = getattr(contracts_module, attr, None)
            if callable(func):
                definitions = await self._call_diagnostic_callable(func, context)
                return {"checks": await self._execute_check_definitions(definitions, context)}
        for attr in (
            "DIAGNOSTIC_MATRIX",
            "DIAGNOSTIC_LAYERS",
            "LAYER_CHECKS",
            "DIAGNOSTIC_CHECKS",
        ):
            definitions = getattr(contracts_module, attr, None)
            if definitions is not None:
                return {"checks": await self._execute_check_definitions(definitions, context)}
        return None

    async def _call_diagnostic_callable(self, func: Any, context: Dict[str, Any]) -> Any:
        kwargs = self._build_callable_kwargs(func, context)
        result = func(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _build_callable_kwargs(func: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return {}
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return dict(context)
        kwargs: Dict[str, Any] = {}
        for name, param in signature.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.POSITIONAL_ONLY):
                continue
            if name in context:
                kwargs[name] = context[name]
            elif name == "layers":
                kwargs[name] = context.get("selected_layers")
        return kwargs

    async def _execute_check_definitions(self, definitions: Any, context: Dict[str, Any]) -> list[Dict[str, Any]]:
        if isinstance(definitions, dict):
            items = definitions.get("checks") or definitions.get("layers") or definitions.get("matrix") or []
        else:
            items = definitions or []
        if not isinstance(items, (list, tuple, set)):
            items = [items]

        selected_layers = self._normalize_layers(context.get("selected_layers"))
        results: list[Dict[str, Any]] = []
        for index, item in enumerate(items):
            normalized = await self._execute_single_check(item, index, context)
            if not normalized:
                continue
            layer = normalized.get("layer")
            if selected_layers and layer and layer not in selected_layers and layer != "matrix":
                continue
            results.append(normalized)
        return results

    async def _execute_single_check(self, definition: Any, index: int, context: Dict[str, Any]) -> Dict[str, Any] | None:
        if isinstance(definition, dict):
            runner = self._resolve_runner(definition)
            name = str(definition.get("name") or definition.get("id") or f"check_{index}")
            layer = self._normalize_layer_name(definition.get("layer") or definition.get("group") or "matrix")
            if runner is None:
                payload = dict(definition)
                payload.setdefault("name", name)
                payload["layer"] = layer
                return self._normalize_check_payload(payload)
            payload = await self._call_diagnostic_callable(runner, {**context, "definition": definition, "check": definition})
            return self._normalize_check_payload(payload, default_name=name, default_layer=layer)
        if callable(definition):
            payload = await self._call_diagnostic_callable(definition, {**context, "check": definition})
            return self._normalize_check_payload(payload, default_name=getattr(definition, "__name__", f"check_{index}"), default_layer="matrix")
        return self._normalize_check_payload(definition, default_name=f"check_{index}", default_layer="matrix")

    @staticmethod
    def _resolve_runner(definition: Dict[str, Any]) -> Any | None:
        for key in ("run", "runner", "check", "callable", "probe"):
            value = definition.get(key)
            if callable(value):
                return value
        return None

    @staticmethod
    def _normalize_layer_name(layer: Any) -> str:
        raw = str(layer or "matrix").strip().lower() or "matrix"
        return _LAYER_ALIASES.get(raw, raw)

    def _normalize_check_payload(self, payload: Any, *, default_name: str | None = None, default_layer: str = "matrix") -> Dict[str, Any] | None:
        if payload is None:
            return None
        if hasattr(payload, "__dict__") and not isinstance(payload, dict):
            payload = vars(payload)
        if not isinstance(payload, dict):
            payload = {"details": payload}
        name = str(payload.get("name") or payload.get("id") or default_name or "check")
        layer = self._normalize_layer_name(payload.get("layer") or payload.get("group") or default_layer)
        status = str(payload.get("status") or "").strip().lower()
        ok = payload.get("ok")
        if ok is None:
            if status:
                ok = status in _SUCCESS_STATUSES
            else:
                ok = not bool(payload.get("error"))
        if not status:
            status = "ok" if ok else "error"
        return {
            "name": name,
            "layer": layer,
            "status": status,
            "ok": bool(ok),
            "details": payload.get("details"),
            "error": payload.get("error"),
            "source": payload.get("source") or "diagnostic_contracts",
        }

    def _extract_contract_checks(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            checks = payload.get("checks")
            if isinstance(checks, list):
                return [item for item in (self._normalize_check_payload(entry) for entry in checks) if item]
            results_payload = payload.get("results")
            if isinstance(results_payload, list):
                normalized_results: list[Dict[str, Any]] = []
                for entry in results_payload:
                    if not isinstance(entry, dict):
                        continue
                    normalized_results.append(
                        {
                            "name": str(entry.get("layer") or entry.get("name") or "check"),
                            "layer": self._normalize_layer_name(entry.get("layer") or entry.get("group") or "matrix"),
                            "status": "ok" if entry.get("ok", False) else "error",
                            "ok": bool(entry.get("ok", False)),
                            "details": {
                                "summary": entry.get("summary"),
                                "failures": list(entry.get("failures") or []),
                                "observed": entry.get("observed") if isinstance(entry.get("observed"), dict) else {},
                            },
                            "error": None if entry.get("ok", False) else "; ".join(str(item) for item in (entry.get("failures") or [])),
                            "source": entry.get("source") or "diagnostic_contracts",
                        }
                    )
                if normalized_results:
                    return normalized_results
            layers = payload.get("layers")
            if isinstance(layers, dict):
                results: list[Dict[str, Any]] = []
                for layer_name, entries in layers.items():
                    if isinstance(entries, list):
                        for entry in entries:
                            normalized = self._normalize_check_payload(entry, default_layer=str(layer_name))
                            if normalized:
                                results.append(normalized)
                if results:
                    return results
        if isinstance(payload, list):
            return [item for item in (self._normalize_check_payload(entry) for entry in payload) if item]
        return []

    def _extract_contract_matrix(self, payload: Any, checks: list[Dict[str, Any]], selected_layers: set[str] | None) -> Dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("diagnostic_matrix"), dict):
            matrix = dict(payload["diagnostic_matrix"])
            matrix.setdefault("source", "diagnostic_contracts")
            matrix.setdefault("checks", checks)
            matrix.setdefault("selected_layers", sorted(selected_layers) if selected_layers else ["components", "memory", "ai_models", "matrix"])
            return matrix
        return self._derive_matrix(checks, selected_layers, source="diagnostic_contracts")

    @staticmethod
    def _extract_contract_status(payload: Any) -> str | None:
        if isinstance(payload, dict):
            status = payload.get("status")
            if status is not None:
                return str(status).strip().lower()
            matrix = payload.get("diagnostic_matrix")
            if isinstance(matrix, dict) and matrix.get("status") is not None:
                return str(matrix.get("status")).strip().lower()
        return None

    @staticmethod
    def _extract_contract_ok(payload: Any) -> bool | None:
        if isinstance(payload, dict) and payload.get("ok") is not None:
            return bool(payload.get("ok"))
        return None

    def _merge_contract_matrix_template(self, contracts_module: Any, matrix: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        template = getattr(contracts_module, "diagnostic_matrix", None)
        if not callable(template):
            return matrix
        try:
            payload = template(**self._build_callable_kwargs(template, context))
        except Exception:
            return matrix
        if isinstance(payload, dict):
            merged = dict(payload)
            merged.update(matrix)
            return merged
        return matrix

    def _filter_checks(self, checks: list[Dict[str, Any]], selected_layers: set[str] | None) -> list[Dict[str, Any]]:
        if selected_layers is None:
            return [check for check in checks if isinstance(check, dict)]
        filtered: list[Dict[str, Any]] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            layer = self._normalize_layer_name(check.get("layer"))
            is_provider_alias = layer == "ai_models" and "providers" in selected_layers
            is_legacy_provider_alias = layer == "providers" and "ai_models" in selected_layers
            if layer in selected_layers or is_provider_alias or is_legacy_provider_alias or layer == "matrix":
                normalized = dict(check)
                normalized["layer"] = layer
                filtered.append(normalized)
        return filtered

    def _derive_matrix(self, checks: list[Dict[str, Any]], selected_layers: set[str] | None, *, source: str) -> Dict[str, Any]:
        layer_summary: dict[str, Dict[str, Any]] = {}
        for check in checks:
            layer = self._normalize_layer_name(check.get("layer"))
            bucket = layer_summary.setdefault(layer, {"status": "healthy", "ok": True, "check_count": 0})
            bucket["check_count"] += 1
            if not check.get("ok", False):
                bucket["status"] = "degraded"
                bucket["ok"] = False
        return {
            "source": source,
            "selected_layers": sorted(selected_layers) if selected_layers else ["components", "memory", "ai_models", "matrix"],
            "layers": layer_summary,
            "check_count": len(checks),
            "checks": checks,
        }

    def _refresh_matrix_from_checks(self, report: Dict[str, Any], *, source: str) -> None:
        report["diagnostic_matrix"] = self._derive_matrix(report.get("layer_checks", []), self._normalize_layers(report.get("requested_layers")), source=source)
        if any(not check.get("ok", False) for check in report.get("layer_checks", []) if isinstance(check, dict)):
            self._degrade(report)

    def _build_remediation_plan(self, report: Dict[str, Any]) -> list[Dict[str, Any]]:
        plan: list[Dict[str, Any]] = []
        for name, model in report.get("ai_models", {}).items():
            if not isinstance(model, dict):
                continue
            status = str(model.get("status") or "").strip().lower()
            if status in _SUCCESS_STATUSES:
                continue
            diagnostics = model.get("diagnostics", {}) if isinstance(model.get("diagnostics"), dict) else {}
            remediation = diagnostics.get("remediation")
            actions = remediation if isinstance(remediation, list) else []
            plan.append(
                {
                    "area": "ai_models",
                    "name": name,
                    "status": status or "unknown",
                    "error": model.get("error"),
                    "actions": actions,
                }
            )
        for check in report.get("layer_checks", []):
            if not isinstance(check, dict) or check.get("ok", False):
                continue
            details = check.get("details", {}) if isinstance(check.get("details"), dict) else {}
            failures = details.get("failures") if isinstance(details.get("failures"), list) else []
            summary = details.get("summary") or check.get("error")
            plan.append(
                {
                    "area": str(check.get("layer") or "matrix"),
                    "name": str(check.get("name") or "check"),
                    "status": str(check.get("status") or "degraded"),
                    "error": check.get("error"),
                    "failure_signatures": failures,
                    "summary": summary,
                    "actions": [str(summary)] if summary else [],
                }
            )
        return plan

    def _build_readiness_summary(self, report: Dict[str, Any]) -> Dict[str, Any]:
        layer_status = report.get("layer_check_status", {}) if isinstance(report.get("layer_check_status"), dict) else {}
        core_ready = bool(layer_status.get("ok", report.get("status") == "healthy"))
        provider_ready = True
        for model in report.get("ai_models", {}).values():
            if isinstance(model, dict) and str(model.get("status") or "").strip().lower() not in _SUCCESS_STATUSES:
                provider_ready = False
                break
        blocking = [item.get("name") for item in report.get("remediation_plan", []) if isinstance(item, dict)]
        return {
            "core_ready": core_ready,
            "provider_ready": provider_ready,
            "startup_ready": core_ready and provider_ready and report.get("status") == "healthy",
            "blocking_issues": [str(item) for item in blocking if str(item).strip()],
        }

    def finalize(self) -> Dict[str, Any]:
        """Module summary for reporting."""
        return {
            "status": "active" if self._is_active else "inactive",
            "capabilities": ["self_diagnostic", "system_health", "diagnostic_matrix"],
        }
