import asyncio
from core.core.orchestrator import Orchestrator
from core.core.qwen_code_module import QwenCodeModule

async def verify_qwen_integration():
    print("--- Starting Qwen Code Integration Verification ---")
    orch = Orchestrator()
    
    # 1. Check if module is loaded
    qwen = orch.get_module("qwen_code")
    if not isinstance(qwen, QwenCodeModule):
        print("ERROR: QwenCodeModule not found in Orchestrator modules.")
        return

    # 2. Check health status
    status = qwen.finalize()
    print(f"Module Status: {status['status']}")
    print(f"Version: {status['version']}")
    print(f"Binary Path: {status['binary']}")

    if status['status'] != "active":
        print("ERROR: Qwen Code module is not active.")
        return

    # 3. Perform a simple query
    print("Testing simple query...")
    response = qwen.query("Hello! Return a simple 'Hello from Qwen' string.")
    print(f"Qwen Response: {response}")

    if "Hello" in response or "Qwen" in response:
        print("SUCCESS: Qwen integration verified.")
    else:
        print(f"WARNING: Unexpected response from Qwen: {response}")

if __name__ == "__main__":
    asyncio.run(verify_qwen_integration())
