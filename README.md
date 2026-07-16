# SourceVCode Orchestrator

SourceVCode Orchestrator is a multi-provider task orchestration runtime built around a Go control plane. It accepts work over HTTP and WebSocket, turns incoming requests into execution plans, routes those plans to the best available agents and models, tracks workflow state, stores memory, and exposes enough runtime diagnostics to explain what happened at every stage.

The repository is designed to run as a complete orchestration stack. The Go runtime is the scheduler and decision engine, not just an API wrapper. It owns planning, routing, delivery, workflow persistence, model inventory, provider health, memory-aware selection, transport audit, and task lifecycle reporting.

## What this project does

The orchestrator handles five core responsibilities:

1. It accepts tasks from HTTP, control, and WebSocket entrypoints.
2. It builds execution plans, including dependency-aware and parallel steps.
3. It selects a model and provider using live inventory, task metadata, memory, and runtime signals.
4. It routes the task to the correct agent and coordinates execution.
5. It records workflow history, transport events, runtime inventory, and operational diagnostics.

This repository also contains the supporting infrastructure needed to run the runtime in practice: PostgreSQL with pgvector, RabbitMQ, local model access, an AI kernel sidecar, and an external chat proxy.

## Main capabilities

- Asynchronous task intake with explicit workflow states
- Dependency-aware execution plans with parallel batching
- Multi-provider model inventory with live runtime refresh
- Task-aware model selection using complexity, risk, memory, retrieval, route history, and budget pressure
- Provider-safe agent routing that respects the model selection result
- Local, AI-kernel, and cloud execution paths
- Message delivery through in-memory transport or RabbitMQ
- Runtime event streaming over HTTP, SSE, and WebSocket
- Transport audit for incoming WebSocket messages and orchestration envelopes
- Sourcecraft planning and delegation endpoints
- Database backup, restore, bootstrap, and runtime preflight tooling

## Repository layout

- `go-core/` contains the Go runtime and orchestration engine
- `scripts/chat-proxy/` contains the lightweight reverse proxy that exposes external chat traffic on port `80` and forwards it to the internal orchestrator
- `scripts/chat-ws-relay.mjs` is a relay client for interactive or stdin-based WebSocket chat submission
- `scripts/chat-ws-audit-bridge.mjs` is an audit-focused relay client that prints the full message exchange
- `docker-compose.yml` defines the local stack used to run the orchestrator and its dependencies
- `memory_store/` contains local memory artifacts used by the runtime
- `docs/` contains the publication-ready documentation for this repository

## Runtime architecture

The runtime is organized as a layered system:

- `cmd/orchestrator` provides the main daemon entrypoint and operational subcommands
- `internal/api` exposes HTTP, WebSocket, SSE, runtime inventory, transport audit, control, and task endpoints
- `internal/kernel` contains the planner, router, model selector, provider registry, policy enforcement, and workflow coordination
- `internal/agents` implements provider adapters and agent execution logic
- `internal/delivery` handles mailbox delivery, acknowledgements, result envelopes, retries, and dead-letter flow
- `internal/state` persists workflows, routes, memory references, and supporting runtime state
- `internal/memory` manages memory ingestion and vector-oriented persistence
- `internal/realtime` publishes runtime and inventory streams
- `internal/domain` defines the core contracts used across the runtime

The orchestrator is deliberately stateful. It keeps enough information to answer why a task was accepted, how it was planned, which provider was selected, which agent executed the task, and why any fallback or rejection happened.

## Task lifecycle

A task moves through a predictable lifecycle:

1. Intake
   The runtime accepts the request over HTTP or WebSocket and normalizes it into a task envelope or internal task contract.
2. Planning
   The planner builds an execution plan and determines whether any work can run in parallel.
3. Model selection
   The selector computes complexity, risk, cost pressure, retrieval requirements, and memory signals, then chooses a provider and model from the live registry.
4. Routing
   The router finds an agent whose provider and capability match the selected execution path.
5. Delivery
   The orchestrator sends work directly or through mailbox transport, depending on the runtime mode.
6. Result handling
   Result envelopes update workflow state, runtime events, and memory records.
7. Review and completion
   The workflow is finalized as `completed`, `failed`, or `dead_lettered`, with state and diagnostics retained for later inspection.

## Model selection and routing

The current runtime no longer assumes that GPT-family models are the only meaningful targets. The selector and router work together as a single decision path.

Important behavior in the current implementation:

