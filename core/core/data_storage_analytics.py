from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .data_plane_monitor import snapshot_postgres_data_plane
from .persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _safe_json_load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _storage_dir() -> Path:
    configured = (os.getenv("AI_BRIDGE_MEMORY_STORE_DIR") or "memory_store").strip()
    return Path(configured)


@dataclass(slots=True)
class DataRange:
    first_at: str | None = None
    last_at: str | None = None
    first_id: int | None = None
    last_id: int | None = None
    retention_days: float = 0.0


@dataclass(slots=True)
class DatasetSnapshot:
    name: str
    rows: int = 0
    size_bytes: int = 0
    index_file: str | None = None
    file_count: int = 0
    payload_size_bytes: int = 0
    date_field: str | None = None
    updated_field: str | None = None
    id_field: str | None = None
    created_range: DataRange = field(default_factory=DataRange)
    updated_range: DataRange = field(default_factory=DataRange)


@dataclass(slots=True)
class DataStorageAnalyticsReport:
    generated_at: str
    source: str
    database_url_configured: bool
    storage_root: str
    total_size_bytes: int
    total_size_mb: float
    total_files: int
    sessions_tracked: int
    datasets: list[DatasetSnapshot] = field(default_factory=list)
    postgres: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    operational_signals: dict[str, Any] = field(default_factory=dict)
    analytics_dimensions: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    management_summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["datasets"] = [asdict(item) for item in self.datasets]
        return payload


def _range_from_rows(rows: list[dict[str, Any]], *, date_field: str, id_field: str, updated_field: str | None = None) -> tuple[DataRange, DataRange]:
    created_points: list[tuple[datetime, dict[str, Any]]] = []
    updated_points: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        created_at = _parse_ts(row.get(date_field))
        if created_at is not None:
            created_points.append((created_at, row))
        if updated_field:
            updated_at = _parse_ts(row.get(updated_field))
            if updated_at is not None:
                updated_points.append((updated_at, row))

    def _build(points: list[tuple[datetime, dict[str, Any]]]) -> DataRange:
        if not points:
            return DataRange()
        first_dt, first_row = min(points, key=lambda item: item[0])
        last_dt, last_row = max(points, key=lambda item: item[0])
        return DataRange(
            first_at=_iso(first_dt),
            last_at=_iso(last_dt),
            first_id=int(first_row.get(id_field)) if isinstance(first_row.get(id_field), int) else None,
            last_id=int(last_row.get(id_field)) if isinstance(last_row.get(id_field), int) else None,
            retention_days=round((last_dt - first_dt).total_seconds() / 86400, 3),
        )

    return _build(created_points), _build(updated_points)


def _directory_stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = [item for item in path.rglob("*") if item.is_file()]
    return len(files), sum(item.stat().st_size for item in files)


def _analyze_file_dataset(
    *,
    name: str,
    index_path: Path,
    payload_dir: Path,
    id_field: str,
    date_field: str,
    updated_field: str | None = None,
) -> DatasetSnapshot:
    rows = _safe_json_load(index_path, [])
    file_count, payload_size = _directory_stats(payload_dir)
    size_bytes = index_path.stat().st_size if index_path.exists() else 0
    if not isinstance(rows, list):
        rows = []
    created_range, updated_range = _range_from_rows(rows, date_field=date_field, id_field=id_field, updated_field=updated_field)
    return DatasetSnapshot(
        name=name,
        rows=len(rows),
        size_bytes=size_bytes,
        index_file=str(index_path),
        file_count=file_count,
        payload_size_bytes=payload_size,
        date_field=date_field,
        updated_field=updated_field,
        id_field=id_field,
        created_range=created_range,
        updated_range=updated_range,
    )


def _collect_file_backed_analytics(base_dir: Path) -> tuple[list[DatasetSnapshot], int, int, int]:
    datasets = [
        _analyze_file_dataset(
            name="memories",
            index_path=base_dir / "memory_index.json",
            payload_dir=base_dir / "memories",
            id_field="memory_id",
            date_field="created_at",
            updated_field="updated_at",
        ),
        _analyze_file_dataset(
            name="trained_memories",
            index_path=base_dir / "trained_memory_index.json",
            payload_dir=base_dir / "trained_memories",
            id_field="trained_memory_id",
            date_field="created_at",
            updated_field="updated_at",
        ),
    ]
    session_map = _safe_json_load(base_dir / "session_map.json", {})
    sessions_tracked = len(session_map) if isinstance(session_map, dict) else 0
    all_files = [item for item in base_dir.rglob("*") if item.is_file()] if base_dir.exists() else []
    total_files = len(all_files)
    total_size = sum(item.stat().st_size for item in all_files)
    return datasets, sessions_tracked, total_files, total_size


