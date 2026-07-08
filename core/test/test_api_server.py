from core.api.server import build_http_app
from core.scripts.orchestrator_daemon import REQUIRED_HTTP_ENDPOINTS


class _FakeProviderInventory:
    def build_all_provider_endpoint_inventories(self, **kwargs):
        return {"providers": {}, "summary": {}}

    def build_all_provider_runtime_inventories(self, **kwargs):
        return {"providers": {}, "summary": {}}

    def model_index_summary(self):
        return {"updated_at": 0, "total_models": 0, "provider_counts": {}, "by_model": {}, "by_provider": {}}

    def build_provider_endpoint_inventory(self, provider, **kwargs):
        return {"provider": provider, "summary": {}}

    def build_provider_runtime_inventory(self, provider, **kwargs):
        return {"provider": provider, "status": "ready", "models": [], "summary": {}}

    def find_model(self, model_name):
        return None


class _FakeBudgetRouter:
    def suppression_snapshot(self):
        return {}


class _FakeUsageModule:
    def finalize(self):
        return {}


class _FakeModuleManager:
    def get_module(self, name):
        if name == "model_usage":
            return _FakeUsageModule()
        return None


class _FakeAIKernelBridge:
    def gate(self, *, model_name=None, ensure_ready=False):
        return {"provider": "ai_kernel", "ready": True, "model_name": model_name}


class _FakeRegistry:
    def list_agents(self):
        return []


class _FakeHealthcheck:
    def check_all(self):
        return []

    def check_providers(self):
        return {}


class _FakeOrchestrator:
    def __init__(self):
        self.provider_inventory = _FakeProviderInventory()
        self.provider_budget_router = _FakeBudgetRouter()
        self.module_manager = _FakeModuleManager()
        self.ai_kernel_bridge = _FakeAIKernelBridge()
        self.registry = _FakeRegistry()
        self.healthcheck = _FakeHealthcheck()

    def module_state(self):
        return {"sourcecraft": {}, "postgres_state": {}, "local_model_manager": {"blocked_models": [], "memory_pressure": {"pressure_state": "normal"}}}

    def get_module(self, name):
        return None


def test_build_http_app_exposes_required_routes_via_api_server_facade():
    app = build_http_app(_FakeOrchestrator())

    route_paths = {getattr(route, "path", "") for route in app.routes}

    for path in REQUIRED_HTTP_ENDPOINTS:
        assert path in route_paths
