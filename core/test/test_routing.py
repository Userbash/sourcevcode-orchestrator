from core.core.agent_registry import AgentRegistry
from core.core.load_balancer import LoadBalancer
from core.core.inventory_stream_hub import InventoryStreamHub
from core.core.runtime_event_stream_hub import RuntimeEventStreamHub
from core.core.models import AgentStatus, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.task_router import TaskRouter
from core.core.orchestrator import Orchestrator
from core.core.tdd_policy_module import StrictTDDModule


def test_register_agent_and_route_by_capability():
    registry = AgentRegistry()
    registry.register("tester-1", "tester", "local://tester", ["test", "ci"])
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.TEST, TaskInput("run tests"), TaskContext("p", ".", "main"))

    accepted = router.route(task)

    assert accepted.assigned_agent == "tester-1"
    assert accepted.status.value == "accepted"


def test_router_honors_preferred_agent_id_for_parallel_batch():
    registry = AgentRegistry()
    registry.register("code-main", "codex", "local://code-main", ["code", "fix"])
    registry.register("code-alt", "codex", "local://code-alt", ["code", "fix"])
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.CODE, TaskInput("implement branch A"), TaskContext("p", ".", "main"))
    task.required_capability = "code"
    task.routing_hints = {"preferred_agent_id": "code-alt"}

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "code-alt"


def test_router_honors_explicit_orchestrator_route_mode():
    registry = AgentRegistry()
    registry.register("code-main", "codex", "local://code-main", ["code", "fix"])
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.CODE, TaskInput("inspect ingress route"), TaskContext("p", ".", "main"))
    task.routing_hints = {"route_mode": "orchestrator"}

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "orchestrator"


def test_sourcecraft_task_routes_to_orchestrator_when_no_dedicated_agent():
    registry = AgentRegistry()
    registry.register("codex-main", "codex", "local://codex", ["code", "fix"])
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.CODE, TaskInput("Prepare SourceCraft release notes and PR flow for repo status"), TaskContext("p", ".", "main"))
    task.required_capability = "sourcecraft"

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "orchestrator"


def test_balancer_avoids_high_load_agent():
    registry = AgentRegistry()
    busy = registry.register("busy", "tester", "local://busy", ["test"], limits={"max_active_tasks": 1})
    idle = registry.register("idle", "tester", "local://idle", ["test"], limits={"max_active_tasks": 5})
    busy.metrics.active_tasks = 3
    idle.metrics.active_tasks = 0

    chosen = LoadBalancer().choose(registry.list_agents(), "test")

    assert chosen is idle


def test_router_excludes_unroutable_agent_statuses():
    for status in (AgentStatus.FAILED, AgentStatus.OFFLINE, AgentStatus.DISABLED, AgentStatus.OVERLOADED):
        registry = AgentRegistry()
        record = registry.register(f"agent-{status.value}", "tester", f"local://{status.value}", ["test"])
        record.status = status
        record.metrics.status = status
        router = TaskRouter(registry, LoadBalancer())
        task = Task(TaskType.TEST, TaskInput("run tests"), TaskContext("p", ".", "main"))

        accepted = router.route(task)

        assert accepted.status.value == "rejected"
        assert accepted.assigned_agent is None


def test_busy_agent_only_accepts_low_priority_tasks():
    registry = AgentRegistry()
    record = registry.register("busy", "docs", "local://busy", ["docs"])
    record.status = AgentStatus.BUSY
    record.metrics.status = AgentStatus.BUSY
    router = TaskRouter(registry, LoadBalancer())

    normal_task = Task(TaskType.DOCS, TaskInput("write docs"), TaskContext("p", ".", "main"), priority=Priority.NORMAL)
    low_task = Task(TaskType.DOCS, TaskInput("write docs"), TaskContext("p", ".", "main"), priority=Priority.LOW)

    rejected = router.route(normal_task)
    accepted = router.route(low_task)

    assert rejected.status.value == "rejected"
    assert rejected.assigned_agent is None
    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "busy"


