# Runtime Routing and Model Selection

## Why this part of the system changed

The most important recent runtime fix addressed a structural mismatch between model selection and agent routing.

Before the change, the orchestrator could accept a task, build a plan, assign one provider during selection, and then route the task to an agent backed by another provider. The runtime policy layer correctly rejected that state, but the rejection happened too late. Transport, intake, and planning looked healthy while execution still failed before any model call began.

A real failure pattern looked like this:

- the selector assigned `mistral`
- the router selected an `ai_kernel` agent
- policy validation blocked execution because the routed agent did not match the assigned provider

The fix was not limited to one conditional branch. The selector, router, policy layer, model inventory, and fallback path now behave as one coherent decision system.

## Model registry

The runtime now maintains a live provider model registry in `go-core/internal/kernel/model_registry.go`.

The registry does more than list configured providers.

It:

1. loads provider configurations
2. refreshes model inventories from those providers
3. records transport and verification outcomes
4. applies freshness, cooldown, and pending windows
5. exposes a normalized view of the runtime model space to the selector and API layer

This means the orchestrator no longer works from a small, hard-coded GPT-only view of the world.

## Provider status model

The registry distinguishes provider state explicitly.

Current provider statuses include:

- `ready`
- `degraded`
- `pending`
- `unavailable`
- `not_configured`

This matters because the runtime must tell the difference between:

- a provider that is healthy and recently confirmed
- a provider that is reachable but degraded
- a provider that is configured but still waiting on a current probe or confirmation cycle
- a provider that is configured but cooling down after repeated failures
- a provider that is not configured at all

Those states should not be routed the same way.

## Model liveness rules

The runtime now determines model liveness from the registry instead of a static list.

A model is not considered routable simply because it once appeared in a provider catalog.

The runtime now cares about several dimensions at once:

- inventory presence
- transport health
- verification status
- confirmation freshness
- retry cooldown state

### Models considered routing-ready

Models are treated as healthy routing candidates when they are:

- present in the current provider inventory
- available upstream
- transport-reachable enough to pass the runtime's readiness rules
- confirmed by the current verification cycle
- attached to a provider snapshot that is still fresh enough for routing

### Models treated as non-routable

Models are excluded when they are in states such as:

- `missing`
- `disabled`
- `unavailable`
- `inventory_missing`
- `transport_failed`
- `endpoint_misconfigured`
- `registration_stale`
- `verification_cooldown`
- `verification_pending`
- `unconfirmed`

This stricter gate prevents the selector from treating stale or half-known inventory as if it were production-ready routing input.

## Missing model detection

If a provider is configured with a default model that does not appear in the upstream inventory, the runtime now injects a synthetic missing-model record.

This prevents silent drift. Instead of failing later with a vague provider error, the runtime can explain that the configured default does not exist in the provider's catalog.

## Refresh, pending, and cooldown controls

Model inventory refresh and validation are controlled through environment variables.

Key settings include:

- `AI_BRIDGE_MODEL_REFRESH_ENABLED`
- `AI_BRIDGE_MODEL_REFRESH_INTERVAL`
- `AI_BRIDGE_MODEL_REFRESH_TIMEOUT`
- `AI_BRIDGE_MODEL_VALIDATE_MODELS`
- `AI_BRIDGE_MODEL_VALIDATE_LIMIT`
- `AI_BRIDGE_MODEL_PENDING_TTL`
- `AI_BRIDGE_MODEL_CONFIRMATION_TTL`
- `AI_BRIDGE_MODEL_RETRY_COOLDOWN`
- `AI_BRIDGE_MODEL_QUEUE_LIMIT`

Compatibility variables under the `GO_CORE_MODEL_REGISTRY_*` prefix are also supported.

These controls let operators choose how aggressive the runtime should be about keeping inventory fresh, how long pending confirmations remain meaningful, and how quickly noisy models should be retried.

## Provider probes and readiness gating

The runtime now has an asynchronous provider probe manager.

Its job is to:

- queue provider probes instead of probing every state transition inline
- refresh stale provider health on a schedule
- avoid probe storms by applying cooldown and queue rules
- merge probe outcomes back into provider health and inventory state

This is important because provider readiness is no longer a static configuration fact. It is a living runtime property that can move through queued, pending, confirmed, degraded, or stale states.

The AI-kernel path uses similar staged readiness semantics. For example, live model probes can cause the AI-kernel gate to report `pending` while verification work is still queued.

