# Release Summary: Adaptive Routing, Analytics Runtime, and Bwrap Recovery

## Scope

This change set moves the core runtime away from a simple transport-first execution model and toward a provider-aware orchestration layer with adaptive routing, model health tracking, analytics-driven context signals, and explicit recovery guidance for runtimes that cannot create `bubblewrap` user namespaces.

## Core Runtime Changes

### Adaptive routing and provider selection

The runtime now builds model decisions from provider inventory, endpoint capability probing, and persisted model-health data instead of relying only on static defaults.

Key additions:
- `core/core/adaptive_routing_engine.py`
- `core/core/model_health_registry.py`
- expanded `core/core/provider_inventory_service.py`
- updated `core/core/model_selector.py`
- updated `core/core/openai_runtime_router.py`
- updated `core/core/openai_compatible_inventory.py`
- updated `core/core/distributed_coding_planner.py`

Behavioral impact:
- primary and fallback model selection now use provider visibility, workability, route readiness, role suitability, and recent runtime health
- parallel work can be assigned per lane with explicit provider and model hints
- provider inventory snapshots now record endpoint capabilities, failure reasons, recommendations, and model participation details
- generated MIMO manifests and OpenAI-compatible templates now reflect the new routing inventory

### Orchestrator hardening and richer branch contracts

`core/core/orchestrator.py` now carries more runtime state and stronger failure controls.

Key additions and updates:
- `core/core/agent_loop_guard.py`
- updated `core/core/orchestrator.py`
- updated `core/core/availability.py`
- updated `core/core/task_decomposer.py`
- updated `core/core/orchestrator_transport.py`

Behavioral impact:
- repeated identical handoffs can be suppressed before the system loops indefinitely
- repeated identical failures can be quarantined and surfaced as runtime guard signals
- handoff payloads now include branch goals, execution contracts, acceptance-criteria deltas, verification evidence, required follow-ups, and risk flags
- runtime availability and fallback handling now distinguish transient provider failure from blocked or degraded routes more explicitly

## Analytics and Data Intelligence

### Runtime analytics signals

The runtime can now derive operational analytics from memory and storage state and feed those signals back into routing.

Key additions:
- `core/core/data_analytics_module.py`
- `core/core/data_storage_analytics.py`
- `core/scripts/storage_analytics_report.py`

Behavioral impact:
- task routing hints can include freshness, retention, search readiness, retrieval readiness, and orchestrator confidence
- runtime state can publish analytics-ready and degraded-policy signals into the event stream
- operators can generate a storage analytics report outside the main orchestrator loop

### Prompt intelligence and matrix extraction

The runtime now includes additive task-text analysis that extracts reusable prompt and retrieval structures.

Key additions:
- `core/core/data_intelligence_module.py`
- `core/core/analytics_matrix_engine.py`
- `core/core/analytics_matrix_orchestration.py`
- `core/core/analytics_coding_orchestration.py`
- `core/agents/data_analytics_matrix_agent.py`

Behavioral impact:
- task descriptions can be transformed into keyword, phrase, sentence-link, template, and character-matrix artifacts
- analytics-heavy work can trigger a specialized multi-agent decomposition wave
- matrix reports can be stored in an in-memory knowledge pool for later reuse

Supporting documentation:
- `docs/ANALYTICS_MATRIX_AGENT_RUNBOOK.md`

## Runtime Recovery for Bwrap-Blocked Environments

This repository now documents and scripts a supported recovery path for environments where user namespace sandboxing is blocked by runtime policy.

Key additions:
- `docs/BWRAP_RUNTIME_SELECTOR.md`
- `docs/BWRAP_RUNTIME_RECOVERY_RUNBOOK.md`
- `scripts/runtime/run_bwrap_aware_agent.sh`
- runtime-specific Docker and Podman launch helpers under `scripts/runtime/`
- `docker-compose.ai.unconfined.override.yml`

Behavioral impact:
- operator workflows now have a documented probe-first path before escalating to unconfined or privileged container runtimes
- preflight checks can validate `unshare`, `bwrap`, and temporary file mutation in the same runtime profile
- container startup documentation now reflects the practical constraints seen on immutable Fedora-family hosts and similar environments

## Tests and Validation

The change set adds or expands targeted coverage for the new runtime paths, including:
- provider inventory probing and runtime routing
- orchestrator loop suppression and richer branch metadata
- analytics modules and analytics matrix orchestration
- preflight core validation

Representative files:
- `core/test/test_provider_inventory_service.py`
- `core/test/test_openai_runtime_router.py`
- `core/test/test_orchestrator.py`
- `core/test/test_data_analytics_module.py`
- `core/test/test_data_intelligence_module.py`
- `core/test/test_analytics_matrix_engine.py`
- `core/test/test_analytics_matrix_orchestration.py`
- `core/test/test_analytics_coding_orchestration.py`
- `core/test/test_preflight_core_suite.py`

## Documentation Updates

The high-level documentation was revised to describe the live runtime instead of the older transport-centric design:
- `docs/SYSTEM_OVERVIEW.md`
- `docs/RUNTIME_CHANGES_AND_MIGRATION_NOTES.md`
- `core/README.md`

## Recommended Reading Order

1. `docs/SYSTEM_OVERVIEW.md`
2. `docs/RUNTIME_CHANGES_AND_MIGRATION_NOTES.md`
3. `docs/RELEASE_SUMMARY_ADAPTIVE_ROUTING_ANALYTICS_AND_RUNTIME_RECOVERY.md`
4. `docs/ANALYTICS_MATRIX_AGENT_RUNBOOK.md`
5. `docs/BWRAP_RUNTIME_SELECTOR.md`
6. `docs/BWRAP_RUNTIME_RECOVERY_RUNBOOK.md`
