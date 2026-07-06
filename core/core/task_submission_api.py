from __future__ import annotations

import json
import os
import re
from typing import Any

from .input_text_normalizer import normalize_text, normalize_text_list
from .frame_orchestrator import build_frame_orchestrator_package
from .input_text_quantizer import quantize_input_text
from .models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
from .socraticode_bridge import SocratiCodeBridge, SocratiCodeBridgeError
from .openai_runtime_router import OpenAIRuntimeRouter


_TASK_TYPE_ALIASES: dict[str, str] = {
    "bug": "fix",
    "fix": "fix",
    "issue": "fix",
    "research": "research",
    "doc": "docs",
    "docs": "docs",
    "review": "review",
    "test": "test",
    "tests": "test",
    "plan": "plan",
    "code": "code",
}

_PRIORITY_ALIASES: dict[str, str] = {
    "urgent": "critical",
    "blocker": "critical",
    "crit": "critical",
    "normal": "normal",
    "medium": "normal",
    "default": "normal",
}

_COMPLEXITY_ALIASES: dict[str, str] = {
    "small": "low",
    "light": "low",
    "balanced": "medium",
    "default": "medium",
    "heavy": "high",
    "urgent": "critical",
}

_COST_TIER_ALIASES: dict[str, str] = {
    "cheap": "economy",
    "low": "economy",
    "fast": "interactive",
    "default": "interactive",
    "normal": "interactive",
    "high": "premium",
}

_GARBAGE_PATTERNS = (
    r"^[\W_]+$",
    r"^(n/?a|none|null|undefined|test|asdf|qwerty|lol)$",
)


def _normalize_task_type(raw: Any) -> TaskType:
    value = str(raw or "code").strip().lower()
    mapped = _TASK_TYPE_ALIASES.get(value, value)
    try:
        return TaskType(mapped)
    except ValueError:
        return TaskType.CODE


def _normalize_priority(raw: Any) -> Priority:
    value = str(raw or "normal").strip().lower()
    mapped = _PRIORITY_ALIASES.get(value, value)
    try:
        return Priority(mapped)
    except ValueError:
        return Priority.NORMAL


def _normalize_complexity(raw: Any) -> Complexity | None:
    if raw is None or str(raw).strip() == "":
        return None
    value = str(raw).strip().lower()
    mapped = _COMPLEXITY_ALIASES.get(value, value)
    try:
        return Complexity(mapped)
    except ValueError:
        return None


