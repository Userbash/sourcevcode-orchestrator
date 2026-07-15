# API and Transport

## Overview

The orchestrator exposes multiple communication surfaces because it is both a control plane and a task execution runtime. It needs direct APIs for automation, state inspection for operators, live streams for observability, and WebSocket transport for chat-style interaction.

The current API surface lives primarily in `go-core/internal/api/http.go`, `go-core/internal/api/control.go`, `go-core/internal/api/runtime_payloads.go`, and `go-core/internal/api/websocket_audit.go`.

## Health and diagnostics

The runtime exposes several health and diagnostics routes:

- `/health`
- `/api/health`
- `/health/full`
- `/diagnostics`
- `/stats`
- `/dump_memory`

These routes are used for liveness checks, deeper runtime inspection, and operator-oriented debugging.

## Provider and model inventory

The runtime exposes provider and model visibility directly through HTTP:

- `/providers/inventory`
- `/providers/runtime_inventory`
- `/providers/models/index`
- `/providers/ai_kernel/gate`
- `/providers/ai_kernel/ensure`
- `/providers/local_llm/residents`
- `/providers/local_llm/connect`
- `/providers/local_llm/disconnect`
- `/providers/local_llm/warm`

These routes make the live model registry observable from the outside. They are useful when checking which providers are configured, which models are available, and whether local model infrastructure is ready.

## Runtime management

The runtime control plane also includes management routes:

- `/runtime/routing_weights`
- `/runtime/providers/...`
- `/runtime/agents/...`
- `/health/local_models`

These endpoints are used to inspect or modify runtime routing behavior, provider state, agent suppression state, and local model health.

## Task and workflow endpoints

The orchestrator exposes task execution and planning routes:

- `/tasks`
- `/tasks/preview_plan`
- `/tasks/run_plan`
- `/tasks/{id}`
- `/state`
- `/modules`
- `/agents`

These routes allow clients to submit work, preview planning output, run explicit plans, inspect a task, or inspect registered modules and agents.

## Delivery and mailbox endpoints

The delivery layer is also visible through the API:

- `/delivery/health`
- `/delivery/inspect_timeouts`
- `/delivery/dispatch`
- `/delivery/ack`
- `/delivery/confirm_payload`
- `/delivery/establish`
- `/delivery/{taskID}`
- `/mailboxes/...`

These routes are useful when diagnosing distributed execution, delivery acknowledgements, or mailbox routing behavior.

## Sourcecraft and orchestration helpers

The runtime includes higher-level orchestration surfaces:

- `/sourcecraft`
- `/sourcecraft/delegate`
- `/sourcecraft/parallel_delegate`
- `/socraticode/context_compaction/status`

These routes provide planning and delegation helpers for repository-oriented workflows and specialized orchestration paths.

## WebSocket endpoints

The runtime exposes multiple WebSocket routes:

- `/control/ws`
- `/chat/ws`
- `/ws/runtime/events`
- `/ws/providers/inventory`

`/chat/ws` is the most important route for chat-style external interaction. It accepts chat traffic using protocol `chat.v1`.

## Server-sent events

The runtime also offers stream endpoints over SSE:

- `/events/runtime`
- `/events/inventory`

These feeds are useful for dashboards and operator monitoring where a full WebSocket control session is unnecessary.

## WebSocket audit logging

One of the important additions in the current runtime is structured WebSocket audit logging.

`go-core/internal/api/websocket_audit.go` records information such as:

- timestamp
- path
- remote address
- session id
- processing stage
- whether normalization happened
- automatic action selected by the runtime
- raw payload
- parsed envelope
- error details

This gives operators a way to inspect the path from incoming chat or control frame to task intake and orchestration behavior.

## Chat relay tools

The repository includes two helper scripts for working with the internal chat WebSocket.

### `script/chat-ws-relay.mjs`

This script is a practical relay client for `/chat/ws`.

It can be used for:

- interactive chat
- sending stdin payloads
- testing message flow without building a separate client

### `script/chat-ws-audit-bridge.mjs`

This script is similar, but it prints more of the stage-by-stage exchange. It is intended for transport audit and lifecycle tracing.

It is useful when the goal is not only to send a task, but to see exactly where intake, acknowledgement, planning, routing, and response handling succeed or fail.

## External chat proxy

The repository also contains a lightweight reverse proxy in `script/chat-proxy/main.go`.

Its purpose is simple:

- accept external chat traffic on port `80`
- forward that traffic to the internal orchestrator on port `8010`
- keep the internal orchestrator as the source of truth

This makes it possible to place a thin external chat surface in front of the runtime without moving decision logic out of the orchestrator.

## End-to-end transport status

The current runtime has already been exercised end to end against the internal WebSocket path:

- `127.0.0.1:8010` responded on `/health`
- WebSocket connection to `/chat/ws` succeeded
- task submission was accepted
- the task was created and planned
- the provider and routed agent stayed aligned
- the final response came back over the same WebSocket session

That confirms that the transport path is not only reachable but operational.

