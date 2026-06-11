from __future__ import annotations
import asyncio
import json
from core.core.orchestrator import Orchestrator

async def main():
    print("Initializing Orchestrator and running Self-Diagnostics...")
    orch = Orchestrator()
    
    # Give some time for async on_load tasks to finish
    await asyncio.sleep(2)
    
    diag_module = orch.get_module("self_diagnostic")
    if not diag_module:
        print("Error: self_diagnostic module not found!")
        return

    report = await diag_module.run_diagnostics()
    print("\n=== SYSTEM SELF-DIAGNOSTIC REPORT ===")
    print(json.dumps(report, indent=2))
    print("=====================================")

if __name__ == "__main__":
    asyncio.run(main())
