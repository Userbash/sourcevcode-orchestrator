import psycopg2
import json
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url
import os

def inspect_db():
    dsn = normalize_database_url(os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", ""))
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Inspect memories
            print("--- MEMORIES SAMPLE (Last 3) ---")
            cur.execute(f"SELECT agent_id, memory_type, content, importance_score FROM {AI_BRIDGE_SCHEMA}.memories ORDER BY memory_id DESC LIMIT 3")
            for row in cur.fetchall():
                print(f"Agent: {row[0]} | Type: {row[1]} | Importance: {row[3]}")
                print(f"Content snippet: {json.dumps(row[2])[:200]}...")
                print("-" * 40)

            # Inspect VFS
            print("\n--- VFS FILES SAMPLE (Last 3) ---")
            cur.execute(f"SELECT file_path, metadata FROM {AI_BRIDGE_SCHEMA}.vfs_files ORDER BY updated_at DESC LIMIT 3")
            for row in cur.fetchall():
                print(f"Path: {row[0]}")
                print(f"Metadata: {row[1]}")
                print("-" * 40)

if __name__ == "__main__":
    inspect_db()
