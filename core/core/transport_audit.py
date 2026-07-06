from __future__ import annotations

import os
from typing import Any

HTTP_CONTROL_PLANE_ENDPOINTS = [
    "/health",
    "/health/full",
    "/api/health",
    "/antigravity/status",
    "/stats",
    "/providers/openai/runtime_inventory",
    "/providers/openai/discovery",
    "/providers/openai/model_templates",
    "/providers/inventory",
    "/providers/{provider}/inventory",
    "/providers/runtime_inventory",
    "/providers/{provider}/runtime_inventory",
    "/providers/models/index",
    "/providers/models/index/{model_name}",
    "/providers/local_llm/residents",
    "/providers/local_llm/connect",
    "/providers/local_llm/disconnect",
    "/providers/local_llm/warm",
    "/health/local_models",
    "/dump_memory",
    "/sourcecraft",
    "/sourcecraft/delegate",
    "/diagnostics",
]

WS_ENDPOINTS = [
    "/ws/providers/inventory",
    "/ws/providers/runtime_inventory",
    "/ws/providers/models/index",
    "/ws/runtime/events",
    "/chat/ws",
]

DEFAULT_DIRECT_CALL_MODULES = [
    "local_llm",
    "sourcecraft",
    "memory_control",
    "validation_memory_gate",
    "local_model_manager",
    "trigger_dispatcher",
    "risk_advisor",
    "intelligence",
    "security_sentinel",
]


def _message_bus_backend(orchestrator: Any | None) -> str:
    if orchestrator is not None:
        bus = getattr(orchestrator, 'message_bus', None)
        if bus is not None:
            return type(bus).__name__
    raw = str(os.getenv('AI_BRIDGE_MESSAGE_BUS_BACKEND', 'inmemory') or 'inmemory').strip().lower()
    return raw or 'inmemory'


def _loaded_direct_modules(orchestrator: Any | None) -> list[str]:
    if orchestrator is None or not hasattr(orchestrator, 'get_module'):
        return list(DEFAULT_DIRECT_CALL_MODULES)
    modules: list[str] = []
    for name in DEFAULT_DIRECT_CALL_MODULES:
        try:
            if orchestrator.get_module(name) is not None:
                modules.append(name)
        except Exception:
            continue
    return modules or list(DEFAULT_DIRECT_CALL_MODULES)


