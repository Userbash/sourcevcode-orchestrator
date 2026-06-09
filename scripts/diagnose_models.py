import json
from core.core.orchestrator import Orchestrator

def diagnose():
    orch = Orchestrator()
    
    # Need to make sure modules are loaded (they are in Orchestrator.__init__)
    usage_mod = orch.get_module("model_usage")
    # Availability is not a module in Orchestrator? Let me check orchestrator.py again.
    # Ah, 'availability.py' defines ModelAvailability class, which is not loaded as a module in orchestrator.py
    # But orchestrator.py has self.availability = ModelAvailability()
    
    print("--- AI Model Health & Intensity Report ---")
    
    # 1. Health/Availability
    print("\n[Health Check]:")
    health_data = orch.availability.check_all()
    for provider, health in health_data.items():
        print(f"Provider: {provider}, Status: {health.status.value}, Latency: {health.latency_ms:.2f}ms")
        if health.error:
            print(f"  Error: {health.error}")
            
    # 2. Usage Intensity
    if usage_mod:
        print("\n[Usage Intensity]:")
        stats = usage_mod.get_statistics()
        print(json.dumps(stats, indent=2))
    else:
        print("\n[Usage Intensity]: ModelUsageModule not loaded.")

if __name__ == "__main__":
    diagnose()
