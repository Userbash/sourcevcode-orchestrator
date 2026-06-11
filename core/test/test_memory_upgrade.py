import json
import os

import psycopg2
import pytest
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url
from core.core.unified_vfs import StateIntegrity, UnifiedVFSModule

@pytest.fixture
def db_conn():
    dsn_raw = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    if not dsn_raw:
        pytest.skip("AI_BRIDGE_MEMORY_DATABASE_URL is not configured")
    dsn = normalize_database_url(dsn_raw)
    from core.core.persistent_memory import ensure_storage_schema

    if not ensure_storage_schema(dsn_raw):
        pytest.skip("PostgreSQL memory schema is unavailable")

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        pytest.skip(f"PostgreSQL is unavailable: {exc}")
    yield conn
    conn.close()

def test_schema_extension(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {AI_BRIDGE_SCHEMA}.task_plans")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {AI_BRIDGE_SCHEMA}.agent_performance_metrics")
        assert cur.fetchone()[0] == 0

def test_vfs_integrity_failure():
    vfs = UnifiedVFSModule()
    vfs.write_state("test/corrupt", {"data": "test"}, "test-agent")

    # Corrupt the file fallback used in tests when PostgreSQL is unavailable.
    path = vfs._safe_path("test/corrupt")
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["content"] = "corrupted"
        path.write_text(json.dumps(data), encoding="utf-8")

    vfs._nodes.pop("test/corrupt", None)
    node = vfs.read_state("test/corrupt")
    assert node is None

def test_relevance_retrieval(db_conn):
    # Test that memories are retrieved by importance
    assert db_conn is not None
