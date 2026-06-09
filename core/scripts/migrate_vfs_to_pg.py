import json
import os
import psycopg2
from psycopg2.extras import Json
from pathlib import Path
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url, ensure_storage_schema

def migrate():
    vfs_dir = Path("memory_store/vfs")
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    
    if not vfs_dir.exists():
        print("No VFS directory found.")
        return
        
    if not database_url:
        print("No database URL provided.")
        return
        
    dsn = normalize_database_url(database_url)
    
    # Ensure schema exists (drop if structure is old)
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {AI_BRIDGE_SCHEMA}.vfs_files")
            
    if not ensure_storage_schema(database_url):
        print("Failed to initialize database schema.")
        return
        
    print(f"Connecting to {dsn}...")
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            for p in vfs_dir.rglob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    relative_path = data["path"]
                    content = json.dumps(data["content"]).encode("utf-8")
                    
                    cur.execute(
                        f"""
                        INSERT INTO {AI_BRIDGE_SCHEMA}.vfs_files (
                            file_path, content, checksum, last_updated, owner_agent, integrity, metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            relative_path, 
                            psycopg2.Binary(content),
                            data["checksum"],
                            data["last_updated"],
                            data["owner_agent"],
                            data.get("integrity", "valid"),
                            Json(data.get("metadata", {}))
                        ),
                    )
                    print(f"Migrated {relative_path}")
                    p.unlink() # Delete after migration
                except Exception as e:
                    print(f"Failed to migrate {p}: {e}")
    
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
