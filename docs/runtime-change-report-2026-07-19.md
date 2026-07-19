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


## Addendum: Local Workspace Changes on July 19, 2026

This addendum documents the uncommitted workspace changes that are present on July 19, 2026 in addition to the committed history described above.

Unlike the earlier sections, this part is intentionally closer to a file-by-file engineering explanation. The goal is to answer four practical questions for each change:

- what was changed
- what the new code or configuration does
- why the change is needed
- what an operator or developer should expect after it lands

## 16. Full observability stack added to the root deployment

Files touched:

- `docker-compose.yml`
- `infra/loki/loki-config.yaml`
- `infra/loki/promtail-config.yaml`
- `infra/prometheus/prometheus.yml`
- `infra/grafana/Dockerfile`
- `infra/rabbitmq/rabbitmq.conf`
- `infra/rabbitmq/enabled_plugins`
- `scripts/run-podman-stack.sh`

### What changed

The root deployment now includes a complete observability chain instead of only the application containers.

Four new services were added to `docker-compose.yml`:

- `loki` for log storage and querying
- `promtail` for scraping structured log files from the host-mounted runtime log directory
- `prometheus` for scraping metrics
- `grafana` for dashboards and operator inspection

RabbitMQ also gained mounted config files so the metrics plugin is enabled explicitly and Prometheus metrics are exposed on port `15692`.

The new infra files define the behavior of each service:

- `infra/loki/loki-config.yaml` configures single-binary Loki with local filesystem storage under `/loki`, an in-memory ring, and structured metadata support.
- `infra/loki/promtail-config.yaml` tells Promtail to watch `/var/log/runtime/go_core/*.log`, parse JSON log lines, promote fields such as `level`, `component`, `request_id`, `session_id`, `action`, and `status`, and ship the result to Loki.
- `infra/prometheus/prometheus.yml` scrapes `prometheus`, `go_core`, `loki`, `promtail`, and `rabbitmq`.
- `infra/grafana/Dockerfile` is deliberately minimal. It keeps the upstream Grafana entrypoint intact and only prepares the bundled plugin directory, which avoids the custom entrypoint/plugin cleanup behavior that caused unnecessary fragility earlier.
- `infra/rabbitmq/rabbitmq.conf` enables the RabbitMQ Prometheus listener.
- `infra/rabbitmq/enabled_plugins` enables `rabbitmq_management` and `rabbitmq_prometheus`.

### What this does

This change creates a usable debugging surface around the orchestrator:

- application logs become queryable in Loki
- metrics become queryable in Prometheus
- Grafana can sit on top of both for dashboards and ad hoc inspection
- RabbitMQ metrics can be scraped without extra manual setup

Before this change, an operator mostly had container stdout, ad hoc `podman logs`, and the raw `/metrics` endpoint. After this change, the stack can be inspected as a system rather than as isolated containers.

### Why it was needed

The recent runtime work added more asynchronous behavior, provider health loops, routing decisions, background publication, WebSocket traffic, and bootstrap branching. Once a system reaches that level of concurrency, plain startup logs are no longer enough.

This observability stack was needed so the operator can answer questions such as:

- Did the request fail in HTTP, WebSocket dispatch, or provider execution?
- Is `go_core` healthy but emitting repeated warnings?
- Are logs present even when the process has already recovered?
- Are metrics and logs telling the same story?

### Important behavioral details

The `go_core` service now mounts `./.runtime/logs/go_core` into `/var/log/go-core`. That mount is the bridge between the application logger and Promtail.

The compose file also adds explicit CPU, memory, and PID limits for the new observability services and for `go_core`. This matters because observability tooling can accidentally become the largest consumer on a small host if resource policy is not set up from the beginning.

## 17. Structured logging and an in-process diagnostics buffer

Files touched:

- `go-core/internal/observability/logger.go`
- `go-core/cmd/orchestrator/main.go`
- `go-core/internal/api/http.go`
- `go-core/internal/api/control.go`
- `go-core/internal/api/http_test.go`

### What changed

A new package, `go-core/internal/observability`, was added. It introduces a structured logging manager built on `log/slog`.

The package does three jobs at once:

