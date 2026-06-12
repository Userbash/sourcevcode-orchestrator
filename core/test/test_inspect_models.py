import asyncio
import sys
import os

# Добавляем корневую директорию проекта в sys.path, чтобы импортировать модуль core
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.mimo.bridge import MimoAsyncBridge

async def main():
    bridge = MimoAsyncBridge()
    print("Refreshing model cache...")
    models = await bridge.refresh_cache()
    
    print(f"\nFound {len(models)} models:\n")
    
    local_models = [m for m in models if m.provider == 'local']
    online_models = [m for m in models if m.provider != 'local']
    
    print("--- LOCAL MODELS ---")
    for m in local_models:
        print(f"ID: {m.full_id}")
        
    print("\n--- ONLINE MODELS ---")
    for m in online_models:
        print(f"ID: {m.full_id} | Provider: {m.provider}")

if __name__ == "__main__":
    asyncio.run(main())
