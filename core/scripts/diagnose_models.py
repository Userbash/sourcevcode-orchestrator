import json
from core.core.orchestrator import Orchestrator


def diagnose():
    orch = Orchestrator()
    usage_mod = orch.get_module("model_usage")
    local_model_manager = orch.get_module("local_model_manager")

    print("--- AI Model Health & Residency Report ---")

    print("\n[Health Check]:")
    health_data = orch.availability.check_all()
    for provider, health in health_data.items():
        print(f"Provider: {provider}, Status: {health.status.value}, Latency: {health.latency_ms:.2f}ms")
        if health.error:
            print(f"  Error: {health.error}")

    if usage_mod:
        print("\n[Usage Intensity]:")
        print(json.dumps(usage_mod.get_statistics(), indent=2))

    if local_model_manager and hasattr(local_model_manager, "finalize"):
        local_state = local_model_manager.finalize()
        print("\n[Local Model Residency]:")
        print(json.dumps({
            "resident_models": local_state.get("resident_models", []),
            "blocked_models": local_state.get("blocked_models", []),
            "memory_pressure": local_state.get("memory_pressure", {}),
            "warmups": local_state.get("warmups", 0),
            "evictions": local_state.get("evictions", 0),
        }, indent=2))
    else:
        print("\n[Local Model Residency]: local_model_manager is not loaded.")


if __name__ == "__main__":
    diagnose()