def _connect_postgres(dsn: str):
    import psycopg2  # type: ignore

    return psycopg2.connect(dsn)


def _collect_postgres_analytics(database_url: str) -> dict[str, Any]:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        return {"configured": False, "reachable": False, "datasets": []}

    try:
        with _connect_postgres(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        'memories' AS dataset,
                        COUNT(*)::bigint AS rows,
                        MIN(created_at)::text AS first_at,
                        MAX(created_at)::text AS last_at,
                        MIN(memory_id)::bigint AS first_id,
                        MAX(memory_id)::bigint AS last_id
                    FROM {AI_BRIDGE_SCHEMA}.memories
                    UNION ALL
                    SELECT
                        'commands' AS dataset,
                        COUNT(*)::bigint AS rows,
                        MIN(executed_at)::text AS first_at,
                        MAX(executed_at)::text AS last_at,
                        MIN(command_id)::bigint AS first_id,
                        MAX(command_id)::bigint AS last_id
                    FROM {AI_BRIDGE_SCHEMA}.commands
                    UNION ALL
                    SELECT
                        'sessions' AS dataset,
                        COUNT(*)::bigint AS rows,
                        MIN(created_at)::text AS first_at,
                        MAX(updated_at)::text AS last_at,
                        NULL::bigint AS first_id,
                        NULL::bigint AS last_id
                    FROM {AI_BRIDGE_SCHEMA}.sessions
                    UNION ALL
                    SELECT
                        'users' AS dataset,
                        COUNT(*)::bigint AS rows,
                        MIN(created_at)::text AS first_at,
                        MAX(created_at)::text AS last_at,
                        MIN(user_id)::bigint AS first_id,
                        MAX(user_id)::bigint AS last_id
                    FROM {AI_BRIDGE_SCHEMA}.users
                    UNION ALL
                    SELECT
                        'trained_memories' AS dataset,
                        COUNT(*)::bigint AS rows,
                        MIN(created_at)::text AS first_at,
                        MAX(created_at)::text AS last_at,
                        MIN(trained_memory_id)::bigint AS first_id,
                        MAX(trained_memory_id)::bigint AS last_id
                    FROM {AI_BRIDGE_SCHEMA}.trained_memories
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc), "datasets": []}

    datasets: list[dict[str, Any]] = []
    for dataset, row_count, first_at, last_at, first_id, last_id in rows:
        first_dt = _parse_ts(first_at)
        last_dt = _parse_ts(last_at)
        datasets.append(
            {
                "dataset": str(dataset),
                "rows": int(row_count),
                "first_at": _iso(first_dt),
                "last_at": _iso(last_dt),
                "first_id": int(first_id) if first_id is not None else None,
                "last_id": int(last_id) if last_id is not None else None,
                "retention_days": round((last_dt - first_dt).total_seconds() / 86400, 3) if first_dt and last_dt else 0.0,
            }
        )
    return {"configured": True, "reachable": True, "datasets": datasets}


def _analytics_dimensions() -> list[str]:
    return [
        "created_at",
        "updated_at",
        "session_id",
        "agent_id",
        "task_type",
        "required_capability",
        "provider",
        "model_name",
        "memory_type",
        "source",
        "status",
        "outcome",
        "error_type",
        "latency_ms",
        "cost",
        "token_usage",
        "priority",
        "business_impact",
    ]


def _management_summary(report: DataStorageAnalyticsReport) -> dict[str, Any]:
    earliest: datetime | None = None
    latest: datetime | None = None
    total_rows = 0
    for dataset in report.datasets:
        total_rows += dataset.rows
        first_at = _parse_ts(dataset.created_range.first_at)
        last_at = _parse_ts(dataset.created_range.last_at)
        if first_at is not None and (earliest is None or first_at < earliest):
            earliest = first_at
        if last_at is not None and (latest is None or last_at > latest):
            latest = last_at

    return {
        "total_rows": total_rows,
        "earliest_data_at": _iso(earliest),
        "latest_data_at": _iso(latest),
        "retention_days": round((latest - earliest).total_seconds() / 86400, 3) if earliest and latest else 0.0,
        "storage_health": "healthy" if total_rows > 0 else "empty",
        "analytics_ready": bool(total_rows > 0 and report.total_size_bytes > 0),
    }


