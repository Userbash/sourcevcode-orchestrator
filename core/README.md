# Core Runtime

`core/` contains the active orchestration runtime.

This directory is not just a bag of agent helpers. It is where task intake, decomposition, routing, memory assembly, execution supervision, and final merge logic live.

## What is in this directory

- `core/` orchestration modules, routing logic, runtime memory, delivery supervision, and diagnostics
- `agents/` local and provider-backed agent implementations
- `scripts/` development and runtime entry points
- `test/` regression and behavior tests for the orchestrator runtime

## Current runtime behavior

The current runtime is built for engineering tasks.

A task can enter through API or WebSocket, but transport is not the main story anymore. The important behavior is what happens after intake:

- input is normalized
- the task is classified and decorated
- an execution plan is built
- large code work can branch into parallel sub-tasks
- routing picks agents and providers using policy and health signals
- memory context is built through dedicated modules
- results are merged into one final report

## Important modules

### `core/orchestrator.py`

The main runtime controller. It owns planning, routing, async execution, branch batching, fallback behavior, and final orchestration reports.

### `core/task_decomposer.py`

Builds atomic task graphs. It now supports parallel code fan-out and correct fan-in dependency handling for branch families.

### `core/memory_control_module.py`

Builds provider-aware runtime context and tags parallel batches with memory profiles.

### `core/validation_memory_gate.py`

Warms memory, builds consensus signals, and records validation-memory conflicts.

### `core/delivery_supervisor.py`

Supervises mailbox-style local-agent delivery and tracks handoff state.

## What changed from older descriptions

Older documentation in this directory gave too much weight to WebSocket transport, bridge framing, and generic external-agent wiring.

Those pieces still exist, but they no longer explain the runtime well on their own. The active design is centered on orchestration, not on transport.

The biggest current differences are:

- normalized intake profiles are carried through the runtime
- large coding tasks can run as parallel branch sets
- branch dependencies are explicit and enforced at fan-in
- memory warmup and conflict state are visible in module output
- provider policy is tightly coupled to execution tracing and budget state

## Running tests

From repository root:

```bash
python3 -m pytest core/test
```

Focused suites:

```bash
python3 -m pytest core/test/test_orchestrator.py -q
python3 -m pytest core/test/test_ai_bridge_orchestrator_protocol.py -q
python3 -m pytest core/test/test_validation_memory_gate.py -q
```

## Development note

If you change decomposition, routing, or runtime memory behavior, update the documentation at the same time. The current repository depends on docs staying close to the active runtime, not to older architectural drafts.
