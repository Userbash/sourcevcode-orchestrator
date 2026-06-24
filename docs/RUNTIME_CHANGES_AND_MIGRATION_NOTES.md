# Runtime Changes and Migration Notes

This document explains how the current runtime differs from older documentation and older code slices.

It is written for engineers who need a clear answer to a simple question: what changed, what stayed, and what should be considered legacy now.

## Summary

The system moved from a broad, compatibility-heavy orchestration layer toward a more explicit engineering runtime.

The current code puts more weight on structured intake, dependency-aware planning, async execution, parallel code fan-out, memory validation, and observable final state. Older descriptions often focused on bridge compatibility, generalized chat transport, or narrow linear pipelines. Those pieces are no longer the right mental model for the live path.

## Old picture versus current picture

### Before

Older documents often described the runtime like this:

- task enters the system
- planner creates a mostly linear sequence
- one main coding lane performs implementation
- test and review happen afterward
- memory is attached in a relatively loose way
- compatibility paths are described almost as first-class behavior

That picture was not completely wrong, but it is no longer complete enough.

### Now

The runtime now behaves more like this:

- input is normalized before routing
- task shape influences decomposition strategy
- larger code work can fan out across multiple agents
- branch families carry explicit task metadata and memory profiles
- downstream tasks wait for the full branch family at fan-in points
- runtime memory is assembled through dedicated modules
- validation-memory warmup, consensus, and conflict state are surfaced in module state
- provider fallback and budget decisions are part of the observable runtime path

## Concrete code changes reflected in the current runtime

### 1. Input normalization became a first-class step

The system now builds a normalized task profile early and carries that profile into routing, decomposition, and prompt preparation.

Why it matters:

- noisy user text no longer distorts the whole plan
- provider selection can respond to risk and confidence more consistently
- decomposition can react to execution shape instead of relying only on task type

### 2. Parallel code execution is now a real orchestration feature

The runtime can split suitable code work into multiple branches and assign those branches to distinct agents.

Why it matters:

- one large coding task no longer has to flow through one agent path
- branch diversity can improve speed and resilience
- routing can spread work across different providers or model families

### 3. Fan-in behavior was corrected

Older task splitting logic could leave downstream work effectively tied to only part of a branch group. The current implementation keeps branch-family dependencies explicit and waits for the whole family at the merge point.

Why it matters:

- review no longer starts before the complete implementation set exists
- test and merge stages see the full branch surface
- the DAG now reflects the real execution contract

### 4. Memory handling became modular and visible

The runtime now uses dedicated modules to build context, warm memory, score consensus, and record conflicts.

Why it matters:

- memory behavior is easier to inspect
- execution context is less ad hoc
- operators can see whether memory was warmed, whether validation agreed, and whether the runtime is under pressure

### 5. `memory_warmup_report` is part of final module state

The final module snapshot now includes a compact report for validation-memory warmup and conflict state.

It includes signals such as:

- warmup totals
- parallel batch count
- conflict count
- consensus count
- local model warmup count
- memory pressure state
- the latest conflict snapshot

Why it matters:

This turns memory from a hidden implementation detail into an operational signal.

### 6. Delivery supervision is now part of the normal local-agent path

The orchestrator no longer depends on a loose direct-call model for local execution. It uses a delivery supervisor and mailbox-style handoff path.

Why it matters:

- timeout behavior is more consistent
- handoff state is easier to inspect
- local execution supervision is closer to the rest of the runtime protocol

## Legacy pieces that should not guide new work

The following categories should be treated carefully:

- compatibility aliases kept only for older imports
- old bridge-first descriptions that ignore the current orchestration core
- stale files that are no longer imported anywhere
- broad ecosystem documents that describe components no longer present in the active repository path

## Removed stale runtime code

The repository previously carried a dead legacy router stub in `core/core/models/model_router.py`. It was not imported by the runtime or by tests, and it was marked in the file itself as stale. That file has been removed.

## How to read the current repository

If you want the most accurate picture of the live system, read in this order:

1. `docs/SYSTEM_OVERVIEW.md`
2. `README.md`
3. `core/core/orchestrator.py`
4. `core/core/task_decomposer.py`
5. `core/core/memory_control_module.py`
6. `core/core/validation_memory_gate.py`

## Guidance for future updates

When the runtime changes again, update the documentation in this order:

1. `docs/SYSTEM_OVERVIEW.md`
2. `docs/RUNTIME_CHANGES_AND_MIGRATION_NOTES.md`
3. `README.md`
4. any narrow release or feature note that depends on the changed behavior

That order keeps the top-level story accurate and stops older summaries from drifting away from the code.
