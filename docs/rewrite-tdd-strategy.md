# Clean-room rewrite strategy

## Decision and scope

The legacy `go-core` remains unchanged and serves only as an executable
behavioural reference. New work takes place in `rewrite/`, an isolated Go module.
The rewrite starts with a small in-process orchestrator. PostgreSQL, RabbitMQ,
vector memory, provider probes, dashboards, admin commands and WebSocket control
are adapters or later milestones—not prerequisites for a correct kernel.

The current codebase has these responsibilities: task intake, plan construction,
provider/model selection, provider-safe agent routing, dependency/conflict-aware
execution, retries/dead-letter handling, persistence, and HTTP/WebSocket
observability. The original code interleaves many of them. The new design must
keep them separate.

## Target architecture

```
HTTP / WS adapters ──> Application service ──> Kernel
                         │                    ├─ planner
                         │                    ├─ selector/router
                         │                    ├─ scheduler
                         │                    └─ workflow state machine
                         └─ Ports ──> Store | Agent executor | Event publisher
```

Domain objects must contain no I/O. The kernel depends on small interfaces
(`WorkflowStore`, `AgentRegistry`, `Executor`, `Clock`, `IDGenerator`), while
file/Postgres/RabbitMQ/provider/HTTP implementations depend on the kernel.

## Non-negotiable invariants

1. A task has a non-empty id and description; invalid input changes no state.
2. Workflow transitions are explicit and terminal states cannot be reopened.
3. A chosen `(provider, model)` is preserved by routing. A fallback changes both
   together and is recorded with a reason.
4. Only ready, compatible, non-stale candidates may be selected.
5. A plan is a DAG. Unknown dependencies and cycles are rejected before work runs.
6. A step starts only after every dependency completes successfully.
7. Steps whose normalized conflict keys overlap never execute concurrently.
8. Independent, non-conflicting ready steps may execute concurrently, subject to
   configured limits.
9. A branch failure cancels not-yet-completed work, persists a terminal failure,
   and preserves all completed results.
10. Retrying the same idempotency key returns the original workflow; it never
    executes work twice.
11. Every persisted workflow has append-only decision/audit events.

## Delivery sequence

1. **Foundation:** domain value types, validation, deterministic IDs/clock, and
   an in-memory store. Make unit tests green one at a time.
2. **Decision path:** registry, selector, router and atomic fallback. Do not add
   HTTP or a real provider yet.
3. **Workflow engine:** DAG validation, scheduler, cancellation, checkpoints,
   idempotency and file-store adapter.
4. **HTTP:** health, task submission/readback, plan preview/run, error envelope,
   provider inventory. The black-box HTTP tests become mandatory.
5. **Operations:** structured logs/metrics, then optional WebSocket/SSE and
   external adapters. Add contract tests before each adapter.
6. **Parity gate:** run a small, recorded set of legacy requests against old and
   new binaries, compare normalized response/status/decision fields—not internal
   implementation details.

## Test policy

Run fast unit tests on every change; run race tests before merge; run HTTP tests
with the real binary; run E2E smoke in an isolated temporary state directory.

```sh
cd rewrite
go test ./internal/orchestrator
go test -race ./internal/orchestrator
go test ./test/http ./test/e2e
go vet ./...
```

Coverage is a guardrail, not the goal: require 90% line coverage for the kernel
after it exists and 100% coverage for state-transition and validation tables.
No test may use `time.Sleep` to infer correctness; inject a clock or use channels.

## Explicitly deferred

Real cloud credentials, migrations, RabbitMQ, PostgreSQL/pgvector and UI stacks
are excluded from the first vertical slice. They need their own adapter contract
and opt-in integration tests so the core remains deterministic and runnable
offline.