def test_orchestrator_provider_preference_uses_agent_filtering():
    registry = AgentRegistry()
    overloaded = registry.register("overloaded", "docs", "local://overloaded", ["docs"], provider="local")
    overloaded.status = AgentStatus.OVERLOADED
    overloaded.metrics.status = AgentStatus.OVERLOADED
    busy = registry.register("busy", "docs", "local://busy", ["docs"], provider="local")
    busy.status = AgentStatus.BUSY
    busy.metrics.status = AgentStatus.BUSY

    orchestrator = object.__new__(Orchestrator)
    orchestrator.registry = registry
    orchestrator.local_agents = {"overloaded": object(), "busy": object()}

    normal_choice = orchestrator._select_agent_by_provider_preference("docs", ["local"], priority=Priority.NORMAL)
    low_choice = orchestrator._select_agent_by_provider_preference("docs", ["local"], priority=Priority.LOW)

    assert normal_choice is None
    assert low_choice == "busy"


def test_router_excludes_agents_over_capacity_before_scoring():
    registry = AgentRegistry()
    overloaded = registry.register("calculated-overloaded", "tester", "local://overloaded", ["test"], limits={"max_active_tasks": 1})
    ready = registry.register("ready", "tester", "local://ready", ["test"], limits={"max_active_tasks": 5})
    overloaded.metrics.active_tasks = 2
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.TEST, TaskInput("run tests"), TaskContext("p", ".", "main"))

    accepted = router.route(task)

    assert overloaded.status == AgentStatus.OVERLOADED
    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "ready"



def test_repo_ops_capability_routes_to_orchestrator_without_dedicated_agent():
    registry = AgentRegistry()
    registry.register("codex-main", "codex", "local://codex", ["code", "fix"])
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.PLAN, TaskInput("Check repo policy and branch governance"), TaskContext("p", ".", "main"), required_capability="repo_ops")

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "orchestrator"


def test_router_prefers_non_openai_agents_when_reusable_memory_is_strong():
    registry = AgentRegistry()
    registry.register("openai-code", "codex", "local://openai", ["code"], provider="openai")
    registry.register("local-code", "codex", "local://local", ["code"], provider="local")
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.CODE, TaskInput("Refactor login parser", files=["auth.py"]), TaskContext("p", ".", "main"))
    task.required_capability = "code"
    task.routing_hints = {"memory_reuse": {"matched": True, "similarity": 0.91}}

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "local-code"


def test_router_prefers_secure_agent_for_high_risk_trusted_profile():
    registry = AgentRegistry()
    registry.register("local-code", "codex", "local://local", ["code"], provider="local")
    registry.register("secure-openai", "codex", "local://secure", ["code"], provider="openai", model_name="gpt-senior-secure", critical=True)
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.CODE, TaskInput("Rotate auth secrets in production"), TaskContext("p", ".", "main"))
    task.required_capability = "code"
    task.routing_hints = {"normalized_text_profile": {"risk_bucket": "high", "decision_trust": "trusted", "confidence_score": 0.84}}

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "secure-openai"


def test_router_avoids_economy_bias_for_single_lane_validation_profile():
    registry = AgentRegistry()
    registry.register("openai-review", "codex", "local://openai-review", ["review"], provider="openai", model_name="gpt-5-review")
    registry.register("local-review", "codex", "local://local-review", ["review"], provider="local", model_name="local-small")
    router = TaskRouter(registry, LoadBalancer())
    task = Task(TaskType.REVIEW, TaskInput("Review risky auth changes"), TaskContext("p", ".", "main"))
    task.required_capability = "review"
    task.routing_hints = {"normalized_text_profile": {"execution_shape": "single_lane_validation", "input_quality_bucket": "clean", "decision_trust": "trusted", "confidence_score": 0.81}}

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "openai-review"


