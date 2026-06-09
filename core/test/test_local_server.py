import asyncio
from core.core.api_bridge_module import APIBridgeModule
from core.core.orchestrator import Orchestrator

def main():
    api = Orchestrator()
    bridge = APIBridgeModule(port=8080)
    bridge.on_load(api)
    print("Test server running on port 8080...")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bridge.on_unload()

if __name__ == "__main__":
    main()
