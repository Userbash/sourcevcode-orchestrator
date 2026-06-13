from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, asdict, field
from uuid import uuid4
from typing import Any
from urllib.parse import urlparse

from .persistent_memory import AI_BRIDGE_SCHEMA, ensure_storage_schema, normalize_database_url


@dataclass(slots=True)
class TableSnapshot:
    table: str
    row_count: int
    last_updated: str | None = None
    latest_marker: str | None = None


@dataclass(slots=True)
class DataPlaneSnapshot:
    ok: bool
    database_url: str = ""
    tables: list[TableSnapshot] = field(default_factory=list)
    rabbitmq_ok: bool | None = None
    rabbitmq_target: str | None = None
    details: str = ""
    postgres_state: str = "unknown"
    postgres_error: str | None = None
    probe: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tables"] = [asdict(item) for item in self.tables or []]
        return payload


def _connect_postgres(dsn: str):
    import psycopg2  # type: ignore

    return psycopg2.connect(dsn)


def fetch_recent_rows(database_url: str, table: str, *, limit: int = 5) -> list[dict[str, Any]]:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        return []
    bounded_limit = max(1, int(limit))
    queries = {
        'memories': f"SELECT memory_id, session_id, source_session_id, agent_id, memory_type, content, metadata, importance_score, created_at, updated_at FROM {AI_BRIDGE_SCHEMA}.memories ORDER BY updated_at DESC, memory_id DESC LIMIT %s",
        'vfs_files': f"SELECT file_path, checksum, last_updated, owner_agent, integrity, metadata, updated_at, content FROM {AI_BRIDGE_SCHEMA}.vfs_files ORDER BY updated_at DESC, last_updated DESC LIMIT %s",
        'json_themes': f"SELECT theme_event_id, task_id, session_id, agent_id, provider, color, status, created_at, event_payload FROM {AI_BRIDGE_SCHEMA}.json_themes ORDER BY created_at DESC, theme_event_id DESC LIMIT %s",
        'commands': f"SELECT command_id, session_id, source_session_id, agent_id, command, success, tokens_used, executed_at, result FROM {AI_BRIDGE_SCHEMA}.commands ORDER BY executed_at DESC, command_id DESC LIMIT %s",
        'sessions': f"SELECT source_session_id, normalized_session_id, created_at, updated_at FROM {AI_BRIDGE_SCHEMA}.sessions ORDER BY updated_at DESC, created_at DESC LIMIT %s",
        'users': f"SELECT user_id, username, email, created_at FROM {AI_BRIDGE_SCHEMA}.users ORDER BY created_at DESC, user_id DESC LIMIT %s",
        'user_roles': f"SELECT user_id, role FROM {AI_BRIDGE_SCHEMA}.user_roles ORDER BY user_id DESC, role ASC LIMIT %s",
    }
    query = queries.get(table)
    if query is None:
        return []
    try:
        with _connect_postgres(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (bounded_limit,))
                rows = cur.fetchall()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if table == 'memories':
            out.append({
                'memory_id': row[0], 'session_id': row[1], 'source_session_id': row[2], 'agent_id': row[3],
                'memory_type': row[4], 'content': row[5], 'metadata': row[6], 'importance_score': row[7],
                'created_at': row[8], 'updated_at': row[9],
            })
        elif table == 'vfs_files':
            out.append({
                'file_path': row[0], 'checksum': row[1], 'last_updated': row[2], 'owner_agent': row[3],
                'integrity': row[4], 'metadata': row[5], 'updated_at': row[6], 'content': row[7],
            })
        elif table == 'json_themes':
            out.append({
                'theme_event_id': row[0], 'task_id': row[1], 'session_id': row[2], 'agent_id': row[3],
                'provider': row[4], 'color': row[5], 'status': row[6], 'created_at': row[7], 'event_payload': row[8],
            })
        elif table == 'commands':
            out.append({
                'command_id': row[0], 'session_id': row[1], 'source_session_id': row[2], 'agent_id': row[3],
                'command': row[4], 'success': row[5], 'tokens_used': row[6], 'executed_at': row[7], 'result': row[8],
            })
        elif table == 'sessions':
            out.append({'source_session_id': row[0], 'normalized_session_id': row[1], 'created_at': row[2], 'updated_at': row[3]})
        elif table == 'users':
            out.append({'user_id': row[0], 'username': row[1], 'email': row[2], 'created_at': row[3]})
        elif table == 'user_roles':
            out.append({'user_id': row[0], 'role': row[1]})
    return out