## What the selector evaluates

The selector in `go-core/internal/kernel/model_selector.go` has been expanded into a scored routing decision layer.

It now considers:

- task type
- required capability
- inferred complexity
- task risk
- priority
- file count
- constraint count
- budget pressure
- token pressure
- route history
- recent peer failures
- retrieval or RAG requirements
- vector memory activity
- trained memory signals provided in task context
- provider and model health from the live registry
- reasoning-memory signals and prior reasoning traces
- live runtime pressure from the runtime manager

This is the practical answer to the requirement to use memory, KPI-like signals, retrieval, route history, and runtime pressure when choosing a model.

## Complexity and risk

The selector classifies tasks before it scores providers.

It can recognize differences such as:

- low, medium, and high complexity
- high-risk work
- coding-heavy work
- review and planning work
- documentation work
- research-oriented work

That allows the runtime to reason about task shape before it chooses a provider.

## Runtime pressure and routing weights

The runtime manager no longer treats every healthy provider as equally cheap to use.

Routing weight can now incorporate:

- provider pressure
- worker-class pressure
- in-flight counts
- per-agent slot usage
- per-model slot usage
- global slot usage
- suppression state
- observed error-rate penalties

This changes routing behavior in a useful way. A provider can be technically available and still lose the decision if it is overloaded, suppressed, or producing too many recent failures.

## Scoring and candidate resolution

The selector no longer uses a single fallback chain. It builds a candidate list from live inventory and scores the candidates.

The score is influenced by:

- base policy preference
- provider priority
- past route success
- budget fit
- retrieval fit
- failure penalties
- live load pressure

This is important because it gives the runtime a real way to compare multiple valid providers instead of only asking which provider appears first in configuration.

## Expanded provider support

The current runtime is not limited to GPT-family providers.

Provider resolution and tests now cover real support for:

- `openai`
- `codexsale`
- `mistral`
- `mimo`
- `antigravity`
- `local`
- `ai_kernel`

This does not mean all providers are identical. It means they are real selection candidates when they are configured, alive, confirmed, and policy-compatible.

## Router behavior after the fix

The router in `go-core/internal/kernel/router.go` now respects `AssignedProvider` during initial routing.

The important changes are:

- candidate agents are filtered by the provider chosen during selection
- routing now fails early and clearly if no available agent matches the assigned provider
- fallback updates provider and model assignment consistently instead of leaving split state behind
- dynamically overridable agents can be treated differently from agents that require a fixed assigned model

A task can still move to a fallback path when runtime conditions require it, but fallback is now explicit rather than accidental.

## Dynamic step routing and execution metadata

Planning and execution are also more tightly connected now.

A plan step can carry metadata such as:

- worker class
- cluster id
- context budget
- conflict keys
- execution weight

This metadata is propagated into routing hints and execution contracts.

Some step types, especially plan and analysis steps or tasks with zero dependencies, can intentionally leave provider and model unset during plan construction. That allows execution-time routing to pick the best provider from the freshest confirmed inventory instead of freezing the choice too early.

## Conflict-aware parallel scheduling

The execution planner now understands that dependency-free does not always mean safe-to-run-together.

Ready steps can be held back when they share conflict keys. A conflict key represents a mutable resource domain such as a repository branch, a file group, or another coordination surface.

This prevents avoidable parallel races while still letting the runtime keep unrelated work in flight.

## Policy alignment

The policy layer now sees better-aligned input because the selector and router are working from the same provider decision.

This reduces false execution blocks and makes policy rejection more meaningful. When the runtime rejects execution now, it is less likely to be rejecting its own internal inconsistency.

## Tests added for this behavior

Tests across the kernel now verify behavior such as:

- the router honoring an assigned provider
- clear rejection when no matching agent is available
- fallback to healthy non-GPT cloud models
- preference for code-specialist paths where appropriate
- memory and route-history influence on selection
- budget-aware local preference when task pressure demands it
- helper and checkpoint behavior in the planner and orchestrator

## Practical effect

The result of this work is that routing and model selection now behave like one system instead of two disconnected heuristics.

In runtime terms, this means:

- healthy non-GPT providers are real candidates
- stale, unconfirmed, or cooling-down models can be excluded from selection automatically
- provider selection uses more than static defaults
- live runtime pressure can change routing even when inventories look healthy
- agent routing honors the provider decision instead of fighting it
- execution failures are more likely to represent real runtime issues rather than internal disagreement