def _normalize_source(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value or "user_input"


def _normalize_cost_tier(raw: Any, *, source: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "interactive" if source == "websocket" else "standard"
    return _COST_TIER_ALIASES.get(value, value)


def _normalize_scalar(raw: Any, *, max_chars: int = 512) -> str:
    return normalize_text(raw, max_chars=max_chars)


def _provider_hint(normalized: dict[str, Any]) -> str | None:
    for key in ("provider", "preferred_provider"):
        value = normalized.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _model_hint(normalized: dict[str, Any]) -> str | None:
    for key in ("model", "requested_model", "assigned_model"):
        value = normalized.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_list(raw: Any) -> list[str]:
    return normalize_text_list(raw)


def _is_meaningful_text(text: str) -> bool:
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) < 4:
        return False
    if not re.search(r"[A-Za-zА-Яа-я0-9]", cleaned):
        return False
    lowered = cleaned.lower()
    for pattern in _GARBAGE_PATTERNS:
        if re.match(pattern, lowered):
            return False
    return True


_SOCRATICODE_TARGET_TASK_TYPES = {TaskType.CODE, TaskType.REVIEW, TaskType.TEST, TaskType.PLAN}


def _socraticode_enabled_for_submission() -> bool:
    raw = str(os.getenv("SOCRATICODE_ENABLED", "false") or "false").strip().lower()
    command = str(os.getenv("SOCRATICODE_MCP_COMMAND") or "").strip()
    return raw in {"1", "true", "yes", "on"} or bool(command)


def _normalize_socraticode_annotation(advisory: dict[str, Any], task: Task) -> dict[str, Any]:
    return {
        "status": "applied",
        "bridge_source": "task_submission",
        "task_type": task.type.value,
        "repo_path": str(advisory.get("repo_path") or task.context.repo_path or "."),
        "context_coverage": advisory.get("context_coverage") if isinstance(advisory.get("context_coverage"), dict) else {},
        "cost_downgrade": advisory.get("cost_downgrade") if isinstance(advisory.get("cost_downgrade"), dict) else {},
        "parallelism": advisory.get("parallelism") if isinstance(advisory.get("parallelism"), dict) else {},
        "routing_recommendations": advisory.get("routing_recommendations") if isinstance(advisory.get("routing_recommendations"), dict) else {},
        "compact_context": advisory.get("compact_context") if isinstance(advisory.get("compact_context"), dict) else {},
    }


def _socraticode_has_strong_coverage(annotation: dict[str, Any]) -> bool:
    coverage = annotation.get("context_coverage") if isinstance(annotation.get("context_coverage"), dict) else {}
    status = str(coverage.get("status") or "").strip().lower()
    score_raw = coverage.get("score", coverage.get("coverage_ratio", coverage.get("ratio", 0.0)))
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0
    return status == "strong" or score >= 0.88


def _apply_socraticode_pre_prompt_routing(task: Task, annotation: dict[str, Any]) -> None:
    if not isinstance(task.routing_hints, dict):
        task.routing_hints = {}
    if str(annotation.get("status") or "").strip().lower() != "applied":
        return
    if not _socraticode_has_strong_coverage(annotation):
        return

    cost = annotation.get("cost_downgrade") if isinstance(annotation.get("cost_downgrade"), dict) else {}
    parallel = annotation.get("parallelism") if isinstance(annotation.get("parallelism"), dict) else {}
    routing = annotation.get("routing_recommendations") if isinstance(annotation.get("routing_recommendations"), dict) else {}

    original_cost_tier = str(task.routing_hints.get("cost_tier") or "").strip().lower()
    target_cost_tier = str(cost.get("target_cost_tier") or "").strip().lower()
    if cost.get("eligible") and target_cost_tier and target_cost_tier != original_cost_tier:
        if original_cost_tier:
            task.routing_hints.setdefault("original_cost_tier", original_cost_tier)
        task.routing_hints["cost_tier"] = target_cost_tier
        task.routing_hints["socraticode_cost_tier_applied"] = True

    recommended_parallel_raw = parallel.get("recommended_parallel_branches")
    if recommended_parallel_raw is None:
        recommended_parallel_raw = routing.get("target_parallel_branches")
    try:
        recommended_parallel = int(recommended_parallel_raw) if recommended_parallel_raw is not None else None
    except (TypeError, ValueError):
        recommended_parallel = None
    if recommended_parallel is None or recommended_parallel < 1:
        return

    current_parallel_raw = task.routing_hints.get("parallel_branches")
    try:
        current_parallel = int(current_parallel_raw) if current_parallel_raw is not None else None
    except (TypeError, ValueError):
        current_parallel = None

    if current_parallel is not None and recommended_parallel >= current_parallel:
        return

    if current_parallel is not None:
        task.routing_hints.setdefault("original_parallel_branches", current_parallel)
    task.routing_hints["parallel_branches"] = recommended_parallel
    task.routing_hints["socraticode_parallel_branches_applied"] = True


def _apply_socraticode_annotation(task: Task) -> None:
    if task.type not in _SOCRATICODE_TARGET_TASK_TYPES:
        return
    if not _socraticode_enabled_for_submission():
        return
    if not isinstance(task.routing_hints, dict):
        task.routing_hints = {}

    bridge = SocratiCodeBridge(repo_path=str(task.context.repo_path or "."))
    try:
        with bridge:
            advisory = bridge.analyze_task(
                task=task,
                context={},
                description=task.input.description,
                task_type=task.type.value,
                routing_hints=task.routing_hints,
            )
    except SocratiCodeBridgeError as exc:
        task.routing_hints["socraticode"] = {
            "status": "unavailable",
            "bridge_source": "task_submission",
            "task_type": task.type.value,
            "error": str(exc),
        }
        return
    except Exception as exc:
        task.routing_hints["socraticode"] = {
            "status": "error",
            "bridge_source": "task_submission",
            "task_type": task.type.value,
            "error": str(exc),
        }
        return

    annotation = _normalize_socraticode_annotation(advisory if isinstance(advisory, dict) else {}, task)
    task.routing_hints["socraticode"] = annotation
    task.routing_hints["socraticode_context_coverage"] = annotation["context_coverage"]
    task.routing_hints["socraticode_cost_downgrade"] = annotation["cost_downgrade"]
    task.routing_hints["socraticode_parallelism"] = annotation["parallelism"]
    _apply_socraticode_pre_prompt_routing(task, annotation)


def _extract_description(data: dict[str, Any]) -> str:
    for key in ("description", "message", "text", "prompt", "objective"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_text(value, max_chars=6000)
    return ""


def normalize_user_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        stripped = normalize_text(payload, max_chars=8000)
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                payload = parsed
            else:
                return {"description": stripped}
        except json.JSONDecodeError:
            return {"description": stripped}
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_name = str(key).strip()
        if not key_name:
            continue
        if key_name in {"description", "message", "text", "prompt", "objective", "project", "repo_path", "branch", "session_id", "task_id", "provider", "preferred_provider", "model", "requested_model", "assigned_model", "source", "type", "priority", "complexity", "cost_tier", "tier"} and value is not None:
            normalized[key_name] = _normalize_scalar(value, max_chars=6000 if key_name in {"description", "message", "text", "prompt", "objective"} else 512)
        elif key_name in {"files", "constraints", "acceptance_criteria"}:
            normalized[key_name] = _as_list(value)
        elif isinstance(value, str):
            normalized[key_name] = _normalize_scalar(value)
        else:
            normalized[key_name] = value
    return normalized


def validate_normalized_payload(normalized: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not isinstance(normalized, dict) or not normalized:
        return False, ["empty_payload"]

    description = _extract_description(normalized)
    if not _is_meaningful_text(description):
        issues.append("empty_or_garbage_description")

    task_type = str(normalized.get("type") or "").strip().lower()
    if task_type and task_type not in {t.value for t in TaskType} and task_type not in _TASK_TYPE_ALIASES:
        issues.append("unknown_task_type")

    files = normalized.get("files")
    if files is not None and not isinstance(files, (list, tuple, str)):
        issues.append("invalid_files_field")

    acceptance = normalized.get("acceptance_criteria")
    if acceptance is not None and not isinstance(acceptance, (list, tuple, str)):
        issues.append("invalid_acceptance_criteria")

    if normalized.get("session_id") is not None and not str(normalized.get("session_id")).strip():
        issues.append("empty_session_id")

    return len(issues) == 0, issues


def create_standard_task(data: dict[str, Any]) -> Task:
    normalized = normalize_user_payload(data)
    ok, issues = validate_normalized_payload(normalized)
    if not ok:
        raise ValueError(f"Invalid task payload: {', '.join(issues)}")

    try:
        description = _extract_description(normalized)
        if not description:
            raise ValueError("missing description")

        source = _normalize_source(normalized.get("source"))
        cost_tier = _normalize_cost_tier(normalized.get("cost_tier") or normalized.get("tier"), source=source)
        complexity = _normalize_complexity(normalized.get("complexity"))
        provider = _provider_hint(normalized)
        requested_model = _model_hint(normalized)
        files = _as_list(normalized.get("files"))
        constraints = _as_list(normalized.get("constraints"))
        acceptance_criteria = _as_list(normalized.get("acceptance_criteria")) or ["tests pass"]
        task_type = _normalize_task_type(normalized.get("type"))
        input_profile = quantize_input_text(
            cleaned_text=description,
            files=files,
            acceptance_criteria=acceptance_criteria,
            explicit_type=task_type.value,
        )

        task = Task(
            type=task_type,
            input=TaskInput(
                description=description,
                files=files,
                constraints=constraints,
                acceptance_criteria=acceptance_criteria,
            ),
            context=TaskContext(
                project=str(normalized.get("project", "default")),
                repo_path=str(normalized.get("repo_path", ".")),
                branch=str(normalized.get("branch", "main")),
            ),
            priority=_normalize_priority(normalized.get("priority")),
            session_id=normalized.get("session_id"),
            complexity=complexity,
        )
        ext_task_id = normalized.get("task_id")
        if isinstance(ext_task_id, str) and ext_task_id.strip():
            task.task_id = ext_task_id.strip()
        if not task.routing_hints:
            task.routing_hints = {}
        task.routing_hints.setdefault("source", source)
        task.routing_hints.setdefault("cost_tier", cost_tier)
        task.routing_hints["normalized_text_profile"] = input_profile
        if input_profile.get("execution_shape") == "parallel_candidate" and task.type == TaskType.CODE:
            task.routing_hints.setdefault("parallelize_code", True)
            if input_profile.get("scope_bucket") in {"multi_file", "multi_area"}:
                task.routing_hints.setdefault("parallel_branches", 3)
        if source == "websocket":
            task.routing_hints.setdefault("channel", "ws")
            task.routing_hints.setdefault("interactive", True)
            task.routing_hints.setdefault("ws_priority", task.priority.value)
        if provider:
            task.routing_hints["provider_preference"] = provider
        if requested_model:
            sanitized_model = OpenAIRuntimeRouter.sanitize_model(requested_model, require_allowlist=False)
            if sanitized_model:
                task.assigned_model = sanitized_model
                task.routing_hints["requested_model"] = sanitized_model
            else:
                task.routing_hints["requested_model_rejected"] = requested_model
        task.routing_hints.setdefault("input_validation", {"status": "ok", "issues": []})
        _apply_socraticode_annotation(task)
        frame_package = build_frame_orchestrator_package(task, normalized)
        task.routing_hints["frame_orchestrator"] = frame_package.as_dict()
        task.routing_hints["frame_xml_package"] = frame_package.validation.xml_orchestrator_package_output
        return task
    except Exception as e:
        raise ValueError(f"Invalid task data format: {e}") from e
