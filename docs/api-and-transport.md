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

### `/providers/inventory`

The provider inventory payload is now designed to explain why a provider is or is not routable.

Important fields include:

- `status` and `status_reason`
- `probe_queued`
- `cooldown_until`
- `refresh_after`
- current inventory snapshots and default-model state
- collaboration metadata for providers such as `ai_kernel`

The collaboration metadata is intended to explain how a provider is expected to participate in multi-provider orchestration. For `ai_kernel`, the payload can now describe recommended roles such as primary, helper, fallback, or parallel worker, together with the task shapes that fit those roles.

### `/providers/models/index`

The model index payload is now much richer than a plain list of model ids.

Per model or variant, the payload can include:

- `inventory_status`
- `transport_status`
- `verification_status`
- `transport`
- `last_http_status`
- `last_probe_latency_ms`
- `last_success_at`
- `consecutive_failures`
- `consecutive_successes`
- `verification_interval_sec`
- a structured `last_error` object with category, message, retryability, endpoint, request id, observed time, and latency

This matters because operators can now distinguish very different failure modes that previously looked similar from the outside.

Examples include:

- inventory drift, where a configured model no longer exists upstream
- transport failure, where a model exists but the endpoint is unhealthy
- verification pending, where the provider is known but the runtime has not yet confirmed routing readiness
- cooldown and stale-registration states, where the runtime intentionally delays reuse of a noisy or outdated registration

### Pending and staged readiness

Some provider surfaces can now report `pending` instead of only `ready` or `failed`.

That is intentional.

A `pending` state means the runtime has enough information to know that a provider may become usable, but it does not yet have a confirmed result from the current verification cycle. This is used for probe queues, verification windows, and AI-kernel availability checks.

## Runtime management

The runtime control plane also includes management routes:

- `/runtime/routing_weights`
- `/runtime/providers/...`
- `/runtime/agents/...`
- `/health/local_models`

These endpoints are used to inspect or modify runtime routing behavior, provider state, agent suppression state, and local model health.

### Routing weights and pressure feedback

Routing-weight output now reflects more than static provider preference.

The runtime manager can incorporate:

- provider pressure
- worker-class pressure
- in-flight task counts
- per-agent slot usage
- per-model slot usage
- global slot usage
- suppression state
- failure-rate penalties

This makes the route-inspection surface much more useful when an operator is trying to understand why a technically healthy provider is not currently winning selection.

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

### Planning and resumable execution behavior

`/tasks/preview_plan` and `/tasks/run_plan` now sit on top of a more detailed plan-execution model.

A previewed plan can be stored with:

- a static checkpoint for immutable plan structure
- a runtime checkpoint for mutable progress

This allows plan execution to be resumed later without rebuilding the original plan graph and without losing the completed-artifact state that has already been recorded.

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

The control channel now also has explicit end-to-end verification coverage, including the version handshake path. This matters because routing and runtime inspection logic increasingly depend on long-lived control sessions rather than only stateless HTTP requests.

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

## Runtime verification profiles

`go-core/cmd/verify-orchestrator` now does more than a simple alive-or-dead smoke check.

The verifier can report synthetic runtime profiles that summarize how the runtime behaves across several workflow shapes.

Current profile types include:

- a basic sequential documentation-oriented scenario
- an intermediate research and review scenario
- an advanced parallel code-execution scenario

Each profile can include:

- a runtime level and human-readable description
- focus areas and warnings
- workflow traces
- provider, model, and capability distribution summaries
- task event counts and unexpected-event counts
- mean queue, execution, and total latency
- observed parallelism

This makes the verifier useful as a behavior-reporting tool, not only as a transport reachability check.

## Chat relay tools

The repository includes two helper scripts for working with the internal chat WebSocket.

### `scripts/chat-ws-relay.mjs`

This script is a practical relay client for `/chat/ws`.

It can be used for:

- interactive chat
- sending stdin payloads
- testing message flow without building a separate client

### `scripts/chat-ws-audit-bridge.mjs`

This script is similar, but it prints more of the stage-by-stage exchange. It is intended for transport audit and lifecycle tracing.

It is useful when the goal is not only to send a task, but to see exactly where intake, acknowledgement, planning, routing, and response handling succeed or fail.

## External chat proxy

The repository also contains a lightweight reverse proxy in `scripts/chat-proxy/main.go`.

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
