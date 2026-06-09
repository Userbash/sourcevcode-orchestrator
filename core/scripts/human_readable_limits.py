import json
from core.core.orchestrator import Orchestrator

def print_report():
    orch = Orchestrator()
    health = orch.availability.check_all()
    usage_mod = orch.get_module("model_usage")
    stats = usage_mod.get_statistics() if usage_mod else {"models": {}}
    
    print("==========================================================")
    print("          📊 СВОДКА ПО ИИ-МОДЕЛЯМ И ЛИМИТАМ 📊           ")
    print("==========================================================\n")
    
    for provider, data in health.items():
        print(f"🔹 Провайдер: {provider.upper()}")
        status = data.status.value
        print(f"   Статус: {'✅ Здоров' if status == 'healthy' else '❌ ' + status}")
        
        models = data.diagnostics.get("models", [])
        if models:
            # Grouping models to not spam the console
            print(f"   Доступно моделей: {len(models)}")
            print(f"   Популярные: {', '.join(models[:5])}{'...' if len(models) > 5 else ''}")
        else:
            print("   Доступно моделей: 0")
            
        print("   --- Лимиты и потребление ---")
        found_usage = False
        for model_name, stat in stats.get("models", {}).items():
            # Rough matching to see if model belongs to provider
            if provider == "antigravity" and ("gemini" in model_name.lower() or "claude" in model_name.lower()):
                found_usage = True
                print(f"   [{model_name}] Лимит: {stat['limit_tokens']:,.0f} | Использовано: {stat['used_tokens']:,.0f} | Остаток: {stat['remaining_tokens']:,.0f}")
            elif provider == "openai" and "gpt" in model_name.lower():
                found_usage = True
                print(f"   [{model_name}] Лимит: {stat['limit_tokens']:,.0f} | Использовано: {stat['used_tokens']:,.0f} | Остаток: {stat['remaining_tokens']:,.0f}")
            elif provider == "local" and "qwen" in model_name.lower():
                 found_usage = True
                 print(f"   [{model_name}] Лимит: Безлимитно | Использовано: {stat['used_tokens']:,.0f} | Остаток: Безлимитно")
                 
        if not found_usage:
            if provider == "openai":
                print(f"   [gpt-4o] Лимит: 1,000,000 | Использовано: 0 | Остаток: 1,000,000 (Оценка)")
                print(f"   [gpt-4o-mini] Лимит: 5,000,000 | Использовано: 0 | Остаток: 5,000,000 (Оценка)")
            elif provider == "antigravity":
                print(f"   [gemini-1.5-pro] Лимит: 2,000,000 | Использовано: 0 | Остаток: 2,000,000 (Оценка)")
                print(f"   [claude-3.5-sonnet] Лимит: 1,000,000 | Использовано: 0 | Остаток: 1,000,000 (Оценка)")
            else:
                print("   Пока нет данных о потреблении в этой сессии.")
        print("")

if __name__ == "__main__":
    print_report()
