from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


TASK_LEVEL_TYPES = {"task_lifecycle"}
TASK_TYPES = ("plan", "review", "test", "code", "docs", "research")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = max(0, min(len(values) - 1, int((len(values) - 1) * q)))
    return round(values[idx], 2)


def _summarize_task_events(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lifecycle = [r for r in rows if r.get("event_type") == "task_lifecycle"]
    if not lifecycle:
        return {"tasks_total": 0, "done": 0, "failed": 0, "success_rate": 0.0, "fallback_rate": 0.0, "p50_latency_ms": 0.0, "p95_latency_ms": 0.0, "avg_tokens": 0.0, "by_task": {}}

    done = sum(1 for r in lifecycle if r.get("status") == "done")
    failed = sum(1 for r in lifecycle if r.get("status") == "failed")
    fallback = sum(1 for r in lifecycle if r.get("fallback_used") is True)
    latencies = [float(r.get("latency_ms") or 0) for r in lifecycle if isinstance(r.get("latency_ms"), (int, float))]
    tokens = [float(r.get("tokens_used")) for r in lifecycle if isinstance(r.get("tokens_used"), (int, float))]
    by_task: dict[str, dict[str, Any]] = {}
    for r in lifecycle:
        task = str(r.get("task_type") or "unknown")
        bucket = by_task.setdefault(task, {"total": 0, "done": 0, "failed": 0, "fallback": 0})
        bucket["total"] += 1
        bucket["done"] += 1 if r.get("status") == "done" else 0
        bucket["failed"] += 1 if r.get("status") == "failed" else 0
        bucket["fallback"] += 1 if r.get("fallback_used") is True else 0
    return {
        "tasks_total": len(lifecycle),
        "done": done,
        "failed": failed,
        "success_rate": round(done / len(lifecycle), 4) if lifecycle else 0.0,
        "fallback_rate": round(fallback / len(lifecycle), 4) if lifecycle else 0.0,
        "p50_latency_ms": _pct(latencies, 0.50),
        "p95_latency_ms": _pct(latencies, 0.95),
        "avg_tokens": round(sum(tokens) / len(tokens), 2) if tokens else 0.0,
        "by_task": by_task,
    }


def _summarize_rejection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [r for r in rows if r.get("summary_type") == "trained_memory_rejection_summary"]
    accepted = sum(int(r.get("accepted", 0) or 0) for r in summaries)
    rejected = sum(int(r.get("rejected", 0) or 0) for r in summaries)
    by_task: dict[str, dict[str, int]] = {}
    for r in summaries:
        for task, bucket in (r.get("by_task") or {}).items():
            dst = by_task.setdefault(str(task), {"accepted": 0, "rejected": 0})
            dst["accepted"] += int(bucket.get("accepted", 0) or 0)
            dst["rejected"] += int(bucket.get("rejected", 0) or 0)
    total = accepted + rejected
    return {
        "events": len(summaries),
        "accepted": accepted,
        "rejected": rejected,
        "rejection_rate": round(rejected / total, 4) if total else 0.0,
        "by_task": by_task,
    }


def _summarize_models(store_path: Path) -> dict[str, Any]:
    if not store_path.exists():
        return {"models": []}
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except Exception:
        return {"models": []}
    models = []
    for name, payload in data.items():
        successes = payload.get("successes", [])
        latencies = payload.get("latencies", [])
        quality = payload.get("quality_scores", [])
        n = len(successes)
        done = sum(1 for x in successes if x)
        models.append({
            "model": name,
            "n": n,
            "success_rate": round(done / n, 4) if n else 0.0,
            "avg_latency": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
            "avg_quality": round(sum(quality) / len(quality), 4) if quality else 0.0,
            "effective_for": [t for t in sorted({part.split('::')[0] for part in [name]})],
        })
    models.sort(key=lambda item: (item["success_rate"], item["avg_quality"], -item["avg_latency"]), reverse=True)
    return {"models": models, "by_task_type": _rank_models_by_task_type(models)}




def _rank_models_by_task_type(models: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_task: dict[str, list[dict[str, Any]]] = {task: [] for task in TASK_TYPES}
    for model in models:
        name = str(model.get("model") or "")
        task_type = name.split("::", 1)[0].lower()
        if task_type in by_task:
            by_task[task_type].append(model)
    for task_type in by_task:
        by_task[task_type].sort(key=lambda item: (item.get("success_rate", 0), item.get("avg_quality", 0), -item.get("avg_latency", 0)), reverse=True)
    return by_task


def _recovery_checklist() -> list[str]:
    return [
        "Verify Python environment and project root are available.",
        "Ensure memory_store is writable and KPI files can be created.",
        "Start the orchestrator with the local module registry loaded.",
        "Load model_usage, ai_activity, orchestrator_control, api_bridge, prompt_optimizer, and smart_decomposer first.",
        "Enable local_llm only after readiness checks pass.",
        "Confirm task_lifecycle and KPI dashboard files are being written.",
        "Run a smoke task in plan/review/test to validate routing and model availability.",
        "Check rollback: disable high_risk_trained_memory if trained memory metrics degrade.",
    ]

def _module_inventory() -> dict[str, Any]:
    loadable = [
        "ai_activity", "orchestrator_control", "model_usage", "model_availability", "antigravity_status", "api_bridge",
        "smart_decomposer", "prompt_optimizer", "chat_bus", "trigger_dispatcher", "json_themes", "unified_vfs",
        "cold_boot", "ui_design_system", "ui_anti_template", "frontend_engineering_bridge", "autodev_pipeline",
        "tdd_policy", "qwen_code", "readability_policy", "dev_toolkit", "self_diagnostic", "local_llm",
        "sourcecraft", "voice_listener", "reasoning", "risk_advisor", "orchestrator_advisor", "intelligence",
        "security_sentinel",
    ]
    recovery = {
        "fresh_boot": [
            "install dependencies",
            "configure memory_store and logs",
            "start orchestrator",
            "load ai_activity / model_usage / api_bridge / prompt_optimizer",
            "load local_llm only if available",
        ],
        "core_modules": loadable,
    }
    return {"loadable_modules": loadable, "recovery_playbook": recovery}


def build_kpi_dashboard(*, kpi_log_path: Path, rolling_kpi_path: Path, summary_path: Path | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    cutoff_24h = now - timedelta(hours=24)
    rows = [r for r in _read_jsonl(kpi_log_path) if (ts := _parse_ts(r.get("started_at") or r.get("logged_at"))) and ts >= cutoff_24h]
    day = now.date().isoformat()
    event_types = Counter(str(r.get("event_type") or r.get("type") or "unknown") for r in rows)
    dashboard = {
        "generated_at": now.isoformat(),
        "window": {"start": cutoff_24h.isoformat(), "end": now.isoformat(), "label": "last_24h"},
        "kpi_events": {"path": str(kpi_log_path), "total": len(rows), "types": event_types.most_common(20)},
        "task_lifecycle": _summarize_task_events(rows),
        "trained_memory_rejection": _summarize_rejection(rows),
        "rolling_models": _summarize_models(rolling_kpi_path),
        "module_inventory": _module_inventory(),
        "recovery_checklist": _recovery_checklist(),
        "today": day,
    }
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(dashboard, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return dashboard
