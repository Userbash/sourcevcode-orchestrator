# SourceVCode Orchestrator

SourceVCode Orchestrator is the coordination layer for this workspace. It routes tasks, runs the core AI bridge, manages model and provider policy, and exposes the runtime services used by the rest of the stack.

The project is organized around one goal: keep orchestration, routing, and execution decisions in one place so the system stays understandable, debuggable, and easy to operate.

## What it does

- Accepts tasks from chat, API, and internal listeners.
- Breaks work into smaller execution steps when needed.
- Routes work to the best available provider or local agent.
- Applies token, quota, and fallback policy before execution.
- Streams progress and final results back to the caller.
- Keeps runtime logs, health checks, and memory state in sync.

## Why it exists

The repository is meant to be the stable control plane for the workspace. It reduces scattered orchestration logic, makes provider behavior predictable, and gives one place to inspect task flow end to end.

## Main areas

1. Core orchestrator
- task routing and execution
- fallback and retry handling
- provider and token budget policy

2. API bridge
- HTTP endpoints
- WebSocket chat streaming
- task submission and trace reporting

3. Runtime modules
- model usage tracking
- provider availability checks
- memory, scheduler, and lifecycle management

4. Support tooling
- local start scripts
- health checks
- deployment guards and diagnostics

## Architecture summary

- `core/`: task orchestration, routing, and runtime modules
- `scripts/`: local startup, deployment, and diagnostics helpers
- `docs/`: architecture and operations documentation
- `tests/`: system-level validation and tooling tests

## Core principles

- Orchestration logic should be explicit and observable.
- Provider fallback should be deterministic and policy-driven.
- The system should prefer safe degradation over silent failure.
- Runtime state should be inspectable through logs, metrics, and traces.
- Long-running operations should stream progress instead of blocking silently.

## Getting started

1. Prepare environment
```bash
cp .env.example .env
```

2. Start the core stack
```bash
bash scripts/start_core_stack.sh
```

3. Open the runtime
- Orchestrator API: `http://localhost:8000`
- Health: `http://localhost:8000/health`

## Development commands

AI Orchestrator:
```bash
./scripts/start_core_stack.sh
```

Tests:
```bash
npm test
python3 -m pytest core/test
```

## Documentation

See `docs/` for architecture, runtime flow, provider policy, and operational notes.

## License

MIT (see `LICENSE`).

## Recent updates

- GitHub CLI authentication is now bridged through the workspace token flow.
- The orchestrator can automatically read `GITHUB_API_KEY` and reuse it for `gh` and Git operations.
- SourceCraft repository workflows are wired into the core routing and API bridge.
- Host bridge diagnostics now allow common runtime checks such as `env`, `printenv`, `ps`, `df`, and `hostname`.

## Project changes in plain English

This project is the control plane for the workspace. It coordinates work, chooses the right execution path, and keeps the runtime observable.

Recent work focused on making GitHub automation safer and more hands-off:

- `gh` can now log in automatically from the workspace token.
- The container environment keeps the token available at startup, so users do not need to repeat manual login steps.
- Git identity inside the container is set to `Userbash <wairuste@gmail.com>` for repository work.
- SourceCraft is used as the repository-operation layer for repo status, PR, and release workflows.

The result is a workflow where the orchestrator can manage repository tasks, authenticate to GitHub, and keep the experience mostly invisible to the user.
