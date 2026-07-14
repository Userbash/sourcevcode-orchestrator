# Current Worktree Changes

This document describes the changes currently present in the worktree. It is
meant to answer one practical question: what changed in behavior, not just what
files were touched.

## 1. Async orchestration is now a real runtime path

The orchestrator no longer treats async submission as a thin wrapper around the
old synchronous execution path.

What changed:

- submission workers can pull tasks from a dedicated submission queue
- result workers consume result envelopes from a result queue
- workflow state is tracked with explicit terminal and non-terminal statuses
- planner code can wait for workflow completion instead of assuming that a task
  is done because `SubmitTask` returned

Why it matters:

The runtime can now keep scheduling work while other tasks are still running.
That improves throughput and keeps long-running agent calls from blocking the
whole system.

Main files:

- `internal/kernel/orchestrator.go`
- `internal/domain/types.go`
- `internal/domain/delivery.go`

## 2. Distributed execution now uses result envelopes

The distributed path no longer depends on a direct blocking
`agent.Execute(ctx, task)` call for completion.

What changed:

- the orchestrator dispatches `TaskEnvelope` messages
- agent worker pools execute envelopes
- results come back as `TaskResultEnvelope`
- failures can trigger reroute through the router

Why it matters:

This separates task ownership from task completion and makes failover much
cleaner.

Main files:

- `internal/kernel/orchestrator.go`
- `internal/delivery/worker_pool.go`
- `internal/delivery/bus.go`

## 3. Message bus semantics are stronger

The project now supports richer broker behavior in both RabbitMQ and in-memory
mode.

What changed:

- submission and result topics were added
- RabbitMQ consumers use manual delivery wrappers with `Ack` and `Nack`
- queue TTL and dead-letter configuration hooks were added
- the in-memory bus now behaves more like a real broker and wakes consumers
  through signals instead of noisy polling

Why it matters:

Delivery handling is closer to production broker behavior, and the in-memory
fallback wastes less CPU.

Main files:

- `internal/delivery/message_bus.go`
- `internal/delivery/rabbitmq_bus.go`
- `internal/delivery/supervisor.go`

## 4. Worker pools are now first-class execution units

Agent mailboxes are processed by a dedicated worker pool.

What changed:

- configurable per-agent concurrency
- direct stream consumption when the bus supports it
- retry and dead-letter hooks
- worker metrics such as processed count, retries, failures, and average latency

Why it matters:

The runtime can run more than one agent task at a time without keeping the
control path busy.

Main file:

- `internal/delivery/worker_pool.go`

## 5. Planner semantics were tightened

Parallel planning already existed, but the behavior is now better defined and
better tested.

What changed:

- the planner waits for terminal workflow completion where needed
- real dependency-based parallel batches are covered by tests
- typed plan status values replace loose string handling

Why it matters:

Queued work is no longer confused with completed work.

Main files:

- `internal/kernel/advanced_planner.go`
- `internal/kernel/advanced_planner_test.go`

## 6. Fairer scheduling under load

Submission scheduling was adjusted so one task source does not starve the rest
of the system.

What changed:

- session-aware inflight limits
- fairer distribution for queued tasks
- CPU-aware default concurrency values

Why it matters:

Short and independent tasks get a better chance to run even when the system is
already busy.

Main files:

- `internal/kernel/orchestrator.go`
- `internal/kernel/orchestrator_budget_test.go`

## 7. Adaptive runtime decisions were added

The runtime can now react to degraded agents instead of treating every agent as
equally healthy.

What changed:

- adaptive decision types were added
- diagnostics collect health, degradation, suppression, and error-rate signals
- the runtime can choose balanced, throughput, latency-guarded, or recovery mode
- decisions are persisted into memory

Why it matters:

The orchestrator can lower risk when the pool is unhealthy and widen parallel
execution when the pool is stable.

Main files:

- `internal/domain/adaptive.go`
- `internal/kernel/adaptive_runtime.go`
- `internal/memory/adaptive_memory.go`

## 8. Degradation telemetry is now part of the system

The project can record performance and execution regression data as structured
memory, not just ad-hoc logs.

What changed:

- degradation traces and samples were added as typed domain records
- traces are stored in memory and embedded into vector chunks
- real task suites write traces and verify retrieval

Why it matters:

The runtime can now compare healthy and unhealthy execution runs using stored
evidence.

Main files:

- `internal/domain/degradation.go`
- `internal/memory/degradation_memory.go`
- `integration/real_tasks/real_tasks_test.go`

## 9. Peer exchange memory was added

Peer-to-peer work between agents is now written into memory as a first-class
record.

What changed:

- `RecordPeerExchange` was added to the memory manager
- peer routing and result details are stored as text plus metadata

Why it matters:

Later analysis can explain why a task was rerouted, retried, or rejected by an
agent.

Main file:

- `internal/memory/manager.go`

## 10. Sourcecraft support is clearer

Sourcecraft behavior is now described as planning-only rather than a vague
compatibility stub.

What changed:

- task families and safe actions are exposed through typed helpers
- planning emits Sourcecraft-specific hints
- API status payloads explain current support and limitations

Why it matters:

The runtime is more honest about what Sourcecraft can and cannot do today.

Main files:

- `internal/kernel/task_policy.go`
- `internal/kernel/planner.go`
- `internal/api/runtime_payloads.go`

## 11. Provider bootstrap and compatibility handling were cleaned up

What changed:

- MIMO and Antigravity provider configuration no longer depends on fake default
  URLs
- bootstrap registration was split into clearer functions
- default agent registration includes the provider-backed executor set

Main files:

- `internal/agents/openai_compatible.go`
- `internal/kernel/bootstrap.go`

## 12. Regression coverage is much wider

The test surface is broader than before and is closer to how the runtime is
actually used.

Added coverage includes:

- async workflow lifecycle
- planner dependency groups
- worker retries and dead-letter behavior
- adaptive runtime behavior
- memory ingestion and vector chunking
- real task end-to-end regression for code, research, docs, and review scenarios

Main files:

- `internal/kernel/orchestrator_trace_test.go`
- `internal/kernel/orchestrator_real_tasks_regression_test.go`
- `internal/kernel/orchestrator_degradation_test.go`
- `internal/memory/real_task_trace_memory_test.go`
- `integration/real_tasks/real_tasks_test.go`

## Removed local cache data

Local Go build caches were removed from the project tree:

- `.gocache/`
- `.gomodcache/`

That was cleanup only. It does not change runtime behavior.
