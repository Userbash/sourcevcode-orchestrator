# Runtime Change Report: July 16-19, 2026

## Scope

This document explains the committed repository changes that landed during the last three days before July 19, 2026.

Covered commits:

- `b77e9b0` on `2026-07-16 14:12:37 +0500` - `feat: add versioned go-core rollout and verification pipeline`
- `a16b965` on `2026-07-17 23:12:11 +0500` - `feat: expand orchestrator runtime, memory, and ops flows`

This report is based on committed `git` history only. It does not describe uncommitted local workspace changes.

## Why this report exists

The diff for these two commits is large. Reading the raw patch is useful if you need line-level code review, but it is not the best format for understanding intent.

This report translates the raw diff into human-readable engineering documentation:

- what changed
- what each change does
- why the change was needed
- what operational or development problem the change solves
- what effect the change has on the running orchestrator

## Executive Summary

The three-day change window introduced two major waves of work.

The first wave established a safer operational foundation for `go_core`:

- versioned images and rollout pinning
- embedded build metadata
- runtime verification tooling
- richer release notes and test automation

The second wave expanded the orchestrator itself:

- broader runtime routing controls
- deeper memory and retrieval behavior
- provider registry and probe flow changes
- new import and self-learning domain surfaces
- stronger runtime observability and state reporting
- more complete transport, delivery, and API integration paths

In practical terms, the orchestrator moved from a functional control plane to a more inspectable, more operationally managed, and more adaptive runtime.

## Commit-by-Commit Overview

## `b77e9b0`: versioned rollout and verification foundation

This change set focused on deployment discipline, runtime identity, and validation.

### What changed

- Added explicit build metadata support to `go_core`.
- Added a runtime verifier binary and wrapper script.
- Added a repeatable test script for end-to-end validation.
- Consolidated scripts into the root `scripts/` tree.
- Improved runtime metadata exposure over API and WebSocket.
- Expanded routing and catalog logic so runtime selection can use fresher provider information.

### Why it matters

Before this commit, it was harder to answer operationally important questions:

- Which exact binary is running?
- Is the WebSocket surface attached to the same runtime build as HTTP?
- Has the candidate image been verified before rollout?

This commit addressed those gaps by making build identity first-class and by formalizing verification.

## `a16b965`: runtime, memory, and operational expansion

This change set focused on deep runtime behavior rather than rollout mechanics.

### What changed

- Expanded orchestrator runtime flows and runtime state reporting.
- Added more detailed provider inventory, model registry, and probe behavior.
- Added new memory and retrieval paths, including reasoning-oriented memory handling.
- Added self-learning contracts and supporting domain structures.
- Added import tooling for legacy or external data sources.
- Increased API and transport coverage around runtime control, model state, and chat flow.
- Added more tests around planner behavior, runtime execution, provider state, and API responses.

### Why it matters

This commit made the orchestrator better at answering runtime questions in real time:

- What providers exist right now?
- Which models are merely configured and which are actually verified?
- Why did the selector choose a route?
- What memory influenced the decision?
- How healthy is the runtime under pressure?

It also prepared the system for richer memory, learning, and long-lived operational flows.

## Detailed Change Breakdown

## 1. Documentation and operator guidance

Files touched included:

- `README.md`
- `CHANGELOG.md`
- `docs/api-and-transport.md`
- `docs/architecture.md`
- `docs/deployment-and-publication.md`
- `docs/runtime-release-notes-2026-07-16.md`
- `docs/runtime-routing-and-model-selection.md`

### What changed

The documentation was expanded from a basic project description into a real operator-facing runtime manual.

The updated docs now describe:

- orchestrator responsibilities
- runtime architecture and layer boundaries
- task lifecycle and routing flow
- model selection and provider inventory behavior
- memory, retrieval, and reasoning trace usage
- rollout, verification, and publishing workflow
- transport and WebSocket behavior
- external chat relay topology

### What this does

These changes reduce the need to reverse-engineer runtime behavior from source code alone. A developer or operator can now understand the intended control-plane behavior directly from the docs.

### Why it was needed

The codebase had grown beyond the point where a short README was enough. The orchestrator now includes planning, routing, delivery, memory, provider discovery, verification, and runtime inventory. Without documentation updates, the code and the operator mental model drift apart.

## 2. Script consolidation and operational entrypoints

Files touched included:

