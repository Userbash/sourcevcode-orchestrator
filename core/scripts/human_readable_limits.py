from core.core.orchestrator import Orchestrator


def print_report():
    orch = Orchestrator()
    health = orch.availability.check_all()
    usage_mod = orch.get_module("model_usage")
    local_model_manager = orch.get_module("local_model_manager")
    stats = usage_mod.get_statistics() if usage_mod else {"models": {}}
    local_state = local_model_manager.finalize() if local_model_manager and hasattr(local_model_manager, "finalize") else {}

    print("==========================================================")
    print("            AI MODEL STATUS, LIMITS, AND MEMORY           ")
    print("==========================================================\n")

    for provider, data in health.items():
        print(f"Provider: {provider.upper()}")
        status = data.status.value
        print(f"  Status: {status}")

        models = data.diagnostics.get("models", [])
        print(f"  Available models: {len(models)}")
        if models:
            print(f"  Popular: {', '.join(models[:5])}{'...' if len(models) > 5 else ''}")

        print("  Usage:")
        found_usage = False
        for model_name, stat in stats.get("models", {}).items():
            lowered = model_name.lower()
            if provider == "antigravity" and ("gemini" in lowered or "claude" in lowered):
                found_usage = True
            elif provider == "openai" and "gpt" in lowered:
                found_usage = True
            elif provider in {"local", "ai_kernel"} and "qwen" in lowered:
                found_usage = True
            else:
                continue
            limit = stat.get('limit_tokens', 'unlimited')
            used = stat.get('used_tokens', 0)
            remaining = stat.get('remaining_tokens', 'unlimited')
            print(f"    [{model_name}] limit={limit} used={used} remaining={remaining}")
        if not found_usage:
            print("    No session usage data yet.")

        if provider in {"local", "ai_kernel"} and local_state:
            pressure = local_state.get("memory_pressure", {})
            print(f"  Memory pressure: {pressure.get('pressure_state', 'unknown')} ratio={pressure.get('pressure_ratio', 0.0)} resident_gb={pressure.get('resident_memory_gb', 0.0)} budget_gb={pressure.get('budget_limit_gb', 0.0)}")
            resident_rows = [row for row in local_state.get("resident_models", []) if row.get("provider") == provider]
            blocked_rows = [row for row in local_state.get("blocked_models", []) if row.get("provider") == provider]
            print(f"  Resident models: {', '.join(row.get('model_name', '') for row in resident_rows) or 'none'}")
            print(f"  Blocked models: {', '.join(row.get('model_name', '') for row in blocked_rows) or 'none'}")
        print("")


if __name__ == "__main__":
    print_report()
