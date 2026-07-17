# Changelog

## Unreleased

### Documentation and repository structure

- Expanded the root `README.md` and the `docs/` set so the repository-level documentation now explains the current planner, routing, memory, import, and deployment behavior in detail.
- Kept the repository root as the single source of truth for public-facing documentation instead of reintroducing duplicate copies under `go-core`.

### Planning, execution, and checkpoints

- Split parallel-plan persistence into static and runtime checkpoints.
- Added resume support for saved execution plans so long-running or interrupted parallel workflows can be continued without rebuilding the full plan graph.
- Reworked parallel execution from batch barriers to incremental scheduling and result consumption.
- Added conflict-aware scheduling through artifact conflict keys so related tasks do not run concurrently when they share sensitive resources.
- Propagated richer plan artifact metadata into execution contracts and routing hints, including worker class, cluster id, context budget, conflict keys, and task weight.
- Added dynamic step-routing support so analysis and planning steps can intentionally defer provider and model binding until execution time.
- Fixed branch-key derivation in orchestrator routing metadata so missing routing hints no longer collapse into false `"<nil>"` grouping keys.

### Runtime routing and model selection

- Expanded the provider model registry with confirmation TTL, pending TTL, retry cooldown, queue limits, and stale-snapshot gating.
- Added asynchronous provider probing and scheduled refresh behavior for inventory verification and degraded-provider recovery.
- Tightened routing eligibility so usable models now depend on inventory availability, transport health, verification status, and snapshot freshness instead of a simple presence check.
- Added runtime capacity snapshots, slot usage tracking, and pressure-aware routing weights.
- Extended routing readiness checks to handle fixed-model agents, dynamically overridable agents, and confirmed provider-model mappings more explicitly.
- Improved provider health synchronization so providers can be marked unavailable, pending, degraded, or cooling down for concrete operational reasons.

### Memory, reasoning traces, and self-learning

- Expanded retrieval to combine session vector chunks, global vector fallback, RAG memories, and RAG documents under a larger token budget.
- Added reasoning-trace persistence in memory so prior decision paths can be retrieved alongside ordinary context.
- Exposed reasoning-memory summaries and reasoning-trace hit counts in loaded memory context.
- Added domain contracts for self-learning, preference generation, fine-tuning jobs, reasoning engines, trace recording, and model hot reload operations.
- Added a first-class reasoning trace contract in the domain layer for persisted decision points, retrieval usage, latency, and trace metadata.

### API and transport

- Expanded provider inventory payloads with probe queue state, cooldown timing, refresh timing, richer status reasons, and collaboration metadata for the `ai_kernel` provider.
- Expanded the model index payload with inventory, transport, verification, latency, error, and last-success metadata for each model or variant.
- Added explicit `pending` readiness behavior for live model probes and AI-kernel gating.
- Refined merged model-status ranking to distinguish queued, pending, stale, cooldown, verification, transport, and registration failure states.
- Added runtime verification coverage for the WebSocket control handshake and synthetic runtime workflow profiling.

### Import, persistence, and operations

- Added `import-core-sql` for importing legacy SQL-backed runtime data into the current state and memory model.
- Added `import-fable-traces` for ingesting trace datasets into RAG documents and reasoning-trace memory.
- Added managed database protection with periodic JSON snapshots, snapshot pruning, and restore-on-empty behavior.
- Expanded AI-kernel provisioning and service support for local `llama-cpp-python` serving, model download, and service installation.
- Expanded bootstrap behavior so the runtime can prepare local model infrastructure, AI-kernel service state, container runtime dependencies, and database protection more consistently.

### Packaging and deployment

- Updated the orchestrator container packaging.
- Added a root `docker-compose.yml` that defines the local orchestration stack and external chat gateway.
- Rebuilt and validated the runtime container against the live `127.0.0.1:8010` health and WebSocket path.
