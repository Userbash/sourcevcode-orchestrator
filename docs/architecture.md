# Architecture

## System overview

SourceVCode Orchestrator is a control-plane runtime that turns incoming work into managed workflows. It accepts tasks, builds plans, selects models, routes work to agents, coordinates transport, stores execution history, and exposes enough runtime telemetry to explain behavior after the fact.

The system is not organized as a single request-response server. It is closer to a workflow engine with a task planner, policy layer, delivery layer, model inventory, provider-aware routing, and memory-backed execution history.

## Main runtime layers

### Command layer

`go-core/cmd/orchestrator/main.go` is the operational entrypoint. It starts the HTTP server, exposes health checks, bootstraps supporting infrastructure, manages runtime-agent commands, and provides database backup, restore, and import commands.

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
- session state
- vector chunks
- RAG documents
- RAG memories
- route memories
- VFS artifacts and checkpoints
- adaptive traces
- degradation history

This data is used both for observability and for future routing decisions.

## Workflow lifecycle

The orchestrator processes work as a lifecycle rather than a one-shot request.

### 1. Intake

The runtime receives a task from an HTTP or WebSocket surface. The incoming request is normalized into a task contract with metadata such as capability, task type, complexity hints, and context.

### 2. Planning

The planner turns one request into a plan. Plans can contain dependent and independent steps. Independent steps are eligible for parallel execution. Dependent steps wait for prerequisites to complete.

A plan artifact is now more than a task stub. It can carry:

- a worker class that explains what kind of agent should run the step
- a cluster id for grouping related work
- a context budget for steps that should stay within a tighter context window
- conflict keys that describe resources which should not be touched concurrently
- a weight that lets the runtime express step importance or execution pressure

That metadata is copied into execution contracts and routing hints so later stages do not have to rediscover it.

### 3. Selection

The model selector builds a decision context using task metadata, risk, complexity, history, memory, retrieval hints, and provider health.

It does not choose from a fixed hard-coded list. It asks the runtime model registry for live candidates and scores those candidates against the current task.

### 4. Routing

The router chooses an agent whose capability, provider, and availability fit the selected path.

The current router intentionally respects an already assigned provider. This avoids the earlier failure mode where the selector chose one provider but the router sent the task to an incompatible agent.

Routing can also happen in two modes now:

- immediate binding, where a provider and model are chosen during planning or intake
- deferred binding, where a plan or analysis step intentionally leaves provider and model open until execution-time routing can use the freshest inventory and capacity state

### 5. Delivery

The task is delivered directly or through mailbox transport. Distributed execution produces envelopes, acknowledgements, and result handling rather than a single blocking function call.

### 6. Result processing

Results update workflow state, runtime event streams, memory records, and route history. Failure can trigger retries, suppression, reroute logic, or dead-letter handling.

### 7. Finalization

A workflow ends in a stable state such as `completed`, `failed`, or `dead_lettered`. The runtime keeps enough information to reconstruct the path that led there.

## Parallel execution and resume behavior

The planner now persists parallel-plan execution in two checkpoint forms.

### Static checkpoint

The static checkpoint stores the immutable definition of the plan:

- root task id
- normalized plan graph
- serialized plan artifacts

This data changes only when the plan itself changes.

### Runtime checkpoint

The runtime checkpoint stores mutable execution state:

- pending artifact ids
- completed artifact ids
- collected artifact results
- current batch number
- execution status
- update time

This split reduces write amplification and makes resume behavior easier to reason about. The runtime can restore the plan shape from the static checkpoint and then hydrate execution progress from the runtime checkpoint.

Execution itself is more incremental than before.

- Ready steps are launched as soon as their prerequisites are satisfied.
- Results are consumed continuously rather than only after a full batch barrier.
- Runtime progress can be persisted after each completion.
- Shared cancellation stops outstanding work quickly when one branch fails.

This is the foundation for `ResumeExecutionPlan`, which reconstructs a resumable parallel workflow without rebuilding the original plan from scratch.

## Conflict-aware scheduling

Parallelism is no longer only dependency-aware. It is also resource-aware.

Each artifact can expose conflict keys. A conflict key is a normalized identifier for a resource or coordination domain such as a repository path, a branch, or another mutable execution target.

The scheduler now:

- collects conflict keys from ready artifacts
- refuses to launch two ready artifacts whose keys overlap
- registers active keys while an artifact is running
- frees those keys after completion

This prevents the runtime from creating avoidable races when two otherwise independent tasks both modify the same branch, document set, or mutable project surface.

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

This registry now does more than list configured models.

It tracks:

- upstream inventory snapshots
- verification freshness
- pending verification windows
- confirmation TTLs
- retry cooldown windows
- queue limits for unresolved registrations
- transport and probe outcomes

This solves several operational problems:

- it reveals which providers are configured
- it shows which models are actually discoverable upstream
- it distinguishes missing configuration from temporary degradation
- it prevents stale snapshots from being treated as healthy routing inputs
- it gives the selector live candidates instead of static guesses

The registry classifies provider state with explicit statuses such as `ready`, `degraded`, `unavailable`, `pending`, and `not_configured`.

## Runtime pressure management

The runtime manager now feeds live execution pressure back into routing.

Capacity snapshots include:

- in-flight work counts
- agent slot usage
- model slot usage
- global slot usage

Routing weights can now incorporate:

- provider pressure
- worker-class pressure
- suppression state
- live failure rate
- slot saturation

This matters because a provider that is technically healthy may still be the wrong choice when its worker class is saturated or its error rate is climbing.

## Memory-aware orchestration

The runtime can use history and memory as part of routing and model choice.

Selection signals include:

- route history
- recent peer failures
- vector memory activity
- trained memory summaries
- retrieval requirements
- budget hints and token pressure
- reasoning trace summaries and hit counts

Retrieval is now layered. The memory manager can combine:

- session-local vector chunks
- global vector fallback
- RAG memories
- RAG documents
- reasoning-trace memory derived from prior execution traces

Loaded memory context can include a reasoning-memory brief in addition to ordinary knowledge snippets. This gives planning and selection logic a more structured view of what the runtime has learned from previous executions.

## Reasoning traces and self-learning contracts

The domain layer now includes a first-class `ReasoningTrace` contract.

A reasoning trace can record:

- trace, session, task, and parent ids
- agent, provider, and model
- task type and branch
- prompt, reflection, and result summaries
- reasoning mode
- retrieval usage and memory hit counts
- latency
- decision points
- follow-up questions and metadata

The memory layer can persist these traces as RAG-style memory so they become searchable context for later work.

The domain layer also defines self-learning interfaces for:

- model discovery
- reasoning engines
- RAG retrieval
- trace recording
- code evaluation
- preference dataset building
- training jobs
- hot model reload

These contracts do not force one concrete learning system. They create stable boundaries so trace-driven fine-tuning and model replacement can be added without reworking the orchestration core.

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
- legacy SQL and trace import
- AI-kernel provisioning and service installation

This is one of the reasons the repository is publication-ready as an application, not only as a library.