1. It configures the process-wide logger.
2. It optionally mirrors logs to a file.
3. It captures recent log entries in a ring buffer for diagnostics.

The new `observability.Init("go_core")` call in `cmd/orchestrator/main.go` now runs at process startup before the orchestrator is bootstrapped. That guarantees that startup and bootstrap messages also go through the same structured logger.

The `Manager` stores a `slog.Logger` and a ring buffer. The custom `captureHandler` writes log entries to the real output handler and also stores a normalized copy in memory. That copy includes:

- UTC timestamp
- level
- message
- flattened structured fields

The package also exposes `Diagnostics(limit int)`, which is not just a raw log dump. It adds summary analysis:

- `level_counts`
- `component_counts`
- sampled error entries
- heuristic counters for timeouts, WebSocket failures, and provider or registry issues
- short recommendations derived from those patterns

### What this does

This gives the runtime a local memory of its own recent behavior. An operator no longer needs direct shell access to inspect only stdout. The process itself can now answer "what happened recently?" in a structured format.

The logger is also used as a bridge for legacy `log.Printf` style output through `stdlibBridge`, so older standard-library logging still flows into the same JSON/text pipeline instead of disappearing into a separate channel.

### Why it was needed

The existing diagnostics endpoint could describe state, but not the recent sequence of events that produced that state. That is a common blind spot in control-plane software: a snapshot alone does not explain a failure.

This change closes that gap by making recent runtime events inspectable without attaching to container logs in real time.

## 18. New diagnostics and access-log endpoints

Files touched:

- `go-core/internal/api/http.go`
- `go-core/internal/api/control.go`
- `go-core/internal/api/http_test.go`

### What changed

Two new diagnostics entrypoints were added:

- HTTP: `GET /diagnostics/logs?limit=<n>`
- transport action: `diagnostics.logs.get`

The HTTP handler validates `limit`, caps it to `5000`, augments the payload with request metadata, and returns the structured diagnostics report produced by the new observability package.

The transport dispatcher registers the same capability under `diagnostics.logs.get`, which means the diagnostics view is available both over ordinary HTTP and over the control transport used by runtime clients.

`http.go` also now wraps the entire router with `withAccessLog`. That middleware records:

- request ID
- method
- path
- query string
- status code
- duration in milliseconds
- response size
- remote address
- transport and caller metadata

The wrapper implements `WriteHeader`, `Write`, `Flush`, and `Hijack`. The `Hijack` support is especially important because WebSocket upgrade handlers require a response writer that still supports connection hijacking. Without that method, adding request logging would silently break the WebSocket path.

The middleware maps severity by status code:

- `INFO` for successful requests
- `WARN` for `4xx`
- `ERROR` for `5xx`

### What this does

This change turns the API layer into an observable surface instead of a black box.

Operators can now:

- inspect recent logs without shelling into the container
- correlate an HTTP failure with its request ID
- see response sizes and latencies
- use the same diagnostics capability from HTTP or transport clients

### Why it was needed

Once Loki and Promtail exist, the application still needs structured events worth collecting. Access logs and diagnostics logs are the base layer of that signal.

Without this work, the observability stack would exist, but it would mostly collect low-value startup noise rather than actionable request and runtime events.

## 19. Request tracing and response metadata propagation

Files touched:

- `go-core/internal/api/request_context.go`
- `go-core/internal/api/http_test.go`

### What changed

The request context metadata model now includes `RequestID`.

If the caller already supplies `X-Request-Id`, that value is preserved. If not, the server generates a new one with a timestamp prefix plus random entropy. The generated format is practical rather than decorative: it remains sortable by time while still avoiding easy collisions under concurrency.

The middleware now writes the request ID back to the response header and also injects it into JSON response payload metadata.

### What this does

This creates a single correlation key that connects:

- the inbound request
- the HTTP response
- access-log entries
- WebSocket lifecycle events
- diagnostics output

In practice this is one of the most valuable low-cost improvements in the whole patch. Once a user reports a failure, support can search by request ID instead of matching events by timestamp and guesswork.

### Why it was needed

The repository had started accumulating richer runtime and transport flows, but it still lacked a dependable correlation identifier. That becomes painful immediately when multiple requests or sessions are active at once.

