# go-core

`go-core` is the Go runtime behind the orchestrator. It accepts tasks, plans the
work, routes it to the right agent, tracks execution, stores memory, and
publishes runtime events over HTTP, SSE, and WebSocket APIs.

The current runtime is built around an async orchestration model. Tasks can be
queued, split into smaller steps, executed in parallel, retried through a
message bus, and recorded as workflow traces for later diagnosis.

## What the project does

At a high level, the runtime handles five things:

1. It accepts tasks from HTTP, control, and WebSocket entrypoints.
2. It turns those tasks into execution plans and parallel sub-steps.
3. It routes work to local or remote agents.
4. It records workflow state, runtime events, memory artifacts, and
   degradation telemetry.
5. It exposes enough diagnostics to explain why a task succeeded, failed, or
   was rerouted.

The runtime is not just a thin API server. It is the scheduler, workflow store,
delivery layer, planner, router, and execution coordinator.

## Main features

- async task submission with explicit workflow states
- parallel plan execution with dependency-aware batching
- agent-to-agent task delivery over in-memory transport or RabbitMQ
- failover rerouting when an agent rejects or fails a task
- adaptive runtime decisions based on degradation metrics
- vector memory for task history, peer exchanges, adaptive decisions, and
  degradation traces
- real-time runtime and inventory streams over SSE and WebSocket
- Sourcecraft planning-only mode for repository and workflow governance tasks

## Workflow states

The runtime now treats task state as a first-class contract. A task moves
through one of these states:

- `queued`
- `running`
- `completed`
- `failed`
- `dead_lettered`

That state model is used by the orchestrator, the planner, the delivery layer,
and the regression test suites.

## Architecture

- `cmd/orchestrator`: daemon entrypoint, signal handling, graceful shutdown
- `internal/domain`: typed contracts for tasks, workflows, delivery, adaptive
  decisions, and degradation traces
- `internal/kernel`: orchestrator, planner, router, registry, adaptive runtime,
  policy, and workflow coordination
- `internal/delivery`: mailbox delivery, worker pools, in-memory bus, RabbitMQ
  bus, supervisor, and dead-letter flow
- `internal/state`: persistent workflow, memory, routing, and VFS state
- `internal/memory`: memory manager, vector ingestion, adaptive and degradation
  trace persistence
- `internal/realtime`: bounded runtime and inventory event hubs
- `internal/transport`: WebSocket sessions, dispatcher, and framing
- `internal/api`: HTTP/SSE/WebSocket control plane and compatibility routes
- `internal/agents`: OpenAI-compatible providers and local agent executors
- `integration/real_tasks`: end-to-end real task regression and performance
  suites

## How execution works

### 1. Task intake

`SubmitTask` accepts a task and either handles it synchronously or places it on
the submission queue, depending on `GO_CORE_SUBMIT_MODE`.

### 2. Planning

The planner turns one task into an execution plan. If the plan contains
independent steps, those steps can run in parallel. If a step depends on a
previous step, it waits.

### 3. Routing

The router picks an agent based on task type, required capabilities, route
mode, and current runtime conditions. In failover cases it can exclude agents
that already failed the same workflow.

### 4. Delivery

For distributed execution the orchestrator builds a `TaskEnvelope`, sends it to
an agent mailbox topic, and waits for a result envelope instead of blocking on a
direct function call.

### 5. Result handling

Result workers consume `TaskResultEnvelope` messages, update the workflow,
publish runtime events, persist memory records, and trigger reroute logic when
needed.

### 6. Adaptive control

The adaptive runtime watches agent health and error rates. It can reduce
parallelism, suppress bad lanes, and switch the route mode to a safer recovery
path.

## Run

### Local run

```sh
go run ./cmd/orchestrator serve
```

### Tests

```sh
go test ./...
```

### Build

```sh
go build ./...
docker build -t sourcevcode-orchestrator-go .
```

## Important configuration

Server:

- `GO_CORE_ADDR`
- `AI_BRIDGE_API_HOST`
- `AI_BRIDGE_API_PORT`

Database and memory:

- `AI_BRIDGE_MEMORY_DATABASE_URL`

Submission and execution:

- `GO_CORE_SUBMIT_MODE`
- `GO_CORE_SUBMIT_WORKERS`
- `GO_CORE_RESULT_WORKERS`
- `GO_CORE_AGENT_WORKERS`
- `GO_CORE_MAX_CONCURRENT_TASKS`
- `GO_CORE_MAX_CONCURRENT_PER_AGENT`
- `GO_CORE_MAX_CONCURRENT_PER_MODEL`
- `GO_CORE_MAX_INFLIGHT_PER_SESSION`
- `GO_CORE_AGENT_POLL_INTERVAL_MS`

Message bus:

- `GO_CORE_MESSAGE_BUS_BACKEND`
- `AI_BRIDGE_MESSAGE_BUS_BACKEND`
- `AI_BRIDGE_RABBITMQ_URL`
- `GO_CORE_RABBITMQ_MESSAGE_TTL_MS`

Provider configuration:

- OpenAI-compatible providers use their own base URL and API key variables
- MIMO and Antigravity can be configured directly in Go without falling back to
  placeholder defaults

## Transport

Native endpoints include:

- `/control/ws`
- `/chat/ws`
- `/ws/runtime/events`
- `/ws/providers/inventory`
- `/events/runtime`
- `/events/inventory`
- HTTP task, state, health, diagnostics, inventory, model index, and
  Sourcecraft routes

The dispatcher supports ACK frames, cancellation, unsubscription, request
timeouts, heartbeat frames, snapshots, deltas, and structured error responses.

## Test coverage

The current test layout covers:

- kernel orchestration and workflow regression
- planner parallel batching and dependency handling
- delivery retries, dead-letter flow, and worker pool behavior
- adaptive runtime decisions
- memory ingestion for peer exchanges, adaptive decisions, and degradation
  traces
- end-to-end real task regression for `code`, `research`, `docs`, and `review`
  scenarios

## Extra documentation

- [Host runtime](docs/host_runtime.md)
- [P2P delivery](docs/p2p_delivery.md)
- [Project guide](docs/project_guide.md)
- [Current worktree changes](docs/worktree_changes.md)

## Migration status

The Go runtime now covers the control plane, planning, routing, delivery,
workflow state, runtime streams, provider inventory, regression tests, and most
of the orchestration lifecycle.

The old Python daemon and its transport layer are no longer the active control
plane. Some provider-specific or ML-heavy pieces still live outside this tree,
but the core scheduler and orchestrator runtime now run here.
