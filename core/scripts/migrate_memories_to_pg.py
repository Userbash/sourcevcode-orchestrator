import json
import os
import psycopg2
from psycopg2.extras import Json
from pathlib import Path
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url

def migrate():
    memories_dir = Path("memory_store/memories")
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    
    if not database_url:
        print("No database URL provided.")
        return
        
    dsn = normalize_database_url(database_url)
    
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            for p in memories_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    cur.execute(
                        f"""
                        INSERT INTO {AI_BRIDGE_SCHEMA}.memories (
                            session_id, source_session_id, agent_id, memory_type, content, importance_score
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb, 0.5)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            data['session_id'],
                            data['source_session_id'],
                            data['agent_id'],
                            data['type'],
                            Json(data['content']),
                        ),
                    )
                    print(f"Migrated {p.name}")
                    p.unlink() # Delete after successful migration
                except Exception as e:
                    print(f"Failed to migrate {p.name}: {e}")

if __name__ == "__main__":
    migrate()
