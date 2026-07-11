# System Overview

SourceVCode Orchestrator is the coordination runtime for this repository. Its job is not to act like a generic chat bot. Its job is to turn incoming engineering requests into structured work, execute that work through the right agents and providers, and return a result that can be inspected, validated, and explained.

## Current design goals

The current runtime is built around a small set of practical goals:

- keep task routing explicit
- keep execution state observable
- break large work into smaller units only when the decomposition is useful
- allow parallel code execution without losing dependency safety
- keep memory and validation state visible instead of burying it inside prompts
- degrade safely when a provider, bridge, or local runtime is unavailable

## Runtime flow

### 1. Intake

A task enters through HTTP, WebSocket, or an internal runtime path. Before the system decides anything else, it normalizes the incoming payload. The normalization layer strips noisy formatting, preserves structured fields, and builds a routing profile that captures risk, confidence, execution shape, and task quality.

### 2. Planning

The orchestrator creates an execution plan. Small work may stay as a single task. Broader work becomes a DAG of atomic tasks with explicit dependencies.

This is where the runtime changed in a meaningful way. Older documentation often described the system as if work naturally moved through a mostly linear plan-code-test-review chain. That is no longer the only active path. The planner can now create parallel coding branches when the request has enough surface area to justify that split.

### 3. Agent and provider selection

Each atomic task is decorated with capability, complexity, model hints, and routing metadata. The scheduler, model selector, provider inventory, and budget modules then choose the execution lane.

The routing path is policy-driven. It takes into account:

- task type
- risk level
- normalized input quality
- provider health
- provider endpoint capability
- session and provider budget state
- agent readiness
- fallback restrictions

The selection path is no longer based on static provider preference alone. The runtime now keeps a provider inventory snapshot, a model health registry, and an adaptive routing layer. Together they answer three separate questions before work starts:

- is the provider reachable and authenticated
- is the model visible, routable, and healthy for the requested role
- is there a better primary or fallback choice for this task shape right now

### 4. Execution

Execution happens through the delivery supervisor and mailbox handoff path rather than through loose direct calls. That gives the runtime a more consistent way to supervise local agents, track handshakes, detect timeouts, and preserve branch-level task state.

The execution loop now has stronger failure control than older builds. Repeated identical handoffs can be suppressed, repeated failed executions can trigger a loop-guard event, and agent or provider failures can be retried, quarantined, or routed to a fallback lane depending on the failure class.

### 5. Memory and validation

The current runtime no longer treats memory as a loose prompt appendix. It builds task context through dedicated modules.

The important pieces are:

- `MemoryControlModule` for runtime context assembly
- `ValidationMemoryGate` for warmup, consensus, and conflict detection
- layered memory and reusable task memory for short execution context
- local model memory pressure tracking for resident model management

The final module state now includes a `memory_warmup_report` so operators can see warmup totals, conflict counts, pressure state, and the latest validation-memory snapshot in one place.

The runtime also adds two data-oriented modules to the normal task path:

- `DataAnalyticsModule` inspects memory storage, retrieval readiness, freshness, retention, and operational risk, then exposes those signals to routing and health checks
- `DataIntelligenceModule` builds keyword, phrase, sentence, and template matrices from the task, retrieves related analytics memories, and prepares a prompt data pool for downstream agents

### 6. Fan-out and fan-in for code tasks

Large coding tasks can fan out across multiple agents. This branch family is not just cosmetic. Each branch can be assigned its own agent and memory profile.

The important correction in the current code is the fan-in rule: downstream tasks now wait for the whole parallel branch set, not just the first branch that happened to be created. That keeps review, test, and merge steps aligned with the actual execution graph.

Each branch now also carries a more explicit execution contract. That contract can include branch goals, assumptions, exit criteria, expected artifacts, lane labels, and parallel-group metadata. In practice this makes parallel work easier to audit and easier to merge.

The same planning machinery now supports specialized multi-agent waves for analytics-heavy work. The runtime can build dedicated plans for analytics coding tasks and for analytics matrix tasks instead of forcing those requests into the generic coding path.

### 7. Final merge and report

When execution ends, the orchestrator merges branch results, applies review and validation checks, and returns:

- merged output
- per-task results
- live trace rows
- scheduler decisions
- module state
- orchestration report

The final runtime snapshot now also carries model-health data and data-analytics state, which makes it easier to understand why a provider was selected, skipped, retried, or degraded.

## Main modules in the active path

### Orchestrator

Owns planning, routing, execution, fallback, and result merge.

### TaskDecomposer

Builds the task graph and now supports parallel coding branches with proper dependency fan-in and richer branch contracts.

### ModelSelector and provider routing modules

Choose providers and model families using current runtime policy rather than static preferences alone.

### AdaptiveRoutingEngine and ModelHealthRegistry

Turn provider inventory, role suitability, recent probe history, and runtime health into concrete primary, fallback, and parallel routing decisions.

### MemoryControlModule and ValidationMemoryGate

Build runtime context, warm memory from persistent storage, detect memory conflicts, and expose validation state.

### DataAnalyticsModule and DataIntelligenceModule

Expose storage health and retrieval readiness to the runtime, then build structured analytics context that other agents can reuse.

### DeliverySupervisor

Supervises mailbox-style delivery for local agents and gives the orchestrator a consistent handoff path.

### AgentLoopGuard

Detects repeated identical handoffs or repeated failure patterns so the orchestrator can stop burning cycles on work that is not making progress.

## What is no longer the main architecture story

Several older descriptions are now misleading if treated as primary architecture:

- the system is not centered on a generic WebSocket chat bridge
- compatibility aliases are not the recommended integration path
- stale routing experiments that are no longer imported are not part of the active runtime
- documentation written around older broad platform slices should be treated as historical context, not as the source of truth

## Where to read next

- `RUNTIME_CHANGES_AND_MIGRATION_NOTES.md`
- `AI_BRIDGE_RUNTIME_ROUTING.md`
- `AI_ORCHESTRATOR_CORE.md`