- removed or migrated content from `script/`
- new and updated content under `scripts/`
- `scripts/build-go-core-image.sh`
- `scripts/run-podman-stack.sh`
- `scripts/test-go-core.sh`
- `scripts/verify-orchestrator.sh`

### What changed

The repository standardized on a single `scripts/` directory for runtime operations.

New scripts now cover:

- deterministic `go_core` image building
- versioned runtime rollout control
- runtime verification
- full orchestrator test execution
- compose or podman stack management

### What this does

This change turns previously manual or scattered operational work into named commands with stable behavior.

Examples:

- building a runtime image now also carries version metadata
- restarts can follow a pinned image reference
- verification can be exported in one command
- pre-rollout validation can be run consistently by different operators

### Why it was needed

Large runtimes fail when build, test, and deploy flows are ambiguous. The script consolidation reduces operational drift, avoids duplicate helper trees, and makes the rollout path reproducible.

## 3. Versioned `go_core` rollout and build identity

Files touched included:

- `go-core/Dockerfile`
- `go-core/internal/buildinfo/*`
- `go-core/cmd/orchestrator/*`
- rollout handling in `scripts/run-podman-stack.sh`

### What changed

The runtime binary and image now embed:

- `version`
- `commit`
- `build_time`

The image build also publishes matching OCI labels. The stack tooling stores the active image reference so restarts and status checks can target the same build intentionally instead of implicitly.

### What this does

This gives the runtime an identity that survives across:

- container restarts
- image rebuilds
- HTTP checks
- WebSocket checks
- verifier exports

An operator can now compare the running process, the built image, and the chosen rollout target without guesswork.

### Why it was needed

When a WebSocket problem, routing regression, or warmup issue appears, the first question is always "which build is live?" This change makes that answer available programmatically.

## 4. Runtime verifier and validation pipeline

Files touched included:

- `go-core/cmd/verify-orchestrator/main.go`
- `scripts/verify-orchestrator.sh`
- `scripts/test-go-core.sh`
- verification tests under `go-core/internal/api` and `go-core/internal/kernel`

### What changed

The repository gained a dedicated verifier that exports a machine-readable runtime profile.

The verification path now captures:

- goroutine counts
- heap and allocation snapshots
- GC activity
- execution profile metrics
- delivery snapshots
- state snapshots
- agent KPIs
- coordinator KPIs

The automated test script also expanded to run:

- unit and package tests
- coverage
- API smoke coverage
- orchestrator end-to-end checks
- verifier checks
- race detection for kernel code

### What this does

This creates a formal pre-rollout gate. Instead of trusting that the runtime "seems fine", the repository can now produce structured evidence of runtime health and behavior.

### Why it was needed

As the runtime gained planner parallelism, richer provider state, and more delivery paths, passive confidence was no longer enough. Verification had to become explicit.

## 5. API surface expansion and richer runtime payloads

Files touched included:

- `go-core/internal/api/control.go`
- `go-core/internal/api/http.go`
- `go-core/internal/api/runtime_payloads.go`
- multiple API tests

### What changed

The API layer now exposes richer runtime payloads and more consistent metadata.

The new or expanded surfaces include:

- fuller provider inventory output
- richer model index payloads
- runtime routing weights
- agent and provider runtime state
- kernel build version visibility
- stronger transport and chat envelope assertions

The WebSocket bootstrap path also gained clearer initial metadata behavior, including `kernel.version` bootstrap information.

### What this does

Clients, operators, and test harnesses can inspect more of the orchestrator's internal truth without attaching a debugger or reading database state directly.

### Why it was needed

The orchestrator is the source of truth for external chat and runtime routing. If its own state cannot be queried clearly, debugging transport failures becomes slow and error-prone.

## 6. Planner hardening and workflow terminal-state correctness

Files touched included:

- `go-core/internal/kernel/advanced_planner.go`
- planner tests

### What changed

The planner's interpretation of terminal workflow states was tightened.

Plans now succeed only when all relevant workflows reach successful terminal states. A failed, rejected, or otherwise unsuccessful terminal result can no longer be silently treated as a completed success path.

### What this does

This removes a class of false-positive completion states in multi-step or dependent plans.

### Why it was needed

In orchestration systems, "completed" and "succeeded" are not equivalent. Treating terminal failure as normal completion corrupts higher-level workflow logic and leads to misleading final responses.

