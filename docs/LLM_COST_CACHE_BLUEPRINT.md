# LLM Cost and Prompt Cache Blueprint

## Status

- Status: Draft
- Scope: All providers, agents, subagents, prodagents, tool-agents, and background AI workers
- Constraint: Implementation must not break the current orchestrator, agent APIs, or PostgreSQL-backed runtime flows

## Purpose

This blueprint converts the cost-efficiency analysis into a non-breaking implementation plan for the current codebase.

The primary failure mode is not request volume by itself. The primary cost spike comes from prompt cache invalidation, repeated transmission of large contexts, and race-prone session execution that causes cache reuse to collapse.

The goal is to improve cache hit rate and cost telemetry for all model providers without forcing a greenfield rewrite.

## Current Code Reality

The repository already contains major building blocks that this plan must extend instead of replacing:

- Orchestration entrypoint: `core/core/orchestrator.py`
- Task routing and economy policy: `core/core/task_router.py`
- Shared agent scoring: `core/core/load_balancer.py`
- Shared model value formula: `core/core/model_value.py`
- Usage accounting and local proxy cost: `core/core/model_usage_module.py`
- Session and hybrid memory: `core/core/session_memory.py`, `core/core/hybrid_memory.py`
- MIMO ranking and rolling KPI windows: `core/mimo/proxy.py`
- RabbitMQ transport with in-memory fallback: `core/core/rabbitmq_bus.py`
- Shadow state storage: `core/adapters/state/postgres_state_store.py`
- Context helpers: `core/core/context/context_window.py`, `core/core/context/context_summarizer.py`

## Architecture Constraints

The original instruction needs correction in several places to stay compatible with the existing code.

### 1. Do not replace the orchestrator contract

`Task`, `TaskEnvelope`, `AgentRecord`, and `AgentMetrics` are already the runtime contract in `core/core/models.py`.

Implication:

- New cache and billing metadata must be added through backward-compatible fields or sidecar objects.
- Do not replace `Task` with a new envelope format in one step.
- Do not require providers to accept a new mandatory prompt object immediately.

### 2. Do not break provider adapters

`core/core/ports/model_provider.py` currently exposes a minimal provider protocol:

- `complete(prompt: str, **kwargs: Any) -> dict[str, Any]`

Implication:

- Introduce a provider-agnostic request envelope at the gateway layer first.
- Flatten it into the current provider interface until all adapters are migrated.
- The gateway owns normalization; providers stay compatible during rollout.

### 3. Do not make RabbitMQ mandatory

`core/core/rabbitmq_bus.py` already supports in-memory fallback when `aio-pika` is unavailable.

Implication:

- Session queueing must be feature-flagged.
- FIFO and debounce logic must work with both RabbitMQ and in-memory transport.
- The queue design cannot assume RabbitMQ-only infrastructure.

### 4. PostgreSQL is not yet the live state authority for orchestration

`core/adapters/state/postgres_state_store.py` is currently a shadow cache behind a feature flag.

Implication:

- Billing, cache metrics, and prompt metadata should be written in shadow mode first.
- Do not treat Postgres as the immediate synchronization lock or exclusive runtime state manager.
- Migrations should expand observability before they become write-critical.

### 5. Context window and summarization are present but minimal

Current implementations are placeholders:

- `core/core/context/context_window.py`
- `core/core/context/context_summarizer.py`

Implication:

- Replace internals behind the same module boundaries.
- Do not wire production policy directly to placeholder behavior.
- Tests must lock desired behavior before upgrading implementations.

### 6. Reuse existing memory and routing hooks

The current code already supports reusable task memory and memory-aware routing:

- `PromptOptimizerModule._reusable_memory_context`
- `Task.routing_hints`
- `TaskRouter._apply_economy_policy`
- `HybridMemory`

Implication:

- Do not introduce a second competing memory-reuse subsystem.
- Extend current routing hints and memory diagnostics instead.

## Non-Breaking Target State

The target design is a provider-agnostic runtime layer that wraps the current stack instead of replacing it.

### Required invariants

- Every AI call must pass through a single prompt assembly path.
- Every AI call must carry `session_id`.
- Every AI call must have stable prompt metadata: `prompt_version`, `context_version`, `prefix_hash`, `full_prompt_hash`.
- Every AI call must emit normalized usage metrics.
- No subagent may mutate the same session context concurrently without queueing or version checks.

### Compatibility rule

If a module cannot yet consume the full metadata set, the gateway must preserve behavior and pass only the currently supported subset downstream.

## Corrected Delivery Plan

### Phase 1: Observability first

Implement this before changing routing or queueing behavior.

Add normalized usage and cache fields in logging and storage:

- `provider`
- `model`
- `session_id`
- `agent_id`
- `parent_agent_id`
- `uncached_input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `cache_hit_rate`
- `termination_reason`
- `cache_miss_reason`
- `prefix_hash`
- `full_prompt_hash`
- `prompt_version`
- `context_version`

Use shadow-write mode first. Do not make these fields required for old callers on day one.

### Phase 2: Unified runtime envelope at the gateway

Add a new internal object such as `LLMRequestEnvelope`, but keep it internal to the runtime layer at first.

Required rule:

- `orchestrator` and adapters may construct the envelope.
- providers still receive `prompt: str` plus compatible keyword arguments until adapter migration is complete.

Do not change `ModelProvider.complete()` to a breaking signature in the same release.

### Phase 3: Deterministic prompt serializer

Build a dedicated serializer module and route all outbound prompt construction through it.

Serializer rules:

1. System instructions first.
2. Tool and plugin definitions sorted deterministically.
3. Static knowledge blocks next.
4. Dynamic environment block last within the system prefix.
5. User and assistant history after the static prefix.

Required outputs:

- Serialized prompt payload
- `prefix_hash`
- `full_prompt_hash`
- block-level diagnostics for debugging cache invalidation

Do not let individual agents assemble provider-specific prompt orderings on their own.

### Phase 4: Session-safe execution ordering

Extend the existing transport and orchestration flow instead of adding a parallel execution plane.

Implementation direction:

- add session-aware FIFO dispatch on top of `MessageBus` and `RabbitMQBus`
- preserve in-memory fallback behavior
- add optional debounce for bursty system events
- add optimistic version checks for session state writes

This should protect both root agents and subagents from race-driven context mutation.

### Phase 5: Real context window management

Replace placeholder context utilities behind their current module boundaries.

Rules:

- Postgres retains full history.
- Runtime prompt receives only a bounded working set.
- Older content is summarized and re-inserted as a stable context block.
- Summary blocks must be versioned and traceable.

Do not store the raw full conversation inside every live prompt.

### Phase 6: Provider and agent adapter normalization

Migrate adapters one by one:

- OpenAI
- Anthropic
- local LLM
- existing external AI agents
- subagent runtime flows
- prodagent orchestration flows

Each adapter must normalize usage into the shared format, even if the provider does not expose native cache telemetry.

For providers without native cache metrics:

- compute derived `cache_hit_rate` from local request assembly metadata
- mark the source as derived

### Phase 7: Cost guardrails

Add a guard service after metrics are trustworthy.

Trigger condition:

- same `session_id`
- at least 3 consecutive requests
- `uncached_input_tokens > 50000`
- `cached_input_tokens < 20000`
or
- `cache_hit_rate < 0.20`

Actions:

- warn-only mode first
- soft-stop second
- hard-stop only after operator validation

Do not hard-block healthy sessions based on incomplete telemetry.

## Corrected TDD Plan

The original TDD-first direction is correct, but it needs repo-specific test boundaries.

### Mandatory test layers

- unit tests for serializer, envelope validation, and cache-miss classification
- contract tests for provider adapters
- regression tests for the observed spike pattern around repeated large uncached prompts
- routing tests for memory-aware economy policy
- queue ordering tests for per-session execution
- migration tests for new billing fields
- compatibility tests proving legacy provider calls still work

### High-risk regression targets

Add explicit regression tests for:

- prompt block reorder causing `prefix_hash` drift
- dynamic timestamp injected too early in the prompt
- multiple subagents writing to one session without ordering
- local model usage recorded without normalized cost fields
- MIMO and load balancer drifting from the shared value formula

## Epic Adjustments

### Epic 1: Unified runtime contract

Keep it, but implement it first as an internal gateway contract, not as a global API break.

### Epic 2: Prompt serializer

Keep it. This is the highest-value architectural change for cache stability.

### Epic 3: Session state manager

Reuse `SessionMemory`, `HybridMemory`, and `PostgresStateStore` instead of inventing a parallel state subsystem.

### Epic 4: Queueing and sequential processing

Implement on top of `MessageBus` and `RabbitMQBus`, with fallback compatibility.

### Epic 5 and 6: Context window and summarization

Keep both, but ship them by upgrading `core/core/context/` modules rather than adding a second context stack.

### Epic 7: Usage logging and billing schema

Keep it. It aligns directly with `ModelUsageModule`.

### Epic 8: Circuit breaker

Keep it, but only after telemetry quality is validated in shadow mode.

### Epic 9: Provider adapters

Keep it, but make adapter migration incremental and backward-compatible.

### Epic 10: Agent hierarchy compatibility

Keep it. This is important because the repository already has multiple agent types and external runtime adapters.

### Epic 11: Rollout strategy

Keep it, but add an explicit compatibility gate:

- no release may remove the current in-memory execution path until session-safe ordering is proven in tests

## Recommended Backlog Order

1. Expand usage logging in `ModelUsageModule` and KPI event logging.
2. Add gateway-level `LLMRequestEnvelope` and usage normalization.
3. Add prompt serializer and hashing.
4. Add regression tests for cache invalidation patterns.
5. Add session-aware queue ordering and version checks.
6. Replace context window and summarizer internals.
7. Migrate provider adapters.
8. Enable cost guardrails in warn-only mode.
9. Roll out to subagents and prodagents.

## Definition of Done

- No provider adapter is broken by the new envelope layer.
- Existing `Task` and `TaskEnvelope` flows still execute.
- Cache telemetry is visible for all AI call paths, including local models.
- Prompt assembly is deterministic and hashable.
- Session execution ordering is stable for root agent and subagent flows.
- MIMO, task routing, and load balancing continue to use the shared model value logic.
- Regression tests cover the high-cost cache invalidation pattern.

## Implementation Notes for Developers

- Prefer additive changes over signature rewrites.
- Reuse existing module boundaries wherever they already represent the right abstraction.
- Use feature flags for every cross-cutting runtime change.
- Shadow-write new telemetry before enforcing policy from it.
- Do not introduce a second memory, routing, or queue subsystem when an extension of the current one is sufficient.

## Code References

- `core/core/orchestrator.py`
- `core/core/task_router.py`
- `core/core/load_balancer.py`
- `core/core/model_value.py`
- `core/core/model_usage_module.py`
- `core/core/session_memory.py`
- `core/core/hybrid_memory.py`
- `core/core/rabbitmq_bus.py`
- `core/adapters/state/postgres_state_store.py`
- `core/core/context/context_window.py`
- `core/core/context/context_summarizer.py`
- `core/mimo/proxy.py`