## 20. WebSocket and dispatcher lifecycle logging

Files touched:

- `go-core/internal/api/control.go`

### What changed

The WebSocket and dispatch code path now emits structured lifecycle logs.

The new WebSocket logs cover:

- rejected handshakes
- failed upgrades
- connection opened
- connection closed
- failed bootstrap frame send
- heartbeat failures
- read failures
- parse failures
- accepted frames
- failed control frames

The dispatcher path now logs:

- request rejection when a duplicate or conflicting tracked request is seen
- dispatch start
- dispatch failure
- dispatch completion

### What this does

This instrumentation exposes the full path from accepted socket to dispatched action. That makes it much easier to distinguish between:

- transport-level protocol problems
- malformed envelopes
- control-frame handling issues
- downstream dispatch failures

### Why it was needed

The orchestrator already had a WebSocket audit ring, but the audit mechanism stores frames and metadata, not human-oriented lifecycle messages. The new logs complement the audit trail by making failures visible in ordinary diagnostics tools and in Loki queries.

## 21. Safe bootstrap mode and reduced startup side effects

Files touched:

- `go-core/cmd/orchestrator/main.go`
- `go-core/internal/kernel/bootstrap.go`
- `go-core/internal/kernel/bootstrap_mode.go`
- `go-core/internal/kernel/orchestrator.go`
- `go-core/internal/state/open.go`
- `go-core/internal/state/postgres_store.go`

### What changed

This patch introduces and propagates `GO_CORE_BOOTSTRAP_SAFE_MODE`, enabled by default in the stack configuration.

At the process entrypoint level, `main.go` now:

- logs a startup summary through `slog`
- reports resolved database settings in a structured way
- fails fast with a clearer error if PostgreSQL connection configuration is missing
- skips `DBProtector.EnsureProtected` and its background watcher when safe mode is enabled

In `kernel/bootstrap.go`, safe mode short-circuits runtime bootstrap after the core orchestrator object and base modules are registered. That means expensive or optional runtime managers are not started during a "bring the box up safely" bootstrap.

In `kernel/orchestrator.go`, safe mode disables several background behaviors:

- provider registry startup
- self-learning configuration
- submission workers
- result workers
- agent worker pools
- full inventory publication

The inventory publisher also changes behavior under safe mode. Instead of pretending that the full runtime inventory is available, it publishes a reduced snapshot that explicitly reports `bootstrap_safe_mode`.

In `state/postgres_store.go`, safe mode reduces default PostgreSQL connection pool sizes and changes schema handling:

- lower `max_open_conns`
- lower `max_idle_conns`
- optional skip of schema ensure
- shorter schema timeout
- optional skip of `CREATE EXTENSION vector`
- optional skip of vector index creation

In `state/open.go`, the "database store is required" error message now includes the resolved host, port, database, and user. That is small but important: configuration errors become diagnosable without reading env resolution code.

### What this does

Safe mode changes startup philosophy from "fully activate every subsystem immediately" to "get the control plane online predictably, then expand only when explicitly configured."

That is exactly the right tradeoff for constrained hosts, fragile bootstrap environments, and partially prepared databases.

### Why it was needed

The runtime had become operationally ambitious. On a small local Podman stack, that meant bootstrap could spend CPU, memory, and connection budget on work that is helpful later but unnecessary for the initial healthy state.

Safe mode reduces blast radius:

- fewer background loops
- less aggressive database initialization
- lower connection pressure
- more predictable health during early startup

## 22. Compose and Podman bootstrap aligned with the real root stack

Files touched:

- `go-core/internal/ops/compose.go`
- `go-core/internal/ops/db.go`
- `go-core/internal/ops/preflight.go`
- `scripts/run-podman-stack.sh`

### What changed

The operational tooling now treats `docker-compose.yml` as the primary stack definition instead of the older `docker-compose.ai.yml`.

`compose.go` now:

- defaults to `docker-compose.yml`
- keeps a fallback path for callers that still mention the legacy compose filename
- starts `postgresql` instead of the non-existent `db` service name
- mounts RabbitMQ config when the direct Podman fallback path is used

`db.go` and `preflight.go` were updated so error messages and remediation instructions point to the root compose file that actually exists.