def _build_audit_and_signals(report: DataStorageAnalyticsReport) -> tuple[dict[str, Any], dict[str, Any]]:
    now = datetime.now(UTC)
    summary = report.management_summary
    latest = _parse_ts(summary.get("latest_data_at"))
    total_rows = int(summary.get("total_rows", 0) or 0)
    latest_age_days: float | None = None
    future_skew_seconds = 0.0
    if latest is not None:
        delta_seconds = (now - latest).total_seconds()
        latest_age_days = round(delta_seconds / 86400, 3)
        if delta_seconds < 0:
            future_skew_seconds = round(abs(delta_seconds), 3)

    if latest is None:
        freshness_status = "empty"
    elif future_skew_seconds > 300:
        freshness_status = "future_skew"
    elif latest_age_days is not None and latest_age_days <= 2:
        freshness_status = "healthy"
    elif latest_age_days is not None and latest_age_days <= 7:
        freshness_status = "warning"
    else:
        freshness_status = "stale"

    retention_days = float(summary.get("retention_days", 0.0) or 0.0)
    retention_status = "healthy" if retention_days >= 30 else "short"
    session_coverage_status = "healthy" if report.sessions_tracked >= 100 else "limited" if report.sessions_tracked > 0 else "empty"

    search_readiness = "low"
    retrieval_readiness = "low"
    orchestrator_confidence = "low"
    if total_rows > 0 and freshness_status in {"healthy", "warning"}:
        search_readiness = "medium"
        retrieval_readiness = "limited"
        orchestrator_confidence = "medium"
    if total_rows >= 1000 and report.sessions_tracked >= 25 and freshness_status == "healthy":
        search_readiness = "high"
        retrieval_readiness = "high" if retention_days >= 14 else "medium"
        orchestrator_confidence = "high" if report.source == "postgres" or report.total_size_bytes > 0 else "medium"

    issues: list[str] = []
    if total_rows <= 0:
        issues.append("no_rows_detected")
    if freshness_status == "stale":
        issues.append("stale_data_window")
    if freshness_status == "future_skew":
        issues.append("future_dated_timestamps")
    if retention_status == "short":
        issues.append("short_retention_window")
    if session_coverage_status != "healthy":
        issues.append("limited_session_coverage")
    analytics = report.postgres.get("analytics", {}) if isinstance(report.postgres, dict) else {}
    if report.database_url_configured and isinstance(analytics, dict) and not analytics.get("reachable", False):
        issues.append("postgres_unreachable")

    status_score_map = {"empty": 0.0, "future_skew": 0.0, "stale": 0.15, "warning": 0.65, "healthy": 1.0, "short": 0.35, "limited": 0.45}
    readiness_score_map = {"low": 0.2, "limited": 0.45, "medium": 0.7, "high": 1.0}
    signal_score = round(
        (
            status_score_map.get(freshness_status, 0.0) * 0.4
            + status_score_map.get(retention_status, 0.0) * 0.15
            + status_score_map.get(session_coverage_status, 0.0) * 0.15
            + readiness_score_map.get(search_readiness, 0.2) * 0.1
            + readiness_score_map.get(retrieval_readiness, 0.2) * 0.1
            + readiness_score_map.get(orchestrator_confidence, 0.2) * 0.1
        ),
        3,
    )
    degraded_reasons = [
        issue
        for issue in issues
        if issue not in {"limited_session_coverage", "short_retention_window"}
    ]
    if freshness_status in {"warning", "healthy"} and retrieval_readiness in {"medium", "high"} and signal_score >= 0.7:
        policy_mode = "full"
    elif freshness_status in {"empty", "future_skew", "stale"} or signal_score < 0.45:
        policy_mode = "degraded"
    else:
        policy_mode = "cautious"

    routing_policy = {
        "policy_mode": policy_mode,
        "memory_retrieval_mode": "full" if retrieval_readiness == "high" else "limited" if retrieval_readiness in {"limited", "medium"} else "disabled",
        "search_enabled": search_readiness in {"medium", "high"},
        "retrieval_enabled": retrieval_readiness in {"medium", "high"},
        "prefer_fresh_context": freshness_status != "healthy",
        "require_timestamp_audit": freshness_status in {"future_skew", "stale", "empty"},
        "degraded_reasons": degraded_reasons,
    }

    only_soft_issues = {"short_retention_window", "limited_session_coverage"}
    audit_ok = not issues or set(issues).issubset(only_soft_issues)
    audit = {
        "ok": audit_ok,
        "checked_at": report.generated_at,
        "storage_health": summary.get("storage_health", "unknown"),
        "earliest_data_at": summary.get("earliest_data_at"),
        "latest_data_at": summary.get("latest_data_at"),
        "latest_data_age_days": latest_age_days,
        "future_skew_seconds": future_skew_seconds,
        "issues": issues,
    }
    signals = {
        "freshness_status": freshness_status,
        "retention_status": retention_status,
        "session_coverage_status": session_coverage_status,
        "search_readiness": search_readiness,
        "retrieval_readiness": retrieval_readiness,
        "orchestrator_confidence": orchestrator_confidence,
        "analytics_ready": bool(summary.get("analytics_ready")),
        "analytics_signal_score": signal_score,
        "routing_policy": routing_policy,
    }
    return audit, signals


