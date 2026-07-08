# Full FastAPI Migration Plan

## Current state

The project already uses FastAPI for HTTP and websocket transport.

- `core/scripts/orchestrator_daemon.py`
  - creates `FastAPI(...)`
  - declares REST endpoints
  - declares compatibility websocket endpoints
  - starts Uvicorn
- `core/core/orchestrator_transport.py`
  - contains reusable payload/helper functions used by both HTTP and websocket layers
- `core/core/orchestrator_ws_dispatcher.py`
  - owns the action-based control-plane websocket contract

That means the real migration target is not "REST API to FastAPI". The target is:

1. move FastAPI app construction out of the daemon bootstrap file
2. split routes into routers and schemas
3. keep orchestration logic in transport/service modules
4. progressively retire compatibility HTTP routes where websocket control actions already exist

## Can all REST endpoints be moved to a full FastAPI architecture?

Yes.

Constraints:

- Keep `/health`, `/health/full`, `/api/health` as stable HTTP readiness/liveness endpoints.
- Keep `/chat/ws` and `/control/ws` available during migration.
- Preserve `core/test/test_orchestrator_daemon_diagnostics.py` and `core/test/test_control_ws_protocol.py` behavior while route registration is moved.
- Do not move task normalization/business logic into route functions. Keep it in `task_submission_api.py` and transport/service modules.

## Target structure

Recommended structure:

```text
core/api/
  __init__.py
  server.py
  dependencies.py
  schemas/
    health.py
    providers.py
    local_llm.py
    sourcecraft.py
    diagnostics.py
    chat.py
  routers/
    health.py
    providers.py
    local_llm.py
    sourcecraft.py
    diagnostics.py
    streams.py
    control_ws.py
    chat_ws.py
```

Rules:

- `routers/*` only parse inputs, call helpers, shape responses.
- `schemas/*` define Pydantic request/response models.
- `core/core/orchestrator_transport.py` stays the main adapter/service layer.
- `task_submission_api.py` stays the canonical Task builder and validator.
- `orchestrator_daemon.py` becomes daemon/bootstrap only.

## Endpoint mapping

### Keep as HTTP-first

- `GET /health`
- `GET /health/full`
- `GET /api/health`

### Keep in FastAPI but mark compatibility

- `GET /stats`
- `GET /antigravity/status`
- `GET /providers/openai/runtime_inventory`
- `GET /providers/openai/discovery`
- `GET /providers/openai/model_templates`
- `GET /providers/inventory`
- `GET /providers/{provider}/inventory`
- `GET /providers/runtime_inventory`
- `GET /providers/{provider}/runtime_inventory`
- `GET /providers/models/index`
- `GET /providers/models/index/{model_name}`
- `GET /socraticode/context_compaction/status`
- `GET /providers/local_llm/residents`
- `POST /providers/local_llm/connect`
- `POST /providers/local_llm/disconnect`
- `POST /providers/local_llm/warm`
- `GET /providers/ai_kernel/gate`
- `POST /providers/ai_kernel/ensure`
- `GET /health/local_models`
- `GET /dump_memory`
- `GET /sourcecraft`
- `POST /sourcecraft/delegate`
- `POST /sourcecraft/parallel_delegate`
- `GET /transport/audit`
- `GET /diagnostics`

These already have transport helpers and can be moved router-by-router without changing orchestrator logic.

## Execution sequence

### Stage 1. Separate API bootstrap

Goal:

- introduce a dedicated API module
- keep legacy handlers untouched

Work:

1. add `core/api/server.py`
2. make `run_orchestrator.py` start HTTP from that module
3. keep `orchestrator_daemon.py` as compatibility source of the legacy app factory

Status:

- implemented in this change

### Stage 2. Extract app factory from daemon

Goal:

- move `_build_http_app()` body from `core/scripts/orchestrator_daemon.py` into `core/api/server.py`

Work:

1. move middleware and helper closures into `core/api/server.py`
2. leave thin wrappers in `orchestrator_daemon.py`
3. update imports in tests to prefer `core.api.server.build_http_app`

