import pytest
import os
import psycopg2
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url
from core.core.unified_vfs import UnifiedVFSModule, StateIntegrity
import json

@pytest.fixture
def db_conn():
    dsn = normalize_database_url(os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", ""))
    from core.core.persistent_memory import ensure_storage_schema
    ensure_storage_schema(os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", ""))
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()

def test_schema_extension(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {AI_BRIDGE_SCHEMA}.task_plans")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {AI_BRIDGE_SCHEMA}.agent_performance_metrics")
        assert cur.fetchone()[0] == 0

def test_vfs_integrity_failure():
    # This requires an async loop as UnifiedVFSModule methods are async now
    import asyncio
    
    async def run_test():
        vfs = UnifiedVFSModule()
        await vfs.on_load(None) # type: ignore
        
        path = "test/corrupt"
        content = {"data": "test"}
        await vfs.write_state(path, content, "test-agent")
        
        # Manually corrupt the DB entry
        dsn = normalize_database_url(os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", ""))
        import asyncpg
        conn = await asyncpg.connect(dsn)
        await conn.execute(f"UPDATE {AI_BRIDGE_SCHEMA}.vfs_files SET content = 'corrupted'::bytea WHERE file_path = $1", path)
        await conn.close()
            
        # Re-read
        node = await vfs.read_state(path)
        # Note: the current read_state returns None on failure, 
        # need to verify it handles integrity and logs it
        assert node is None

    asyncio.run(run_test())

def test_relevance_retrieval(db_conn):
    # Test that memories are retrieved by importance
    pass
