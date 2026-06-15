# Release Summary: Layered Runtime Memory and Multi-Agent Task Orchestration

## Overview

This release combines five previously separate changes into one publishable update that improves execution planning, runtime memory handling, provider fallback behavior, SourceCraft publication flow, and multi-agent task distribution inside the orchestrator.

The goal of this update is to make the orchestrator more reliable when it receives large engineering tasks, when it needs to coordinate multiple agents at once, and when it needs to recover from provider failures or weak routing decisions.

## What Changed

### 1. SourceCraft runtime hardening and publication flow

This part of the update strengthens the runtime behavior around SourceCraft-backed task execution and repository publication logic.

Key changes:
- Better routing for repository and publication actions inside the orchestrator.
- Cleaner task submission handling for publication-oriented flows.
- Additional validation around task payload normalization and execution context.
- Better testing around SourceCraft runtime behavior and publication scenarios.

Why this matters:
- Publication tasks are more predictable.
- Repository operations are less likely to be mixed with unrelated execution flows.
- The orchestrator has a clearer separation between execution planning and publication handling.

Files with the most visible impact:
- `core/core/sourcecraft_module.py`
- `core/core/task_submission_api.py`
- `core/scripts/orchestrator_daemon.py`
- `core/test/test_sourcecraft.py`
- `core/test/test_sourcecraft_module.py`
- `docs/SOURCECRAFT_PUBLICATION_REPORT.md`

### 2. Layered execution memory

A new layered memory model was introduced to capture execution history in a form that can be reused during later planning and execution.

New file:
- `core/core/layered_context_memory.py`

What it stores:
- raw intent at submission time
- normalized task payload
- planning draft
- decomposition result
- routing outcome
- execution prompt
- result summary
- quality outcome
- lessons learned

How it works:
- When a task enters the system, the orchestrator records the initial intent and normalized payload.
- During planning, the decomposition draft and execution plan are stored.
- During execution, prompts, routing decisions, and final results are captured.
- A compact context slice can later be rebuilt from those layers and injected into future agent prompts.

Why this matters:
- Agents get concise, structured memory instead of raw unfiltered history.
- Repeated task families can reuse proven prompt patterns.
- The system becomes better at preserving reasoning continuity across planning, routing, and execution.

Files with the most visible impact:
- `core/core/layered_context_memory.py`
- `core/core/persistent_memory.py`
- `core/core/session_memory.py`
- `core/test/test_layered_context_memory.py`

### 3. Centralized runtime memory control

Runtime memory access is now managed through a dedicated control module instead of being assembled ad hoc inside the orchestrator.

New file:
- `core/core/memory_control_module.py`

What it does:
- builds runtime memory context for each task and agent
- selects an appropriate memory profile based on provider and model
- records planning, routing, and result events into layered memory
- prepares memory metadata for parallel execution batches

Memory profiles introduced:
- `rich_synthesis`
  Used for richer external reasoning providers that benefit from broader context.
- `focused_execution`
  Used when the execution path should stay narrow and operational.
- `drafting`
  Used for lightweight drafting and local execution flows.
- `routing_meta`
  Used for routing-centric or system-level decision paths.

Why this matters:
- Memory delivery is now consistent across agents.
- The orchestrator no longer has to manually build context in multiple places.
- Parallel tasks can carry structured memory metadata from the start.

Files with the most visible impact:
- `core/core/memory_control_module.py`
- `core/core/orchestrator.py`
- `core/test/test_memory_control_module.py`

### 4. Prompt integration across agents

Agent prompt construction was updated so that runtime memory is added in a short, readable form and written back into memory after use.

Updated agents:
- `core/agents/base_agent.py`
- `core/agents/local_llm_agent.py`
- `core/agents/mistral_agent.py`
- `core/agents/antigravity_cli_agent.py`
- `core/agents/gemini_agent.py`

New behavior:
- each agent can build a compact `memory_brief`
- that brief is appended to the execution prompt
- the prompt can be recorded into layered memory for later reuse

Why this matters:
- execution prompts become more consistent
- prompt history becomes analyzable
- routing and planning can learn from successful prompt patterns

### 5. Parallel task fan-out and agent preassignment

The orchestrator can now split larger code tasks into multiple branches and distribute them across multiple local agents.

Key changes:
- `task_decomposer.py` can generate a parallel plan for large code tasks
- `task_router.py` can honor `preferred_agent_id`
- `orchestrator.py` can preassign ready tasks across agents before batch execution starts

How the fan-out works:
- a large code task may be split into multiple code branches
- each branch can be tagged for a different local agent
- a review or consolidation task can depend on all code branches
- the orchestrator schedules these branches in parallel when dependencies allow it

Why this matters:
- one large implementation task no longer has to run through a single agent path
- different code strategies can be explored in parallel
- review and consolidation become explicit parts of the execution graph