### Stage 3. Split routers

Goal:

- reduce one monolithic app factory into `APIRouter` modules

Suggested order:

1. `health.py`
2. `providers.py`
3. `local_llm.py`
4. `sourcecraft.py`
5. `diagnostics.py`
6. `streams.py`
7. `control_ws.py`
8. `chat_ws.py`

### Stage 4. Add Pydantic request/response models

Goal:

- replace raw `dict` bodies in FastAPI signatures with typed models

Safe first candidates:

- local LLM connect/disconnect/warm payloads
- AI kernel ensure payload
- SourceCraft delegate payloads

Do not replace internal `create_standard_task()` validation. The schema layer should validate transport shape, not orchestration semantics.

### Stage 5. Introduce dependencies

Goal:

- stop capturing orchestrator as a giant closure variable everywhere

Add:

- `get_orchestrator(request) -> Orchestrator`
- app state storage for orchestrator instance

Then route handlers can be declared as normal functions with `Depends`.

### Stage 6. Deprecate compatibility HTTP control routes

Goal:

- keep the HTTP surface for basic observability and operational compatibility
- move interactive control-plane traffic to `/control/ws`

Policy:

- keep health endpoints permanently
- keep selected read-only HTTP snapshots if external tooling depends on them
- direct new clients to `/control/ws`

## Parallel task split for AI agents

Use parallel workstreams with one owner per bounded area.

### Agent A: API bootstrap and app factory

Files:

- `core/api/server.py`
- `core/scripts/run_orchestrator.py`
- `core/scripts/orchestrator_daemon.py`

Deliverables:

- dedicated `build_http_app()` module
- dedicated `start_http_server()` module
- thin daemon compatibility wrapper

### Agent B: Router extraction

Files:

- `core/api/routers/health.py`
- `core/api/routers/providers.py`
- `core/api/routers/local_llm.py`

Deliverables:

- first router split
- no business logic changes
- route parity tests

### Agent C: SourceCraft and diagnostics

Files:

- `core/api/routers/sourcecraft.py`
- `core/api/routers/diagnostics.py`
- `core/core/orchestrator_transport.py`

Deliverables:

- typed request models
- streaming/http parity checks
- error contract stabilization

### Agent D: Websocket control-plane

Files:

- `core/api/routers/control_ws.py`
- `core/api/routers/streams.py`
- `core/core/orchestrator_ws_dispatcher.py`

Deliverables:

- router extraction only
- preserve action names
- preserve ack/subscription lifecycle

### Agent E: Chat websocket and task ingestion

Files:

- `core/api/routers/chat_ws.py`
- `core/core/task_submission_api.py`
- `core/core/orchestrator.py`

Deliverables:

- keep `stream_user_task()` contract stable
- add typed inbound envelope normalization before orchestration

### Agent F: Tests and migration guardrails

Files:

- `core/test/test_orchestrator_daemon_diagnostics.py`
- `core/test/test_control_ws_protocol.py`
- `core/test/test_task_submission_api.py`
- new `core/test/test_api_server.py`

Deliverables:

- app factory parity tests
- router registration tests
- transport contract snapshots

## Coding instructions for each agent

Common rules:

1. Do not change orchestration behavior and transport payload semantics in the same PR as route extraction.
2. Keep route paths stable until compatibility removal is explicitly scheduled.
3. Use transport helpers from `core/core/orchestrator_transport.py`; do not duplicate payload assembly in routers.
4. Add tests for every moved route group before deleting legacy registration code.
5. Preserve deprecation headers for control-plane HTTP compatibility endpoints.

## Definition of done

Migration is complete when:

1. `core/scripts/orchestrator_daemon.py` no longer constructs FastAPI routes directly.
2. `core/api/server.py` owns app creation.
3. route groups are split into routers with typed schemas.
4. tests pass with imports targeting `core.api.server.build_http_app`.
5. daemon layer only boots orchestrator, agents, and Uvicorn.