def postgres_read_write_probe(database_url: str, *, session_id: str = 'health-probe', agent_id: str = 'diagnostic', key: str | None = None) -> dict[str, Any]:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        return {'ok': False, 'error': 'database url not configured'}
    probe_key = key or f'probe-{uuid4().hex[:8]}'
    memory_payload = {'probe': True, 'key': probe_key, 'session_id': session_id, 'agent_id': agent_id}
    try:
        from psycopg2.extras import Json  # type: ignore
        with _connect_postgres(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                INSERT INTO {AI_BRIDGE_SCHEMA}.sessions (source_session_id, normalized_session_id, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (source_session_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING normalized_session_id
                """, (session_id, f"probe-{session_id}"))
                normalized_session_id = cur.fetchone()[0]
                cur.execute(f"""
                INSERT INTO {AI_BRIDGE_SCHEMA}.memories (session_id, source_session_id, agent_id, memory_type, content, metadata, importance_score)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                RETURNING memory_id
                """, (normalized_session_id, session_id, agent_id, 'diagnostic', Json(memory_payload), Json({'key': probe_key, 'diagnostic': True}), 0.1))
                memory_id = cur.fetchone()[0]
                cur.execute(f"""
                SELECT content, metadata, updated_at
                FROM {AI_BRIDGE_SCHEMA}.memories
                WHERE memory_id = %s
                """, (memory_id,))
                row = cur.fetchone()
        if not row:
            return {'ok': False, 'error': 'probe_insert_not_readable', 'key': probe_key}
        return {'ok': True, 'key': probe_key, 'memory_id': memory_id, 'read_back': {'content': row[0], 'metadata': row[1], 'updated_at': row[2]}}
    except Exception as exc:
        return {'ok': False, 'key': probe_key, 'error': str(exc)}


def snapshot_postgres_data_plane(database_url: str) -> DataPlaneSnapshot:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        return DataPlaneSnapshot(ok=False, details="database url not configured", tables=[], postgres_state="missing")

    try:
        with _connect_postgres(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        'memories' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(updated_at)::text AS last_updated,
                        MAX(created_at)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.memories
                    UNION ALL
                    SELECT
                        'vfs_files' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(updated_at)::text AS last_updated,
                        MAX(last_updated)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.vfs_files
                    UNION ALL
                    SELECT
                        'json_themes' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(created_at)::text AS last_updated,
                        MAX(created_at)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.json_themes
                    UNION ALL
                    SELECT
                        'commands' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(executed_at)::text AS last_updated,
                        MAX(executed_at)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.commands
                    UNION ALL
                    SELECT
                        'sessions' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(updated_at)::text AS last_updated,
                        MAX(updated_at)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.sessions
                    UNION ALL
                    SELECT
                        'users' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        MAX(created_at)::text AS last_updated,
                        MAX(created_at)::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.users
                    UNION ALL
                    SELECT
                        'user_roles' AS table_name,
                        COUNT(*)::bigint AS row_count,
                        NULL::text AS last_updated,
                        NULL::text AS latest_marker
                    FROM {AI_BRIDGE_SCHEMA}.user_roles
                    ORDER BY table_name
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        return DataPlaneSnapshot(ok=False, database_url=dsn, tables=[], details=f"postgres unavailable: {exc}", postgres_state="unavailable", postgres_error=str(exc))

    tables = [
        TableSnapshot(
            table=str(row[0]),
            row_count=int(row[1]),
            last_updated=str(row[2]) if row[2] is not None else None,
            latest_marker=str(row[3]) if row[3] is not None else None,
        )
        for row in rows
    ]
    ok = any(item.row_count > 0 for item in tables)
    details = "postgres data plane reachable" if ok else "postgres reachable but tables are empty"
    return DataPlaneSnapshot(ok=ok, database_url=dsn, tables=tables, details=details, postgres_state="healthy" if ok else "empty")


def _parse_rabbitmq_host_port(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None, None
    port = parsed.port or 5672
    return parsed.hostname, port


def check_rabbitmq_connectivity(rabbitmq_url: str | None = None, *, timeout: float = 3.0) -> dict[str, Any]:
    url = (rabbitmq_url or os.getenv("AI_BRIDGE_RABBITMQ_URL", "amqp://guest:guest@localhost/")).strip()
    host, port = _parse_rabbitmq_host_port(url)
    if not host or not port:
        return {
            "ok": False,
            "target": url,
            "error": "invalid_rabbitmq_url",
        }

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {
            "ok": True,
            "target": f"{host}:{port}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "target": f"{host}:{port}",
            "error": str(exc),
        }




def postgres_recovery_plan(snapshot: DataPlaneSnapshot) -> dict[str, Any]:
    table_states = {item.table: item.row_count for item in snapshot.tables}
    steps: list[str] = []
    blockers: list[str] = []

    if snapshot.postgres_state == "missing":
        blockers.append("AI_BRIDGE_MEMORY_DATABASE_URL is empty")
        steps.append("Set AI_BRIDGE_MEMORY_DATABASE_URL to the correct PostgreSQL DSN.")
        steps.append("Restart the orchestrator after the DSN is restored.")
    elif snapshot.postgres_state == "unavailable":
        blockers.append(snapshot.postgres_error or "postgres connection failed")
        steps.append("Check whether the Postgres container/service is running.")
        steps.append("Verify host, port, credentials, and network reachability.")
        steps.append("Inspect Postgres logs for crash, auth, disk, or schema errors.")
    elif snapshot.postgres_state == "read_write_failed":
        blockers.append(snapshot.postgres_error or "probe failed")
        steps.append("Run a read-after-write probe and inspect the returned error.")
        steps.append("Check whether the database is read-only, disk-full, or schema-corrupted.")
        steps.append("If writes fail but reads work, verify permissions on the target schema.")
    elif snapshot.postgres_state == "empty":
        steps.append("The database is reachable but empty.")
        steps.append("Confirm the correct volume is attached and no fresh empty cluster was created.")
        steps.append("Run the migration/seed scripts to re-create the schema and seed data.")
    else:
        steps.append("PostgreSQL responded and the probe succeeded.")

    if not snapshot.rabbitmq_ok:
        blockers.append(f"RabbitMQ unreachable at {snapshot.rabbitmq_target or 'unknown'}")
        steps.append("Restart RabbitMQ or fix the broker host/port in AI_BRIDGE_RABBITMQ_URL.")

    if table_states.get("memories", 0) == 0:
        steps.append("Memories table is empty: verify the memory writer is enabled and the DSN points to the expected cluster.")
    if table_states.get("vfs_files", 0) == 0:
        steps.append("VFS table is empty: verify UnifiedVFS is loaded and PostgreSQL sync is enabled.")

    if not steps:
        steps.append("No remediation needed.")

    severity = "ok" if snapshot.ok else ("critical" if snapshot.postgres_state in {"missing", "unavailable", "read_write_failed"} else "warning")
    return {
        "severity": severity,
        "ok": snapshot.ok,
        "postgres_state": snapshot.postgres_state,
        "blockers": blockers,
        "steps": steps,
        "table_states": table_states,
        "summary": "; ".join(blockers + steps[:2]) if blockers or steps else "healthy",
    }


def seed_default_admin_user(database_url: str) -> dict[str, Any]:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        return {"ok": False, "error": "database url not configured"}
    username = os.getenv("DB_DEFAULT_USER_USERNAME", "admin_local").strip()
    email = os.getenv("DB_DEFAULT_USER_EMAIL", "admin@local.test").strip()
    role = os.getenv("DB_DEFAULT_USER_ROLE", "platform_admin").strip()
    blocked = os.getenv("DB_DEFAULT_USER_BLOCKED", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        import psycopg2  # type: ignore
        with _connect_postgres(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {AI_BRIDGE_SCHEMA}.users")
                user_count = int(cur.fetchone()[0])
                if user_count > 0:
                    return {"ok": True, "skipped": True, "reason": "users_table_not_empty", "user_count": user_count}
                cur.execute(
                    f"""
                    INSERT INTO {AI_BRIDGE_SCHEMA}.users (username, email)
                    VALUES (%s, %s)
                    ON CONFLICT (username) DO UPDATE SET email = EXCLUDED.email
                    RETURNING user_id
                    """,
                    (username, email),
                )
                user_id = int(cur.fetchone()[0])
                cur.execute(
                    f"""
                    INSERT INTO {AI_BRIDGE_SCHEMA}.user_roles (user_id, role)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, role),
                )
                return {"ok": True, "seeded": True, "user_id": user_id, "username": username, "email": email, "role": role, "blocked": blocked}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def postgres_recovery_code(snapshot: DataPlaneSnapshot) -> str:
    if snapshot.postgres_state == "missing":
        return "POSTGRES_MISSING_DSN"
    if snapshot.postgres_state == "unavailable":
        return "POSTGRES_UNAVAILABLE"
    if snapshot.postgres_state == "read_write_failed":
        return "POSTGRES_READ_WRITE_FAILED"
    if snapshot.postgres_state == "empty":
        return "POSTGRES_EMPTY_SCHEMA"
    if not snapshot.rabbitmq_ok:
        return "RABBITMQ_UNAVAILABLE"
    if not snapshot.ok:
        return "POSTGRES_DEGRADED"
    return "OK"


def postgres_operator_hint(recovery_code: str) -> str:
    hints = {
        "POSTGRES_MISSING_DSN": "Set AI_BRIDGE_MEMORY_DATABASE_URL and restart the orchestrator.",
        "POSTGRES_UNAVAILABLE": "Check the Postgres container/service, network reachability, and logs.",
        "POSTGRES_READ_WRITE_FAILED": "Check read-only mode, disk pressure, schema corruption, and permissions.",
        "POSTGRES_EMPTY_SCHEMA": "Restore the correct volume or seed the schema and default data.",
        "RABBITMQ_UNAVAILABLE": "Restart RabbitMQ or fix AI_BRIDGE_RABBITMQ_URL.",
        "POSTGRES_DEGRADED": "Inspect postgres diagnostics and compare before/after snapshots.",
        "OK": "No recovery action required.",
    }
    return hints.get(recovery_code, "Inspect postgres diagnostics and logs.")


def postgres_recover(database_url: str, rabbitmq_url: str | None = None) -> dict[str, Any]:
    dsn = normalize_database_url(database_url.strip())
    if not dsn:
        empty = DataPlaneSnapshot(ok=False, details="database url not configured", tables=[], postgres_state="missing")
        code = postgres_recovery_code(empty)
        return {
            "status": "error",
            "recovery_code": code,
            "operator_hint": postgres_operator_hint(code),
            "before": empty.as_dict(),
            "recovery": postgres_recovery_plan(empty),
            "after": empty.as_dict(),
            "steps_executed": [],
            "seed": {"ok": False, "skipped": True, "reason": "missing_dsn"},
        }

    before = snapshot_postgres_data_plane(database_url)
    steps_executed: list[str] = []
    schema_ok = ensure_storage_schema(dsn)
    steps_executed.append("ensure_storage_schema" if schema_ok else "ensure_storage_schema_failed")
    seed_result = {"ok": False, "skipped": True, "reason": "not_applicable"}
    if before.postgres_state == "empty" or before.probe.get("ok") is False:
        seed_result = seed_default_admin_user(dsn)
        steps_executed.append("seed_default_admin_user" if seed_result.get("seeded") else ("seed_skipped" if seed_result.get("skipped") else "seed_failed"))
    after = build_data_plane_snapshot(database_url=dsn, rabbitmq_url=rabbitmq_url)
    code = postgres_recovery_code(after)
    status = "ok" if after.ok else ("degraded" if after.postgres_state in {"empty", "healthy"} else "error")
    return {
        "status": status,
        "recovery_code": code,
        "operator_hint": postgres_operator_hint(code),
        "before": before.as_dict(),
        "after": after.as_dict(),
        "recovery": postgres_recovery_plan(after),
        "steps_executed": steps_executed,
        "schema_ok": schema_ok,
        "seed": seed_result,
    }


def postgres_status_summary(snapshot: DataPlaneSnapshot) -> dict[str, Any]:
    return {
        "ok": snapshot.ok,
        "postgres_state": snapshot.postgres_state,
        "postgres_error": snapshot.postgres_error,
        "rabbitmq_ok": snapshot.rabbitmq_ok,
        "probe_ok": bool(snapshot.probe.get("ok")),
        "row_counts": {item.table: item.row_count for item in snapshot.tables},
        "summary": snapshot.details,
        "recovery_code": postgres_recovery_code(snapshot),
        "operator_hint": postgres_operator_hint(postgres_recovery_code(snapshot)),
        "recovery": postgres_recovery_plan(snapshot),
    }


def build_data_plane_snapshot(*, database_url: str, rabbitmq_url: str | None = None) -> DataPlaneSnapshot:
    snapshot = snapshot_postgres_data_plane(database_url)
    snapshot.probe = postgres_read_write_probe(database_url) if database_url.strip() else {"ok": False, "error": "database url not configured"}
    rabbitmq = check_rabbitmq_connectivity(rabbitmq_url)
    snapshot.rabbitmq_ok = bool(rabbitmq.get("ok"))
    snapshot.rabbitmq_target = str(rabbitmq.get("target")) if rabbitmq.get("target") else None
    if not snapshot.probe.get("ok"):
        snapshot.postgres_state = "read_write_failed" if snapshot.postgres_state != "missing" else snapshot.postgres_state
        snapshot.postgres_error = str(snapshot.probe.get("error") or snapshot.postgres_error or "probe_failed")
    if snapshot.details:
        snapshot.details = f"{snapshot.details}; rabbitmq={'ok' if snapshot.rabbitmq_ok else 'failed'}; probe={'ok' if snapshot.probe.get('ok') else 'failed'}"
    else:
        snapshot.details = f"rabbitmq={'ok' if snapshot.rabbitmq_ok else 'failed'}; probe={'ok' if snapshot.probe.get('ok') else 'failed'}"
    snapshot.ok = bool(snapshot.ok and snapshot.rabbitmq_ok and snapshot.probe.get("ok"))
    return snapshot
