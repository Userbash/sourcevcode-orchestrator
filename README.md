# SourceVCode Orchestrator

SourceVCode Orchestrator is the runtime control plane for this repository. It accepts user tasks, normalizes noisy input, decides how work should be split, routes each task to the right agent or provider, and returns one merged result with trace data, validation state, and runtime diagnostics.

The current codebase is built around one active path: structured task orchestration for engineering work. Older descriptions that treated the repository as a general chat bridge, a mixed frontend stack, or a loose collection of experiments are no longer accurate.

## What the runtime does today

- Accepts tasks from HTTP, WebSocket, and internal runtime entry points.
- Cleans and normalizes task input before routing.
- Builds execution plans with explicit dependencies.
- Fans out large coding tasks across multiple agents when the task shape supports parallel work.
- Preserves fan-in dependencies so review, test, and merge steps wait for the full parallel branch set.
- Applies provider policy, budget policy, and fallback rules before execution.
- Builds memory-aware execution context for each task.
- Tracks validation-memory warmup, consensus, and conflict state.
- Returns a merged result together with live trace rows, module state, and orchestration validation output.

## What changed from the older runtime

The active runtime has moved away from several older assumptions.

Previously, the documentation described the system as a broad AI bridge with many interchangeable paths. That description is now too loose. The runtime now has a clearer center of gravity:

- task intake is normalized before planning and routing
- decomposition is dependency-aware rather than mostly linear
- large code work can be split into parallel branches
- branch results are merged only after the whole branch family completes
- memory context is assembled through dedicated modules instead of ad hoc prompt stitching
- validation-memory warmup and conflict state are exposed in the final module report
- provider fallback is explicit, budget-aware, and traceable

Older text also gave too much weight to compatibility paths. Compatibility support still exists where needed, but it is no longer the main story of the system.

## Main runtime areas

1. Orchestration core
   - task planning
   - DAG execution
   - async branch scheduling
   - result merge and validation

2. Routing and policy
   - model selection
   - provider availability
   - budget enforcement
   - fallback control

3. Runtime memory and validation
   - session memory
   - layered context memory
   - validation-memory gate
   - memory warmup reporting

4. Delivery and observability
   - mailbox delivery supervision
   - health checks
   - live trace rows
   - module state snapshots

## Repository layout

- `core/` runtime orchestration code, agents, modules, and tests
- `scripts/` local bootstrap, diagnostics, and operational helpers
- `docs/` architecture, operational notes, and release-oriented documentation
- `docker-compose.ai.yml` local stack definition

## Start the stack

```bash
bash scripts/bootstrap_ai_stack.sh
```

Optional flags:

```bash
bash scripts/bootstrap_ai_stack.sh --agy-login
bash scripts/bootstrap_ai_stack.sh --model qwen2.5:7b-instruct
```

## Runtime endpoints

- Orchestrator API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`
- Full health: `http://127.0.0.1:8000/health/full`
- RabbitMQ UI: `http://127.0.0.1:15672`
- Local LLM: `http://127.0.0.1:11434`

## Development commands

Run orchestrator tests:

```bash
python3 -m pytest core/test
```

Run focused orchestrator regression suites:

```bash
python3 -m pytest core/test/test_orchestrator.py -q
python3 -m pytest core/test/test_ai_bridge_orchestrator_protocol.py -q
```

Provider diagnostics:

```bash
python3 -m core.scripts.verify_openai_bridge
python3 -m core.scripts.verify_provider_stack
python3 -m core.scripts.verify_antigravity_keys
```

## Documentation guide

Start with these files:

- `docs/SYSTEM_OVERVIEW.md` for the current architecture
- `docs/RUNTIME_CHANGES_AND_MIGRATION_NOTES.md` for the old-to-new runtime comparison
- `docs/README.md` for the documentation map

## Notes on compatibility

A few compatibility aliases still exist for import stability and provider migration. They are not the preferred integration path. The documentation in this repository now describes the current runtime first and treats compatibility behavior as secondary.

## License

MIT. See `LICENSE`.
