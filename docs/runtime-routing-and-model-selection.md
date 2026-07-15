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

The registry does four things:

1. It loads provider configurations.
2. It refreshes model inventories from those providers.
3. It classifies provider and model health.
4. It exposes a normalized view of the runtime model space to the selector and API layer.

This means the orchestrator no longer works from a small, hard-coded GPT-only view of the world.

## Provider status model

The registry distinguishes provider state explicitly.

Current provider statuses include:

- `ready`
- `degraded`
- `unavailable`
- `not_configured`

This matters because the runtime must tell the difference between:

- a provider that is healthy
- a provider that is reachable but degraded
- a provider that is configured but currently failing
- a provider that is not configured at all

Those states should not be routed the same way.

## Model liveness rules

The runtime now determines model liveness from the registry instead of a static list.

The selection behavior is intentionally more nuanced than a simple alive-dead split.

### Models considered alive or usable

- models reported as available in a healthy snapshot
- models with a `probe_failed` condition when the runtime still treats them as degraded but usable

### Models treated as dead for selection

- `validation_failed`
- `missing`
- `disabled`
- `unavailable`

The distinction is operationally useful. A failed validation probe may still indicate a temporarily noisy provider. A missing model usually means local configuration and upstream reality no longer match, and routing should stop immediately.

## Missing model detection

If a provider is configured with a default model that does not appear in the upstream inventory, the runtime now injects a synthetic `missing` model record.

This prevents silent drift. Instead of failing later with a vague provider error, the runtime can explain that the configured default does not exist in the provider's catalog.

## Refresh and validation controls

Model inventory refresh and validation are controlled through environment variables.

Key settings include:

- `AI_BRIDGE_MODEL_REFRESH_ENABLED`
- `AI_BRIDGE_MODEL_REFRESH_INTERVAL`
- `AI_BRIDGE_MODEL_REFRESH_TIMEOUT`
- `AI_BRIDGE_MODEL_VALIDATE_MODELS`
- `AI_BRIDGE_MODEL_VALIDATE_LIMIT`

Compatibility variables under the `GO_CORE_MODEL_REGISTRY_*` prefix are also supported.

These controls let operators choose how aggressive the runtime should be about keeping inventory fresh and whether model validation should happen automatically.

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

## Scoring and candidate resolution

The selector no longer uses a single fallback chain. It builds a candidate list from live inventory and scores the candidates.

The score is influenced by:

- base policy preference
- provider priority
- past route success
- budget fit
- retrieval fit
- failure penalties

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

This does not mean all providers are identical. It means they are real selection candidates when they are configured, alive, and policy-compatible.

## Router behavior after the fix

The router in `go-core/internal/kernel/router.go` now respects `AssignedProvider` during initial routing.

The important changes are:

- candidate agents are filtered by the provider chosen during selection
- routing now fails early and clearly if no available agent matches the assigned provider
- fallback updates provider and model assignment consistently instead of leaving split state behind

A task can still move to a fallback path when runtime conditions require it, but fallback is now explicit rather than accidental.

## Policy alignment

The policy layer now sees better-aligned input because the selector and router are working from the same provider decision.

This reduces false execution blocks and makes policy rejection more meaningful. When the runtime rejects execution now, it is less likely to be rejecting its own internal inconsistency.

## Tests added for this behavior

Two test files document the most important guarantees:

- `go-core/internal/kernel/router_provider_test.go`
- `go-core/internal/kernel/model_selector_test.go`

These tests verify behavior such as:

- the router honoring an assigned provider
- clear rejection when no matching agent is available
- fallback to healthy non-GPT cloud models
- preference for code-specialist paths where appropriate
- memory and route-history influence on selection
- budget-aware local preference when task pressure demands it

## Practical effect

The result of this work is that routing and model selection now behave like one system instead of two disconnected heuristics.

In runtime terms, this means:

- healthy non-GPT providers are real candidates
- dead or missing models can be excluded from selection automatically
- provider selection uses more than static defaults
- agent routing honors the provider decision instead of fighting it
- execution failures are more likely to represent real runtime issues rather than internal disagreement