## 7. Delivery snapshot safety and observer isolation

Files touched included:

- delivery-related runtime code and tests in `go-core/internal/delivery`

### What changed

Delivery records are now cloned before they are refreshed, snapshotted, or surfaced to health and reporting paths.

### What this does

This reduces accidental shared-state mutation when observer code inspects delivery data while runtime code is also updating it.

### Why it was needed

Observer paths should not be able to corrupt active runtime state. This is especially important in a concurrent Go runtime where read paths can become hidden mutation paths if slices or maps are shared carelessly.

## 8. Provider registry, model catalog, and live inventory management

Files touched included:

- `go-core/internal/kernel/model_catalog.go`
- `go-core/internal/kernel/model_catalog_test.go`
- `go-core/internal/kernel/model_registry.go`
- `go-core/internal/kernel/provider_probe_manager.go`
- `go-core/internal/kernel/runtime_manager.go`
- related configuration fixtures and tests

### What changed

The provider and model management layer became much more explicit.

The runtime now supports:

- live model inventory refresh
- model verification states
- freshness and staleness tracking
- cooldown behavior after failed registration attempts
- synchronization between configured catalog data and discovered runtime inventory
- richer provider health semantics beyond simple alive/dead checks

### What this does

This separates several concepts that used to be easy to blur together:

- configured model
- discovered model
- verified model
- fresh enough model
- temporarily cooling-down model
- degraded but still partially usable provider

That separation is critical when the runtime chooses a model dynamically.

### Why it was needed

Without these distinctions, the selector can make overly optimistic decisions and the router can send work to a provider that is known to exist on paper but is not actually ready at runtime.

## 9. Selector-router alignment and dynamic step routing

Files touched included:

- `go-core/internal/kernel/model_selector.go`
- `go-core/internal/kernel/router.go`
- `go-core/internal/kernel/orchestrator.go`
- selection and routing tests

### What changed

The selector and router were brought into closer alignment so the provider chosen during selection remains meaningful during actual agent binding and execution.

The runtime also improved support for:

- late provider binding
- step-level routing
- runtime-aware route adjustment
- worker class and pressure-sensitive route weighting

### What this does

This reduces split-brain behavior where one subsystem decides on provider A and another quietly executes on provider B.

### Why it was needed

For external chat and orchestrated task execution, consistency matters. A route decision is only useful if downstream execution respects it.

## 10. Runtime manager pressure, capacity, and suppression behavior

Files touched included:

- `go-core/internal/kernel/runtime_manager.go`
- `go-core/internal/kernel/runtime_manager_test.go`

### What changed

Runtime weighting became more sensitive to live operating conditions.

The manager now accounts more explicitly for:

- worker-class pressure
- slot usage
- failure rate
- suppression state
- runtime inventory quality
- provider degradation

### What this does

This lets routing decisions respond to runtime reality instead of relying only on static provider preferences.

### Why it was needed

The orchestrator is a live scheduler. Static scoring alone is not enough once the runtime is under pressure, in cooldown, or recovering after errors.

## 11. Memory enrichment, retrieval-aware routing, and reasoning memory

Files touched included:

- `go-core/internal/memory/manager.go`
- `go-core/internal/memory/retrieval_agent.go`
- `go-core/internal/memory/reasoning_memory.go`
- `go-core/internal/memory/embeddings.go`
- memory and routing tests

### What changed

The memory system became more than plain vector retrieval.

The runtime now combines more sources when building task context:

- session-local vector chunks
- route memory
- RAG documents
- RAG memories
- reasoning traces represented as reusable memory
- retrieval KPIs and context signals

Routing decisions can also incorporate retrieval-aware signals rather than ignoring the cost and quality of memory loading.

### What this does

This produces a richer execution brief for models and agents while also making the selector aware of memory pressure and context quality.

### Why it was needed

Tasks that depend on prior reasoning, review comments, or route-specific history cannot be served well by a naive text-only retrieval layer. The runtime needed memory that reflects how the system actually works, not just where text was stored.

## 12. Self-learning domain contracts and future-facing runtime interfaces

Files touched included:

- new files under `go-core/internal/selflearn/`
- supporting domain contracts under `go-core/internal/domain`

### What changed

The repository introduced domain structures for self-learning and model-evolution workflows.

These contracts cover concepts such as:

- reasoning requests
- code evaluation
- preference and trace data
- fine-tuning job description
- model discovery
- model hot reload or replacement hooks

### What this does

These files do not train a model by themselves. Instead, they define stable boundaries so future learning or adaptive runtime features can plug into the orchestrator without ad-hoc contracts.

### Why it was needed

Long-lived orchestration systems eventually need structured interfaces for learning from prior runs. It is better to establish those contracts deliberately now than to bolt them on later through fragile one-off payloads.

## 13. Import, bootstrap, and operational data migration flows

Files touched included:

- `go-core/cmd/orchestrator/import_core_sql.go`
- `go-core/cmd/orchestrator/import_fable_traces.go`
- state and import support code

### What changed

New import commands were added for bringing historical or external data into the runtime.

This includes support for:

- importing legacy SQL-backed runtime state
- importing trace-style data for memory or reasoning use

### What this does

This reduces the cost of moving into the current orchestrator architecture from earlier systems or sidecar data stores.

### Why it was needed

Operational adoption is much easier when prior data can be migrated instead of discarded.

## 14. Transport, external chat, and runtime truth alignment

Files touched included:

- API transport tests
- relay scripts
- WebSocket-oriented runtime behavior

### What changed

The transport layer gained stronger expectations around bootstrap metadata, message envelopes, and runtime behavior visible to relay clients.

This included test coverage for WebSocket bootstrap frames and stronger assertions around runtime identity.

### What this does

External chat integrations can now rely on a more explicit startup contract:

- the runtime identifies itself
- the transport layer exposes a stable protocol shape
- test harnesses can detect regressions earlier

### Why it was needed

When the external chat is only a proxy and the orchestrator is the real source of truth, every ambiguity in transport bootstrapping makes diagnosis harder. Clearer startup frames reduce that ambiguity.

## 15. Test coverage growth and regression locking

Files touched included many tests across:

- `go-core/internal/api`
- `go-core/internal/kernel`
- `go-core/internal/memory`
- `go-core/internal/state`
- transport and runtime management areas

### What changed

The repository added or expanded tests in the areas most likely to regress:

- planner terminal-state handling
- runtime inventory payloads
- model catalog loading and sync
- routing behavior
- memory enrichment
- WebSocket bootstrap semantics
- runtime manager pressure logic
- provider inventory and health

### What this does

These tests convert behavioral assumptions into executable checks.

### Why it was needed

The more dynamic the runtime becomes, the more it needs regression coverage to stop subtle routing and state bugs from resurfacing.

## Operational Impact

## What is better after these changes

- Operators can identify the exact live `go_core` build.
- Rollouts can be pinned, verified, and described consistently.
- API and WebSocket surfaces expose more runtime truth.
- Routing is more aware of provider freshness, degradation, and runtime pressure.
- Memory loading is richer and more relevant to reasoning-heavy tasks.
- Migration and import paths make it easier to carry forward historical data.
- Documentation is much closer to the actual runtime behavior.

## What became more complex

- Provider lifecycle is now more nuanced than simple health checks.
- Model registration and validation have more states and more operational knobs.
- Runtime selection depends on more live signals, which is more powerful but also harder to reason about without the new docs and verifier.

## What operators should pay attention to

- provider cooldown loops and refresh intervals
- model validation frequency
- runtime pressure and slot saturation
- delivery and state snapshot behavior under concurrency
- memory growth caused by richer retrieval and reasoning traces

## Known limits of this report

This document explains the intent and effects of the changes, but it is not a line-by-line patch commentary for all modified files.

That is intentional.

The diff spans a large number of files and several subsystems. A literal file-by-file rewrite of every hunk would be less readable and less useful than a subsystem-level engineering explanation.

If a deeper review is needed, the recommended follow-up is:

1. use this report to identify the subsystem of interest
2. inspect the corresponding file group in `git show b77e9b0` or `git show a16b965`
3. review the matching tests to confirm intended runtime behavior

## Recommended reading order

If you are new to the current runtime, read the material in this order:

1. `README.md`
2. `docs/architecture.md`
3. `docs/api-and-transport.md`
4. `docs/runtime-routing-and-model-selection.md`
5. `docs/runtime-release-notes-2026-07-16.md`
6. this report

That sequence moves from high-level architecture to concrete runtime behavior and finally to the specific changes made during this three-day window.
