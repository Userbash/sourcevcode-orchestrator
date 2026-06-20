import json
from core.core.orchestrator import Orchestrator


def get_full_report():
    orch = Orchestrator()
    usage_mod = orch.get_module("model_usage")
    local_model_manager = orch.get_module("local_model_manager")
    availability = orch.availability

    health_raw = availability.check_all()
    health_report = {name: h.as_dict() for name, h in health_raw.items()}
    usage_report = usage_mod.get_statistics() if usage_mod else {"models": {}}
    local_report = local_model_manager.finalize() if local_model_manager and hasattr(local_model_manager, "finalize") else {}

    full_report = {
        "timestamp": usage_mod._api.get_context("now") if usage_mod and usage_mod._api else "now",
        "models_status": health_report,
        "usage_stats": usage_report,
        "local_models": local_report,
    }

    print(json.dumps(full_report, indent=2))


if __name__ == "__main__":
    get_full_report()