Files with the most visible impact:
- `core/core/task_decomposer.py`
- `core/core/task_router.py`
- `core/core/orchestrator.py`
- `core/test/test_routing.py`
- `core/test/test_orchestrator.py`

### 6. Inter-agent handoff and worker-based delivery

The orchestrator delivery path was changed from a single mailbox fetch model to a worker-based execution model for local agents.

What changed in `core/core/orchestrator.py`:
- agent worker threads are created for local agents
- each worker consumes delivery envelopes from the message bus
- dependent tasks can receive handoff context from previously completed tasks
- the orchestrator can collect dependency summaries, errors, and artifacts and pass them to the next agent

New helper behavior:
- consume handoff payloads per task
- dispatch dependency handoff messages before a new batch starts
- keep per-task runtime futures while the worker completes execution

Why this matters:
- delivery is less fragile under parallel load
- agent execution is no longer tied to one immediate mailbox read
- dependency-aware handoff becomes part of the orchestration flow

### 7. Message bus and RabbitMQ transport improvements

The in-memory message bus and RabbitMQ-backed transport were aligned around the same delivery concepts.

Changes in `core/core/message_bus.py`:
- direct pod inbox delivery
- `send_p2p` support
- `relay_p2p` support
- replay of unacked messages
- cleaner ack lifecycle

Changes in `core/core/rabbitmq_bus.py`:
- direct queue setup for point-to-point delivery
- deserialization back into envelopes and P2P messages
- safer operation when an event loop is already running
- timeout-aware background loop execution for async transport work

Why this matters:
- local and RabbitMQ-backed delivery now behave much more similarly
- P2P handoff can survive replay scenarios
- transport is less likely to break when used from mixed sync/async contexts

Files with the most visible impact:
- `core/core/message_bus.py`
- `core/core/rabbitmq_bus.py`
- `core/test/test_rabbitmq_bus_serialization.py`

### 8. Provider fallback and suppression behavior

Provider fallback logic now keeps more detailed failure state and can temporarily suppress unstable providers.

Changes in `core/core/provider_budget_router.py`:
- tracks quota failures separately
- stores the last error type and detail
- stores the last model name involved in a failure
- supports provider suppression windows
- supports a global suppression snapshot
- parses retry-after style cooldown hints from provider errors

Why this matters:
- the orchestrator can react more intelligently to quota exhaustion and auth failures
- transient provider problems are less likely to poison the next routing decision
- repeated failures can push traffic toward safer alternatives

Related files:
- `core/core/provider_budget_router.py`
- `core/core/reasoning_module.py`
- `core/test/test_provider_budget_and_model_usage.py`

### 9. Additional runtime guardrails

This update also adds several related safety and state management improvements.

Important examples:
- cache and version guard behavior for task execution state
- prompt serialization improvements
- richer runtime state storage in PostgreSQL-backed flows
- improved orchestration daemon behavior
- additional KPI and rolling runtime data for execution tracking

Files with the most visible impact:
- `core/core/cache_guard.py`
- `core/core/prompting/prompt_serializer.py`
- `core/adapters/state/postgres_state_store.py`
- `core/test/test_prompt_serializer.py`
- `core/test/test_versioned_state_and_cache_guard.py`
- `core/test/test_orchestrator_session_guard.py`

## Runtime Flow After This Release

1. A user task enters the orchestrator through the submission API.
2. The task is normalized and the initial intent is recorded.
3. The orchestrator builds advisory context and may generate a decomposition draft.
4. If the task is large enough, it can be expanded into a parallel multi-agent execution plan.
5. The router selects or honors preferred local agents for each ready task.
6. Runtime memory is assembled through the memory control layer.
7. Each agent receives a memory-aware execution prompt.
8. When one task depends on another, the orchestrator sends a P2P handoff with useful context.
9. Results are aggregated, scored, stored, and reused for later routing and planning.
10. If a provider degrades or fails, fallback and suppression rules steer execution toward safer options.

## New and Updated Tests

This release adds or updates tests for:
- layered memory recording and retrieval
- runtime memory control behavior
- preferred agent routing
- parallel batch agent preassignment
- dependency handoff between agents
- RabbitMQ serialization and async loop behavior
- prompt serialization and execution guardrails
- SourceCraft publication and runtime flow

Key test files:
- `core/test/test_layered_context_memory.py`
- `core/test/test_memory_control_module.py`
- `core/test/test_routing.py`
- `core/test/test_orchestrator.py`
- `core/test/test_rabbitmq_bus_serialization.py`
- `core/test/test_prompt_serializer.py`
- `core/test/test_versioned_state_and_cache_guard.py`
- `core/test/test_orchestrator_session_guard.py`

## Suggested GitHub Title

Add layered runtime memory and multi-agent task orchestration

## Suggested Human-Readable Commit Title

Add layered runtime memory, SourceCraft publication hardening, and multi-agent orchestration