def test_router_tdd_module_without_route_override_keeps_default_routing():
    class _FakeApi:
        def get_module(self, name):
            return StrictTDDModule() if name == "tdd_policy" else None

    registry = AgentRegistry()
    registry.register("tester-1", "tester", "local://tester", ["test", "ci"])
    router = TaskRouter(registry, LoadBalancer())
    router.set_api(_FakeApi())
    task = Task(TaskType.TEST, TaskInput("run regression tests"), TaskContext("p", ".", "main"))

    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "tester-1"


def test_load_balancer_prefers_resident_ready_lane_when_inventory_sources_are_present():
    registry = AgentRegistry()
    local_agent = registry.register('local-hot', 'codex', 'local://local-hot', ['code'], model_name='qwen2.5:32b-instruct-q4_k_m', provider='local_llm')
    kernel_agent = registry.register('kernel-cold', 'codex', 'local://kernel-cold', ['code'], model_name='hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m', provider='ai_kernel')
    local_agent.metrics.avg_latency_ms = 150
    kernel_agent.metrics.avg_latency_ms = 80

    balancer = LoadBalancer()
    runtime_entries = {
        'local_llm': {'status': 'ready', 'diagnostics': {'model_present': True, 'default_model': 'qwen2.5:32b-instruct-q4_k_m'}},
        'ai_kernel': {'status': 'degraded', 'diagnostics': {'inventory_status': 'degraded', 'model_alias_present': False}},
    }
    model_rows = {
        'qwen2.5:32b-instruct-q4_k_m': {'provider': 'local_llm', 'resident': True},
        'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m': {'provider': 'ai_kernel', 'resident': False},
    }
    balancer.set_inventory_sources(
        runtime_inventory_source=lambda provider: runtime_entries.get(provider, {}),
        model_lookup_source=lambda model_name: model_rows.get(model_name, {}),
    )

    chosen = balancer.choose(registry.list_agents(), 'code')

    assert chosen is local_agent



def test_load_balancer_reacts_to_live_inventory_hub_updates():
    registry = AgentRegistry()
    local_agent = registry.register('local-hot', 'codex', 'local://local-hot', ['code'], model_name='qwen2.5:32b-instruct-q4_k_m', provider='local_llm')
    kernel_agent = registry.register('kernel-fast', 'codex', 'local://kernel-fast', ['code'], model_name='hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m', provider='ai_kernel')
    local_agent.metrics.avg_latency_ms = 170
    kernel_agent.metrics.avg_latency_ms = 70

    hub = InventoryStreamHub()
    balancer = LoadBalancer()
    balancer.set_inventory_sources(
        runtime_inventory_source=hub.provider_runtime_entry,
        model_lookup_source=hub.find_model,
    )

    hub.publish({
        'runtime_inventory': {
            'providers': {
                'local_llm': {'status': 'offline', 'diagnostics': {'model_present': False, 'default_model': 'qwen2.5:32b-instruct-q4_k_m'}},
                'ai_kernel': {'status': 'ready', 'diagnostics': {'inventory_status': 'ready', 'model_alias_present': True}},
            }
        },
        'model_index': {
            'updated_at': 100,
            'total_models': 2,
            'provider_counts': {'local_llm': 1, 'ai_kernel': 1},
            'by_model': {
                'qwen2.5:32b-instruct-q4_k_m': {'provider': 'local_llm', 'resident': False},
                'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m': {'provider': 'ai_kernel', 'resident': False},
            },
            'by_provider': {
                'local_llm': ['qwen2.5:32b-instruct-q4_k_m'],
                'ai_kernel': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'],
            },
        },
    })

    assert balancer.choose(registry.list_agents(), 'code') is kernel_agent

    hub.publish({
        'runtime_inventory': {
            'providers': {
                'local_llm': {'status': 'ready', 'diagnostics': {'model_present': True, 'default_model': 'qwen2.5:32b-instruct-q4_k_m'}},
                'ai_kernel': {'status': 'degraded', 'diagnostics': {'inventory_status': 'degraded', 'model_alias_present': False}},
            }
        },
        'model_index': {
            'updated_at': 101,
            'total_models': 2,
            'provider_counts': {'local_llm': 1, 'ai_kernel': 1},
            'by_model': {
                'qwen2.5:32b-instruct-q4_k_m': {'provider': 'local_llm', 'resident': True},
                'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m': {'provider': 'ai_kernel', 'resident': False},
            },
            'by_provider': {
                'local_llm': ['qwen2.5:32b-instruct-q4_k_m'],
                'ai_kernel': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'],
            },
        },
    })

    assert balancer.choose(registry.list_agents(), 'code') is local_agent



