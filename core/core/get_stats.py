from core.core.orchestrator import Orchestrator
import json

orch = Orchestrator()
stats = orch.module_manager.get_module("model_usage").get_statistics()
print(json.dumps(stats, indent=2))
