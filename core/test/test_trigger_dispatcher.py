from core.core.trigger_dispatcher import TriggerDispatcherModule


class _API:
    def log(self, level: str, message: str) -> None:
        pass


def _module() -> TriggerDispatcherModule:
    module = TriggerDispatcherModule()
    module.on_load(_API())
    return module


def test_core_prefix_without_colon_creates_plan_task():
    payload = _module().process_chat_input("core запусти декомпозицию задачи авторизации")
    assert payload is not None
    assert payload["type"] == "plan"
    assert "авторизации" in payload["description"].lower()


def test_typo_yazhro_still_routes_into_orchestrator():
    payload = _module().process_chat_input("яжро проверь статус антигравити")
    assert payload is not None
    assert payload["type"] == "research"
    assert "антигравити" in payload["description"].lower()


def test_yadro_fix_request_maps_to_fix_task():
    payload = _module().process_chat_input("ядро почини логин в оркестраторе")
    assert payload is not None
    assert payload["type"] == "fix"
    assert "логин" in payload["description"].lower()


def test_legacy_prefix_without_core_still_works():
    payload = _module().process_chat_input("PLAN: разложи задачу по шагам")
    assert payload is not None
    assert payload["type"] == "plan"
    assert "разложи задачу" in payload["description"].lower()
