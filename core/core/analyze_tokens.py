import json
from collections import defaultdict
from pathlib import Path

# Путь к логам KPI относительно корня проекта
log_path = Path(__file__).resolve().parents[2] / "memory_store" / "kpi_events.jsonl"

totals = defaultdict(int)

if log_path.exists():
    with open(log_path, "r") as f:
        for line in f:
            try:
                record = json.loads(line)
                provider = record.get("provider", "unknown")
                tokens = record.get("tokens_used", 0)
                
                # Normalize names
                if provider in ["antigravity", "antigravity-cli", "agy"]:
                    provider = "antigravity"
                elif provider in ["openai", "codex"]:
                    provider = "openai"
                    
                totals[provider] += tokens
            except:
                continue

    print(dict(totals))
else:
    print(f"Error: Log file not found at {log_path}")
