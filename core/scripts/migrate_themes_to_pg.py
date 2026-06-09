import json
import os
import psycopg2
from psycopg2.extras import Json
from pathlib import Path

# Load environment variables (mimicking orchestrator)
# Assuming run from root of wisper
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url, ensure_storage_schema

def migrate():
    themes_path = Path("memory_store/themes.json")
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    
    if not themes_path.exists():
        print("No themes.json found.")
        return
        
    if not database_url:
        print("No database URL provided.")
        return
        
    # Ensure schema exists
    if not ensure_storage_schema(database_url):
        print("Failed to initialize database schema.")
        return
        
    print(f"Reading themes from {themes_path}...")
    with open(themes_path, "r", encoding="utf-8") as f:
        events = json.load(f)
        
    dsn = normalize_database_url(database_url)
    
    print(f"Connecting to {dsn}...")
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            for event in events:
                cur.execute(
                    f"""
                    INSERT INTO {AI_BRIDGE_SCHEMA}.json_themes (
                        task_id, session_id, agent_id, provider, color, status, event_payload, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(event.get("task_id") or "unknown"),
                        str(event.get("session_id") or "default"),
                        event.get("agent_id"),
                        event.get("provider"),
                        event.get("color"),
                        event.get("status"),
                        Json(event),
                        event.get("timestamp"),
                    ),
                )
    print(f"Migrated {len(events)} events to PostgreSQL.")
    
    # Optionally rename/remove the old file
    themes_path.rename(themes_path.with_suffix(".json.bak"))
    print("Renamed themes.json to themes.json.bak")

if __name__ == "__main__":
    migrate()