### What this does

This removes a class of operator confusion where the code, scripts, and docs referred to different compose files or different service names for the same stack.

### Why it was needed

Bootstrap tooling is the first thing an operator uses. If it points to stale filenames, every later improvement is harder to benefit from because the stack fails before the application is even running.

## 23. `run-podman-stack.sh` became the real operations control surface

Files touched:

- `scripts/run-podman-stack.sh`
- `scripts/triage-observability.sh`

### What changed

The Podman stack script was expanded from a startup helper into a more complete local operations interface.

The script now:

- defines container and volume names for Loki, Promtail, Prometheus, and Grafana
- defines images and resource defaults for the whole observability stack
- defines a large set of `GO_CORE_*` runtime guardrail variables
- builds the Grafana image
- runs Loki, Promtail, Prometheus, and Grafana
- mounts log directories for `go_core`
- prints observability endpoints after startup
- adds a `bootstrap` alias for `up`
- adds a `triage` command that delegates to `scripts/triage-observability.sh`
- expands `status` with service health checks and resource policy output
- expands `diagnose` with diagnostics logs, realtime metrics, Prometheus queries, Loki readiness, and Grafana health checks

The script also changes how `go_core` is launched:

- it runs with explicit CPU, memory, reservation, and PID limits
- it passes bounded concurrency settings into the container
- it passes logging configuration so logs are written as JSON into the mounted runtime log directory
- it disables or narrows several optional runtime behaviors by default for local bootstrap

`scripts/triage-observability.sh` is a compact incident-response helper. It fetches:

- `go_core` health
- `go_core` diagnostics logs
- realtime metrics
- Loki, Prometheus, and Grafana health
- Prometheus queries for service liveness and selected realtime counters
- Loki queries for `warn` and `error` logs and for `5xx` HTTP events

### What this does

This gives the repository a repeatable operator workflow:

- bring the stack up
- inspect health
- inspect recent logs
- inspect metrics
- run a standard triage sweep

That is much better than expecting every developer to remember a long series of `podman`, `curl`, Loki, and Prometheus commands.

### Why it was needed

Observability tooling only matters if operators can reach it quickly under pressure. The script changes turn the new stack into something that is actually usable during a failure or verification session.

## 24. Test coverage added specifically for the new operational path

Files touched:

- `go-core/internal/api/http_test.go`
- `go-core/internal/ops/observability_infra_test.go`

### What changed

The tests now validate the new runtime and operator contract directly.

`http_test.go` adds coverage for:

- a successful WebSocket upgrade through the real server handler
- response header and payload propagation of `X-Request-Id`
- successful retrieval of recent diagnostics log entries

These tests matter because they verify integration points rather than just isolated helper functions. For example, the WebSocket test proves that the new access-log middleware did not break connection hijacking.

`observability_infra_test.go` adds repository-level guardrails:

- root `docker-compose.yml` must mount RabbitMQ metrics config
- `run-podman-stack.sh` must mount the same RabbitMQ config
- Grafana provisioning must not rely on placeholder `.keep` files
- the Grafana Dockerfile must not override the upstream entrypoint
- the Loki config must include the local ring and filesystem storage paths needed by the chosen single-binary setup

### What this does

These tests lock in the behavior of the new operational model. They are there to stop accidental regression in areas that are easy to break quietly:

- infra mounts
- WebSocket upgrade compatibility
- request correlation
- diagnostics endpoint visibility

### Why it was needed

Operational regressions are often not caught by business-logic tests. They only show up when the stack is deployed and someone tries to debug a live issue. These tests shift that discovery earlier into CI and local verification.

## Operational reading of the July 19 workspace diff

If you want the short, practical interpretation of the uncommitted July 19 diff, it is this:

- the stack now favors safe startup over aggressive initialization
- the runtime now emits structured logs that can be queried and summarized
- request and WebSocket flows now have correlation-friendly tracing
- local Podman operations now include a complete observability workflow
- infra configuration is moving away from ad hoc defaults toward explicit, test-backed contracts

That is a meaningful operational upgrade, not just a refactor. It makes the orchestrator easier to start, easier to inspect, and easier to debug under real conditions.
