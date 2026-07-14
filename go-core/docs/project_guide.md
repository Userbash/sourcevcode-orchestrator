# Project Guide

## Overview

`go-core` is the orchestration runtime for SourceVCode. It accepts work,
decides how that work should be split, sends it to agents, observes the result,
and stores enough history to explain what happened later.

The current version is built around async execution. The runtime does not rely
on one long blocking call stack anymore. Tasks can be queued, delivered over a
message bus, completed by worker pools, and reassembled into a final workflow
result.

## Core runtime flow

### Task submission

The main entrypoint is the orchestrator. A task reaches the runtime through
HTTP, control, or WebSocket APIs and is handed to `SubmitTask`.

If `GO_CORE_SUBMIT_MODE=async`, the task is first written to the submission
queue. Submission workers pull from that queue and continue the workflow.

If submission runs in sync mode, the same orchestration logic still runs, but
the caller stays in the call path.

### Planning

The planner turns a task into an execution plan. Each plan step becomes a
subtask artifact. If several steps are independent, the planner can release
them in parallel. If one step depends on another, it waits until the dependency
reaches a terminal state.

The planner is dependency-aware. It does not blindly run the whole plan at
once. It runs ready batches, waits for their completion, and then unlocks the
next layer of work.

### Routing

The router selects the next agent. Routing takes task type, capabilities,
runtime hints, and route mode into account.

The failover path can exclude agents that already failed the same workflow.

### Delivery

Distributed execution is based on `TaskEnvelope` messages.

The orchestrator sends envelopes to an agent mailbox topic. A worker pool bound
to that agent consumes the envelope, validates it, runs the work, and publishes
a `TaskResultEnvelope` to the result topic.

This is different from the old direct `agent.Execute` path. The distributed
path now treats result handling as a separate event stream.

### Result handling

Result workers consume task results and move the workflow to a terminal state:

- `completed`
- `failed`
- `dead_lettered`

If a result is recoverable, the orchestrator can reroute the task to another
agent through the normal routing path.

### Memory and telemetry

The runtime stores more than plain workflow records. It also writes:

- peer exchange records
- adaptive decisions
- degradation traces

Those records are chunked and passed into the embedding pipeline so they can be
retrieved later through vector search.

## Main subsystems

### Kernel

`internal/kernel` contains the main scheduler and workflow logic:

- orchestrator
- planner
- router
- runtime manager
- registry
- adaptive runtime

This is the place to start when reading the code.

### Delivery

`internal/delivery` contains the transport-facing execution pieces:

- supervisor
- worker pool
- in-memory bus
- RabbitMQ bus

The in-memory bus is used for local development and tests. RabbitMQ is used
when broker-backed delivery is enabled.

### Memory

`internal/memory` stores structured evidence about runtime behavior:

- task exchanges
- peer exchanges
- adaptive decisions
- degradation traces

The memory manager turns those records into chunks and vectors.

### API and transport

`internal/api`, `internal/realtime`, and `internal/transport` expose the runtime
over HTTP, SSE, and WebSocket protocols.

## RabbitMQ mode

When RabbitMQ is enabled, the runtime treats the broker as the source of truth
for delivery mechanics:

- submission queue: `scheduler.submit`
- result queue: `scheduler.results`
- agent mailboxes: `agent.<agent_id>.inbox`
- dead-letter queue: `dead_letter_queue`

Manual `ack` and `nack` are used in the worker path. This lets the broker
handle requeue and dead-letter behavior in a way the in-memory fallback cannot
fully match.

## Adaptive runtime

The adaptive runtime looks at agent status, error rate, and routing weight. It
can choose among these modes:

- `balanced`
- `throughput`
- `latency_guarded`
- `recovery`

The decision is written into routing hints and stored in memory for later
analysis.

## Degradation tracing

Real task test suites produce degradation traces. A trace records:

- queue latency
- execution latency
- total latency
- throughput
- workflow count
- parallel width
- terminal status counts

These traces are persisted into memory so regressions can be inspected later
with both raw records and vector retrieval.

## Sourcecraft mode

Sourcecraft is planning-only in the current runtime. It can inspect a task,
classify it into a repository workflow family, and emit recommended actions and
guardrails. It does not mutate repositories in this runtime.

The API reports this clearly through the Sourcecraft compatibility payload.

## How to run the project

### Development

```sh
go run ./cmd/orchestrator serve
```

### Unit and integration tests

```sh
go test ./...
```

### Real task regression suite

```sh
go test ./integration/real_tasks
```

## What is covered by tests

The codebase now includes tests for:

- planner branching and dependency batching
- async workflow completion
- reroute and failover behavior
- worker pool retry and dead-letter handling
- adaptive runtime decisions
- memory ingestion for adaptive and degradation data
- real task regression with correctness and performance thresholds

## What the runtime is good at today

The current runtime is strongest in these scenarios:

- multi-step task execution
- parallel task fan-out
- agent failover
- runtime diagnostics
- regression testing with real workflows

It is designed to keep the scheduler moving under mixed load instead of waiting
for one long-running call to finish.