def test_load_balancer_honors_live_agent_runtime_events():
    registry = AgentRegistry()
    ready_agent = registry.register('coder-ready', 'codex', 'local://coder-ready', ['code'], provider='local_llm', model_name='qwen2.5:32b-instruct-q4_k_m')
    stale_agent = registry.register('coder-stale', 'codex', 'local://coder-stale', ['code'], provider='openai', model_name='gpt-5.5')
    ready_agent.metrics.avg_latency_ms = 120
    stale_agent.metrics.avg_latency_ms = 20

    hub = RuntimeEventStreamHub()
    balancer = LoadBalancer()
    balancer.set_runtime_event_source(agent_runtime_source=hub.agent_snapshot)

    hub.publish_agent_event('coder-ready', {'status': 'ready', 'source': 'probe'})
    hub.publish_agent_event('coder-stale', {'status': 'offline', 'source': 'probe'})

    chosen = balancer.choose(registry.list_agents(), 'code')

    assert chosen is ready_agent


def test_router_honors_live_workflow_runtime_preferred_agent_updates():
    registry = AgentRegistry()
    registry.register("code-main", "codex", "local://code-main", ["code", "fix"])
    registry.register("code-alt", "codex", "local://code-alt", ["code", "fix"])

    hub = RuntimeEventStreamHub()
    router = TaskRouter(registry, LoadBalancer())
    router.set_runtime_event_source(workflow_runtime_source=hub.snapshot)

    task = Task(TaskType.CODE, TaskInput("Implement runtime-bound routing"), TaskContext("p", ".", "main"))
    task.routing_hints = {"workflow_id": "wf-live-route"}

    hub.publish_workflow_event("wf-live-route", {"routing_hints": {"preferred_agent_id": "code-alt"}})
    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "code-alt"

    next_task = Task(TaskType.CODE, TaskInput("Implement runtime-bound routing follow-up"), TaskContext("p", ".", "main"))
    next_task.routing_hints = {"workflow_id": "wf-live-route"}

    hub.publish_workflow_event("wf-live-route", {"routing_hints": {"preferred_agent_id": "code-main"}})
    next_accepted = router.route(next_task)

    assert next_accepted.status.value == "accepted"
    assert next_accepted.assigned_agent == "code-main"


def test_router_honors_live_workflow_runtime_capability_override():
    registry = AgentRegistry()
    registry.register("planner", "codex", "local://planner", ["plan"])

    hub = RuntimeEventStreamHub()
    router = TaskRouter(registry, LoadBalancer())
    router.set_runtime_event_source(workflow_runtime_source=hub.snapshot)

    task = Task(TaskType.PLAN, TaskInput("Prepare branch governance rollout"), TaskContext("p", ".", "main"))
    task.routing_hints = {"workflow_id": "wf-sourcecraft-route"}

    hub.publish_workflow_event("wf-sourcecraft-route", {"required_capability": "repo_ops"})
    accepted = router.route(task)

    assert accepted.status.value == "accepted"
    assert accepted.assigned_agent == "orchestrator"
