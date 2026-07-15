# Architecture

## System overview

SourceVCode Orchestrator is a control-plane runtime that turns incoming work into managed workflows. It accepts tasks, builds plans, selects models, routes work to agents, coordinates transport, stores execution history, and exposes enough runtime telemetry to explain behavior after the fact.

The system is not organized as a single request-response server. It is closer to a workflow engine with a task planner, policy layer, delivery layer, model inventory, provider-aware routing, and memory-backed execution history.

## Main runtime layers

### Command layer

`go-core/cmd/orchestrator/main.go` is the operational entrypoint. It starts the HTTP server, exposes health checks, bootstraps supporting infrastructure, manages runtime-agent commands, and provides database backup and restore commands.

The `serve` command can optionally ensure that infrastructure and AI stack services are available before the API starts. That makes the runtime usable as both a development daemon and a more production-like service entrypoint.

### API layer

`go-core/internal/api` exposes the runtime to the outside world.

This layer contains:

- HTTP routes for health, diagnostics, tasks, delivery, providers, and runtime control
- WebSocket endpoints for chat, control, and live event streams
- SSE endpoints for runtime and inventory events
- transport audit logging for inbound WebSocket activity
- runtime payload builders for provider inventory, model status, local model health, and route diagnostics

The API layer does not make planning or routing decisions on its own. It translates transport-level requests into orchestrator operations and returns the resulting workflow and execution state.

### Kernel layer

`go-core/internal/kernel` is the orchestration core.

This layer contains:

- task planning
- route construction
- model selection
- provider and model registry logic
- runtime policy and validation
- workflow coordination
- fallback logic
- runtime weighting and adaptive decisions

The kernel is where the critical decision flow lives. If a task is accepted, the kernel decides what the task means, which model should handle it, which agent is allowed to run it, and what should happen when a provider or agent becomes unavailable.

### Agent layer

`go-core/internal/agents` contains provider adapters and execution helpers.

The current runtime supports OpenAI-compatible providers and local execution paths. The provider abstraction allows the runtime to treat cloud APIs, AI-kernel-backed execution, and local model systems as part of one larger routing space.

### Delivery layer

`go-core/internal/delivery` is responsible for moving task envelopes between runtime components.

This layer supports:

- mailbox delivery
- in-memory transport
- RabbitMQ transport
- result acknowledgements
- worker pools
- retry behavior
- dead-letter handling

The delivery layer makes the runtime resilient to execution paths that are not simple in-process function calls.

### State and memory layers

`go-core/internal/state` and `go-core/internal/memory` handle persistence.

These packages store:

- workflow state
- route decisions
- memory records
- vector-oriented memory artifacts
- adaptive traces
- degradation history

This data is used both for observability and for future routing decisions.

## Workflow lifecycle

The orchestrator processes work as a lifecycle rather than a one-shot request.

### 1. Intake

The runtime receives a task from an HTTP or WebSocket surface. The incoming request is normalized into a task contract with metadata such as capability, task type, complexity hints, and context.

### 2. Planning

The planner turns one request into a plan. Plans can contain dependent and independent steps. Independent steps are eligible for parallel execution. Dependent steps wait for prerequisites to complete.

### 3. Selection

The model selector builds a decision context using task metadata, risk, complexity, history, memory, retrieval hints, and provider health.

It does not choose from a fixed hard-coded list. It asks the runtime model registry for live candidates and scores those candidates against the current task.

### 4. Routing

The router chooses an agent whose capability, provider, and availability fit the selected path.

The current router intentionally respects an already assigned provider. This avoids the earlier failure mode where the selector chose one provider but the router sent the task to an incompatible agent.

### 5. Delivery

The task is delivered directly or through mailbox transport. Distributed execution produces envelopes, acknowledgements, and result handling rather than a single blocking function call.

### 6. Result processing

Results update workflow state, runtime event streams, memory records, and route history. Failure can trigger retries, suppression, reroute logic, or dead-letter handling.

### 7. Finalization

A workflow ends in a stable state such as `completed`, `failed`, or `dead_lettered`. The runtime keeps enough information to reconstruct the path that led there.

## Agents and roles

The runtime is built around role-aware agents rather than one undifferentiated worker pool.

The bootstrap logic registers agents and modules for roles such as:

- orchestration
- planning
- coding
- review
- testing
- documentation
- research

This role structure is important because the selector and router are trying to solve two different questions:

- Which model or provider should handle this task?
- Which agent role should execute this kind of work?

The current runtime treats those decisions as connected but distinct.

## Provider and model inventory

The runtime maintains a model registry that refreshes provider inventories and exposes current model availability to the selector and API layer.

This registry solves several operational problems:

- it reveals which providers are configured
- it shows which models are actually discoverable upstream
- it distinguishes missing configuration from temporary degradation
- it gives the selector live candidates instead of static guesses

The registry classifies provider state with explicit statuses such as `ready`, `degraded`, `unavailable`, and `not_configured`.

## Memory-aware orchestration

The runtime can use history and memory as part of routing and model choice.

Selection signals include:

- route history
- recent peer failures
- vector memory activity
- trained memory summaries
- retrieval requirements
- budget hints and token pressure

This makes the system closer to a memory-aware scheduler than a plain provider switch.

## Transport model

The runtime exposes several transport styles at once:

- direct HTTP control
- WebSocket chat and control channels
- server-sent events for runtime and inventory feeds
- internal mailbox transport for distributed execution

The `chat_gateway` proxy exists to let an external chat surface forward traffic to the internal chat WebSocket while keeping the internal orchestrator as the single source of truth.

## Operational commands

The runtime binary supports practical operational commands:

- stack bootstrap
- runtime preflight
- runtime-agent environment setup
- database inspection, backup, and restore
- AI-kernel provisioning and service installation

This is one of the reasons the repository is publication-ready as an application, not only as a library.

