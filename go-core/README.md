# go-core

`go-core` is the native Go orchestration runtime replacing the Python control plane.

## Run

```sh
go run ./cmd/orchestrator serve
go test ./...
go build ./...
docker build -t sourcevcode-orchestrator-go .
```

The daemon reads `GO_CORE_ADDR` or the legacy `AI_BRIDGE_API_HOST` /
`AI_BRIDGE_API_PORT` variables. Runtime state, memory, routing history,
prompts, and VFS data are stored in PostgreSQL. Configure the connection via
`AI_BRIDGE_MEMORY_DATABASE_URL` or the PostgreSQL environment variables used by
the bootstrap layer.

## Structure

- `cmd/orchestrator`: daemon entrypoint, signal handling, graceful shutdown
- `internal/domain`: task, result, agent, workflow, and event contracts
- `internal/kernel`: orchestrator, planner, router, model selector, registry
- `internal/state`: persistent PostgreSQL-backed workflow, memory, routing, and VFS state
- `internal/realtime`: bounded event hubs with subscriptions and drop accounting
- `internal/transport`: WS protocol, sessions, dispatcher, and RFC6455 transport
- `internal/api`: HTTP/SSE/WS control plane and compatibility routes
- `internal/agents`: native OpenAI-compatible provider clients and agent executors
- `internal/modules`: kernel module registry

## Transport

Native endpoints include:

- `/control/ws`: action dispatcher using `chat.v1` or `chat.json`
- `/chat/ws`: compact and full chat envelopes
- `/ws/runtime/events` and `/ws/providers/inventory`: direct live streams
- `/events/runtime` and `/events/inventory`: SSE compatibility streams
- task, state, health, diagnostics, provider inventory, model index, and
  Sourcecraft HTTP compatibility routes

The dispatcher supports ACK frames, cancellation, unsubscription, request
timeouts, heartbeat frames, snapshots, deltas, and structured error taxonomy.

## Migration status

Ported runtime layers:

- domain contracts, task policy, model selection, planning, routing
- orchestrator lifecycle and persistent workflow state
- realtime runtime/inventory hubs
- daemon bootstrap and HTTP/SSE/WebSocket transport
- native OpenAI, Codex Sale, Mistral, Ollama/local-LLM and AI-kernel clients
- live provider health probes and provider/model inventory
- production Docker/Compose and bootstrap launch points

The old Python HTTP/WebSocket daemon, session and dispatcher were removed after
their Go regression coverage became active.

Remaining Python areas to port before the legacy `core/` tree can be deleted:

- MIMO, Antigravity, voice/audio, ML and tool-enabled code execution
- richer memory, delivery/mailbox, validation/security, and database layers
- remaining maintenance scripts and tests that import legacy Python modules

Provider inventory distinguishes configured state from a successful live probe;
credentials are never returned in snapshots.