- The model registry refreshes provider inventories and discovers live models at runtime.
- Models are classified as ready, degraded, unavailable, missing, or not configured.
- The selector uses task type, complexity, risk, route memory, peer failures, token pressure, retrieval hints, vector memory activity, and trained memory signals.
- The router now respects `AssignedProvider` when binding the selected model to an agent.
- Fallback behavior updates provider and model selection consistently instead of leaving the task in a split state.

This is the change that fixed the earlier selector-router mismatch where the runtime could choose one provider at selection time and another provider at routing time.

## Infrastructure and services

The default stack in `docker-compose.yml` includes:

- `postgresql` for persistent workflow and vector-backed data
- `rabbitmq` for mailbox-style transport
- `local_llm` for local model access
- `ai_kernel` as a local AI execution component
- `go_core` as the orchestrator runtime on port `8010`
- `chat_gateway` as the external reverse proxy on port `80`

The `chat_gateway` service forwards external chat traffic to the internal orchestrator WebSocket endpoint and makes the orchestrator usable as the single source of truth behind an external chat surface.

## HTTP and WebSocket surfaces

The runtime exposes several important entrypoints:

- Health and diagnostics
  `/health`, `/api/health`, `/health/full`, `/diagnostics`, `/stats`
- Provider and model inventory
  `/providers/inventory`, `/providers/runtime_inventory`, `/providers/models/index`
- Runtime management
  `/runtime/routing_weights`, `/runtime/providers/...`, `/runtime/agents/...`
- Delivery and mailboxes
  `/delivery/...`, `/mailboxes/...`
- Task execution
  `/tasks`, `/tasks/preview_plan`, `/tasks/run_plan`, `/tasks/{id}`
- WebSocket control and chat
  `/control/ws`, `/chat/ws`
- Runtime event streams
  `/events/runtime`, `/events/inventory`, `/ws/runtime/events`, `/ws/providers/inventory`
- Transport audit
  `/transport/audit`

The WebSocket chat surface uses protocol `chat.v1`.

## CLI commands

The main runtime binary provides operational commands beyond `serve`:

- `serve`
- `state`
- `healthcheck`
- `bootstrap`
- `runtime-preflight`
- `runtime-agent`
- `runtime-agent-auto`
- `runtime-agent-docker-privileged`
- `runtime-agent-docker-unconfined`
- `runtime-agent-podman-privileged`
- `runtime-agent-podman-unconfined`
- `inspect-db`
- `db-backup`
- `db-restore`
- `import-memory-store`
- `ai-kernel-provision`
- `ai-kernel-serve`
- `ai-kernel-install-service`

These commands cover stack bootstrap, runtime readiness checks, agent runtime setup, database operations, and AI-kernel lifecycle management.

## Running the project

### Local Go runtime

```sh
cd go-core
go run ./cmd/orchestrator serve
```

### Full stack with Docker or Podman

Use the repository root compose file to run the orchestrator stack:

```sh
./scripts/run-podman-stack.sh restart
```

To inspect the currently targeted `go_core` image and the live kernel build metadata:

```sh
./scripts/run-podman-stack.sh status
```

The main internal runtime endpoint is `http://127.0.0.1:8010`.

### WebSocket chat

Internal chat:

```text
ws://127.0.0.1:8010/chat/ws
```

External chat proxy:

```text
http://127.0.0.1/
```

### Tests

```sh
cd go-core
go test ./...
```

## Current validation status

The current runtime has already been exercised across the core transport path:

- Health endpoint on `127.0.0.1:8010` returned `200 OK`
- WebSocket connection to `/chat/ws` succeeded using `chat.v1`
- Chat submission was accepted
- The task was created and planned
- The provider and routed agent stayed aligned after the routing fix
- The task completed and returned a final response over the same WebSocket channel

This confirms that the transport layer, planner, selector, router, and execution path are working together in the rebuilt runtime.

## Publication readiness

This repository is prepared for GitHub publication with a root-level documentation layout:

- `README.md` is the primary entrypoint
- `docs/` contains the detailed technical documentation
- Documentation has been normalized so the root of the repository is the single source of truth

Before a public release, you should still review:

- secrets and provider credentials
- environment defaults
- deployment assumptions for Docker versus Podman
- any private or environment-specific scripts

## Documentation index

- [Architecture](docs/architecture.md)
- [Runtime Routing and Model Selection](docs/runtime-routing-and-model-selection.md)
- [API and Transport](docs/api-and-transport.md)
- [Deployment and Publication](docs/deployment-and-publication.md)
- [Changelog](CHANGELOG.md)