def _recommendations(report: DataStorageAnalyticsReport) -> tuple[list[str], list[str]]:
    recommendations: list[str] = []
    priorities: list[str] = []

    dataset_map = {item.name: item for item in report.datasets}
    memories = dataset_map.get("memories")
    trained = dataset_map.get("trained_memories")

    if memories and memories.rows > 0:
        priorities.append("Collect and track `memory_type`, `agent_id`, and session-level date trends first because memories are the dominant operational dataset.")
    if memories and memories.created_range.retention_days < 30:
        recommendations.append("Retention window is under 30 days. Add long-horizon archival for memory records before expanding downstream analytics.")
    if trained and trained.rows > 0 and trained.payload_size_bytes == 0:
        recommendations.append("Trained memory index exists without payload files. Verify whether trained memory content is intentionally index-only or missing from persistence.")
    analytics = report.postgres.get("analytics", {}) if isinstance(report.postgres, dict) else {}
    if report.database_url_configured and isinstance(analytics, dict) and not analytics.get("reachable"):
        recommendations.append("Primary PostgreSQL analytics path is configured but unreachable. Restore DSN connectivity so reports cover the system-of-record instead of fallback files.")
    if report.sessions_tracked <= 100:
        recommendations.append("Session coverage is still low. Prioritize per-session lifecycle metrics and data completeness checks before building heavier forecasting models.")
    freshness = str(report.operational_signals.get("freshness_status") or "unknown")
    if freshness in {"stale", "future_skew", "empty"}:
        recommendations.append("Data freshness is degraded. Trigger a storage audit, verify timestamp integrity, and lower confidence in retrieval-driven orchestration until ingestion resumes.")
    retrieval = str(report.operational_signals.get("retrieval_readiness") or "low")
    if retrieval in {"low", "limited"}:
        recommendations.append("Retrieval readiness is limited. Expand session metadata, source labels, and event outcome markers before relying on memory search for critical routing.")

    priorities.extend(
        [
            "Store a daily analytics snapshot and compare first/last timestamps, row growth, and size growth to detect ingestion gaps.",
            "Use analytics outputs as routing signals: if freshness drops or row growth stalls, lower confidence in data-driven decisions and trigger diagnostics.",
            "Publish analytics state into the runtime WS stream so orchestrator policy, diagnostics, and operators consume one normalized signal instead of ad hoc logs.",
            "Expand collection in this order: date fields, source/session identifiers, event type, status/outcome, latency/cost, then business impact tags.",
            "Use per-session recency, memory-type density, and agent participation as ranking features for search and retrieval quality.",
            "Audit future-dated or missing timestamps on every refresh because date integrity directly affects retention, freshness scoring, and orchestrator trust.",
        ]
    )
    return recommendations, priorities


def build_data_storage_analytics_report(
    *,
    database_url: str | None = None,
    storage_dir: Path | None = None,
) -> DataStorageAnalyticsReport:
    target_dir = storage_dir or _storage_dir()
    configured_database_url = (database_url if database_url is not None else os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "")).strip()
    datasets, sessions_tracked, total_files, total_size = _collect_file_backed_analytics(target_dir)
    postgres_snapshot = snapshot_postgres_data_plane(configured_database_url).as_dict() if configured_database_url else {
        "ok": False,
        "postgres_state": "missing",
        "tables": [],
        "details": "database url not configured",
    }
    postgres_analytics = _collect_postgres_analytics(configured_database_url)
    report = DataStorageAnalyticsReport(
        generated_at=datetime.now(UTC).isoformat(),
        source="postgres" if postgres_analytics.get("reachable") else "file_fallback",
        database_url_configured=bool(configured_database_url),
        storage_root=str(target_dir),
        total_size_bytes=total_size,
        total_size_mb=round(total_size / 1024 / 1024, 2),
        total_files=total_files,
        sessions_tracked=sessions_tracked,
        datasets=datasets,
        postgres={
            "snapshot": postgres_snapshot,
            "analytics": postgres_analytics,
        },
    )
    report.management_summary = _management_summary(report)
    report.audit, report.operational_signals = _build_audit_and_signals(report)
    report.analytics_dimensions = _analytics_dimensions()
    report.recommendations, report.priorities = _recommendations(report)
    return report


def write_data_storage_analytics_report(
    report: DataStorageAnalyticsReport,
    *,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.as_dict(), ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
