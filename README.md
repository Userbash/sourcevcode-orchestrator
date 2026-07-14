# SourceVCode Orchestrator

SourceVCode Orchestrator is the runtime control plane for this repository. It accepts user tasks, normalizes noisy input, decides how work should be split, routes each task to the right agent or provider, and returns one merged result with trace data, validation state, and runtime diagnostics.

The active daemon is `go-core`. Docker Compose and the Go bootstrap command build
and run the binary; the remaining `core/` Python tree is migration-only.

The current codebase is built around one active path: structured task orchestration for engineering work. Older descriptions that treated the repository as a general chat bridge, a mixed frontend stack, or a loose collection of experiments are no longer accurate.

## What the runtime does today

- Accepts tasks from HTTP, WebSocket, and internal runtime entry points.
- Cleans and normalizes task input before routing.
- Builds execution plans with explicit dependencies.
- Fans out large coding tasks across multiple agents when the task shape supports parallel work.
- Preserves fan-in dependencies so review, test, and merge steps wait for the full parallel branch set.
- Applies provider policy, budget policy, and fallback rules before execution.
- Builds memory-aware execution context for each task.
- Tracks validation-memory warmup, consensus, and conflict state.
- Returns a merged result together with live trace rows, module state, and orchestration validation output.

## What changed from the older runtime

The active runtime has moved away from several older assumptions.

Previously, the documentation described the system as a broad AI bridge with many interchangeable paths. That description is now too loose. The runtime now has a clearer center of gravity:

- task intake is normalized before planning and routing
- decomposition is dependency-aware rather than mostly linear
- large code work can be split into parallel branches
- branch results are merged only after the whole branch family completes
- memory context is assembled through dedicated modules instead of ad hoc prompt stitching
- validation-memory warmup and conflict state are exposed in the final module report
- provider fallback is explicit, budget-aware, and traceable

Older text also gave too much weight to compatibility paths. Compatibility support still exists where needed, but it is no longer the main story of the system.

## Main runtime areas

1. Orchestration core
   - task planning
   - DAG execution
   - async branch scheduling
   - result merge and validation

2. Routing and policy
   - model selection
   - provider availability
   - budget enforcement
   - fallback control

3. Runtime memory and validation
   - session memory
   - layered context memory
   - validation-memory gate
   - memory warmup reporting

4. Delivery and observability
   - mailbox delivery supervision
   - health checks
   - live trace rows
   - module state snapshots

## Repository layout

- `go-core/` active orchestration runtime, provider clients, transport, and tests
- `core/` legacy Python modules still awaiting feature-complete Go ports
- `docs/` architecture, operational notes, and release-oriented documentation
- `docker-compose.ai.yml` local stack definition

## Start the stack

For an existing plain PostgreSQL 16 container that must be switched to `pgvector/pgvector:pg16` without deleting the database volume, use `./bin/migrate-db-to-pgvector.sh`. The script creates a logical backup first, refuses unsafe cross-major in-place reuse, recreates only the `db` service, and runs `CREATE EXTENSION IF NOT EXISTS vector;`.


```bash
./go-core/orchestrator bootstrap
```

Optional flags:

```bash
./go-core/orchestrator bootstrap --skip-ai-kernel
./go-core/orchestrator bootstrap --model qwen2.5:7b-instruct
```

## Runtime endpoints

- Orchestrator API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`
- Full health: `http://127.0.0.1:8000/health/full`
- RabbitMQ UI: `http://127.0.0.1:15672`
- Local LLM: `http://127.0.0.1:11434`

## Development commands

Run and verify the active orchestrator:

```sh
cd go-core
go test ./...
go vet ./...
go build ./cmd/orchestrator
```

Live provider diagnostics are exposed at
`GET /providers/runtime_inventory` and via `providers.runtime_inventory.get`
on `/control/ws`.

## Documentation guide

Start with these files:

- `docs/SYSTEM_OVERVIEW.md` for the current architecture
- `docs/RUNTIME_CHANGES_AND_MIGRATION_NOTES.md` for the old-to-new runtime comparison
- `docs/README.md` for the documentation map

## Notes on compatibility

A few compatibility aliases still exist for import stability and provider migration. They are not the preferred integration path. The documentation in this repository now describes the current runtime first and treats compatibility behavior as secondary.

## License

MIT. See `LICENSE`.
