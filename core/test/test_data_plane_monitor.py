from __future__ import annotations

from core.core import data_plane_monitor as dpm
import core.core.core_healthcheck as healthcheck


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj


def test_snapshot_postgres_data_plane(monkeypatch):
    rows = [
        ("commands", 3, "2026-06-12T10:00:00Z", "2026-06-12T10:00:00Z"),
        ("memories", 2, "2026-06-12T09:59:00Z", "2026-06-12T09:59:00Z"),
        ("sessions", 1, "2026-06-12T09:58:00Z", "2026-06-12T09:58:00Z"),
        ("user_roles", 1, None, None),
        ("users", 1, "2026-06-12T09:57:00Z", "2026-06-12T09:57:00Z"),
        ("vfs_files", 5, "2026-06-12T10:01:00Z", "2026-06-12T10:01:00Z"),
        ("json_themes", 4, "2026-06-12T10:02:00Z", "2026-06-12T10:02:00Z"),
    ]

    monkeypatch.setattr(dpm, "_connect_postgres", lambda dsn: _FakeConn(rows))

    snapshot = dpm.snapshot_postgres_data_plane("postgresql://example")

    assert snapshot.ok is True
    assert any(item.table == "memories" and item.row_count == 2 for item in snapshot.tables)
    assert any(item.table == "vfs_files" and item.last_updated == "2026-06-12T10:01:00Z" for item in snapshot.tables)


def test_rabbitmq_connectivity(monkeypatch):
    seen = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_create_connection(addr, timeout):
        seen["addr"] = addr
        seen["timeout"] = timeout
        return _Conn()

    monkeypatch.setattr(dpm.socket, "create_connection", fake_create_connection)

    result = dpm.check_rabbitmq_connectivity("amqp://guest:guest@rabbitmq:5672/")

    assert result["ok"] is True
    assert result["target"] == "rabbitmq:5672"
    assert seen["addr"] == ("rabbitmq", 5672)




def test_postgres_read_write_probe(monkeypatch):
    seen = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            seen.setdefault('queries', []).append((query, params))
            if 'RETURNING normalized_session_id' in query:
                self._row = ('probe-health-probe',)
            elif 'RETURNING memory_id' in query:
                self._row = (777,)
            elif 'SELECT content, metadata, updated_at' in query:
                self._row = ({'probe': True}, {'key': 'probe-1'}, '2026-06-12T10:00:00Z')

        def fetchone(self):
            return getattr(self, '_row', None)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(dpm, '_connect_postgres', lambda dsn: _Conn())

    result = dpm.postgres_read_write_probe('postgresql://example')

    assert result['ok'] is True
    assert result['memory_id'] == 777
    assert result['read_back']['metadata']['key'] == 'probe-1'


def test_run_healthcheck_includes_data_plane(monkeypatch):
    monkeypatch.setattr(healthcheck, "build_data_plane_snapshot", lambda **kwargs: type("S", (), {"ok": True, "tables": [], "rabbitmq_ok": True, "rabbitmq_target": "rabbitmq:5672", "details": "ok"})())
    ok, checks = healthcheck.run_healthcheck()

    data_plane = next(item for item in checks if item.name == "data_plane")
    assert data_plane.ok is True
    assert "rabbitmq=ok" in data_plane.details
    assert ok in {True, False}


def test_postgres_recovery_plan_reports_steps():
    snapshot = dpm.DataPlaneSnapshot(ok=False, postgres_state="unavailable", postgres_error="connection refused", tables=[], details="postgres unavailable")
    recovery = dpm.postgres_recovery_plan(snapshot)

    assert recovery["severity"] == "critical"
    assert recovery["ok"] is False
    assert any("Postgres container/service" in step for step in recovery["steps"])
    assert "connection refused" in recovery["blockers"][0]


def test_postgres_status_summary_includes_recovery():
    snapshot = dpm.DataPlaneSnapshot(ok=False, postgres_state="missing", tables=[], details="database url not configured")
    summary = dpm.postgres_status_summary(snapshot)

    assert summary["postgres_state"] == "missing"
    assert summary["recovery"]["severity"] == "critical"
    assert summary["recovery"]["blockers"]


def test_postgres_recover_runs_schema_ensure_and_recheck(monkeypatch):
    calls = []

    monkeypatch.setattr(dpm, "ensure_storage_schema", lambda dsn: calls.append(("ensure", dsn)) or True)
    monkeypatch.setattr(dpm, "snapshot_postgres_data_plane", lambda database_url: dpm.DataPlaneSnapshot(ok=False, postgres_state="missing", tables=[], details="database url not configured"))
    monkeypatch.setattr(dpm, "build_data_plane_snapshot", lambda **kwargs: dpm.DataPlaneSnapshot(ok=True, postgres_state="healthy", tables=[], rabbitmq_ok=True, probe={"ok": True}, details="postgres data plane reachable; rabbitmq=ok; probe=ok"))

    result = dpm.postgres_recover("postgresql://example", "amqp://guest:guest@rabbitmq:5672/")

    assert result["status"] == "ok"
    assert result["recovery_code"] == "OK"
    assert result["schema_ok"] is True
    assert result["steps_executed"] == ["ensure_storage_schema"]
    assert calls and calls[0][1] == "postgresql://example"


def test_postgres_status_summary_includes_recovery_code():
    snapshot = dpm.DataPlaneSnapshot(ok=False, postgres_state="unavailable", postgres_error="connection refused", tables=[], details="postgres unavailable")
    summary = dpm.postgres_status_summary(snapshot)

    assert summary["recovery_code"] == "POSTGRES_UNAVAILABLE"
    assert summary["recovery"]["severity"] == "critical"


def test_seed_default_admin_user_skips_when_users_exist(monkeypatch):
    class _Cursor:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def execute(self, query, params=None):
            if 'SELECT COUNT(*) FROM' in query:
                self._row = (2,)
        def fetchone(self):
            return getattr(self, '_row', None)
    class _Conn:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def cursor(self):
            return _Cursor()
    monkeypatch.setattr(dpm, '_connect_postgres', lambda dsn: _Conn())

    result = dpm.seed_default_admin_user('postgresql://example')

    assert result['ok'] is True
    assert result['skipped'] is True


def test_postgres_operator_hint_matches_recovery_code():
    assert 'Set AI_BRIDGE_MEMORY_DATABASE_URL' in dpm.postgres_operator_hint('POSTGRES_MISSING_DSN')
    assert 'Restart RabbitMQ' in dpm.postgres_operator_hint('RABBITMQ_UNAVAILABLE')