def build_transport_audit(orchestrator: Any | None = None) -> dict[str, Any]:
    backend = _message_bus_backend(orchestrator)
    direct_modules = _loaded_direct_modules(orchestrator)
    inventory_hub_present = bool(getattr(orchestrator, 'inventory_stream_hub', None)) if orchestrator is not None else False
    local_agent_count = len(getattr(orchestrator, 'local_agents', {}) or {}) if orchestrator is not None else 0

    subsystems = [
        {
            'name': 'chat_ingress',
            'transport': 'websocket',
            'mode': 'event_stream',
            'direction': 'ingress_egress',
            'endpoints': ['/chat/ws'],
            'ws_only': True,
        },
        {
            'name': 'provider_inventory_stream',
            'transport': 'websocket',
            'mode': 'event_stream',
            'direction': 'egress',
            'endpoints': ['/ws/providers/inventory'],
            'ws_only': True,
        },
        {
            'name': 'provider_runtime_inventory_stream',
            'transport': 'websocket',
            'mode': 'event_stream',
            'direction': 'egress',
            'endpoints': ['/ws/providers/runtime_inventory'],
            'ws_only': True,
        },
        {
            'name': 'provider_model_index_stream',
            'transport': 'websocket',
            'mode': 'event_stream',
            'direction': 'egress',
            'endpoints': ['/ws/providers/models/index'],
            'ws_only': True,
        },
        {
            'name': 'runtime_event_stream',
            'transport': 'websocket',
            'mode': 'event_stream',
            'direction': 'egress',
            'endpoints': ['/ws/runtime/events'],
            'ws_only': True,
        },
        {
            'name': 'control_plane_api',
            'transport': 'http',
            'mode': 'request_response',
            'direction': 'ingress_egress',
            'endpoints': list(HTTP_CONTROL_PLANE_ENDPOINTS),
            'ws_only': False,
        },
        {
            'name': 'agent_dispatch',
            'transport': 'message_bus',
            'mode': 'event_driven',
            'direction': 'internal',
            'backend': backend,
            'ws_only': False,
        },
        {
            'name': 'inventory_runtime_sync',
            'transport': 'in_memory_event_bus',
            'mode': 'event_driven',
            'direction': 'internal',
            'backend': 'inventory_stream_hub' if inventory_hub_present else 'snapshot_polling',
            'ws_only': False,
        },
        {
            'name': 'module_invocation',
            'transport': 'direct_call',
            'mode': 'in_process',
            'direction': 'internal',
            'targets': list(direct_modules),
            'ws_only': False,
        },
    ]

    event_driven = [item for item in subsystems if item.get('mode') == 'event_driven' or item.get('transport') in {'websocket', 'in_memory_event_bus'}]
    http_bound = [item for item in subsystems if item.get('transport') == 'http']
    direct_bound = [item for item in subsystems if item.get('transport') == 'direct_call']
    bus_bound = [item for item in subsystems if item.get('transport') == 'message_bus']

    migration_plan = [
        {
            'phase': 'phase_1',
            'title': 'Keep HTTP control-plane stable',
            'goal': 'Do not move admin, health, readiness, or mutation endpoints to WS.',
            'targets': ['/health', '/health/full', '/providers/*', '/diagnostics'],
            'reason': 'These operations are idempotent control-plane calls and are safer over HTTP/FastAPI.',
        },
        {
            'phase': 'phase_2',
            'title': 'Move event streams to WS only where streaming matters',
            'goal': 'Use WS for provider inventory, chat, live agent events, and fan-out/fan-in progress.',
            'targets': ['provider_model_index_stream', 'runtime_event_stream', 'chat_ingress', 'delivery events', 'workflow progress'],
            'reason': 'These paths benefit from push delivery and low-latency incremental updates.',
        },
        {
            'phase': 'phase_3',
            'title': 'Keep internal orchestration on MessageBus or in-memory bus',
            'goal': 'Do not replace local direct-call and brokered agent dispatch with WS blindly.',
            'targets': ['agent_dispatch', 'module_invocation', 'inventory_runtime_sync'],
            'reason': 'Internal RPC and queue semantics need ack/retry/state guarantees that WS alone does not provide.',
        },
    ]

    return {
        'status': 'ok',
        'summary': {
            'fully_ws': False,
            'core_transport_mode': 'hybrid',
            'control_plane_transport': 'http',
            'event_stream_transport': 'hybrid',
            'internal_dispatch_transport': 'message_bus_and_direct_call',
            'ws_endpoint_count': len(WS_ENDPOINTS),
            'http_endpoint_count': len(HTTP_CONTROL_PLANE_ENDPOINTS),
            'local_agent_count': local_agent_count,
        },
        'ws_endpoints': list(WS_ENDPOINTS),
        'http_endpoints': list(HTTP_CONTROL_PLANE_ENDPOINTS),
        'message_bus': {
            'backend': backend,
            'event_driven': True,
            'ws_only': False,
        },
        'inventory_bus': {
            'backend': 'inventory_stream_hub' if inventory_hub_present else 'snapshot_polling',
            'event_driven': inventory_hub_present,
            'ws_only': False,
        },
        'direct_calls': list(direct_modules),
        'subsystems': subsystems,
        'classification': {
            'event_driven': [item['name'] for item in event_driven],
            'http_control_plane': [item['name'] for item in http_bound],
            'message_bus': [item['name'] for item in bus_bound],
            'direct_call': [item['name'] for item in direct_bound],
        },
        'migration_plan': migration_plan,
    }
