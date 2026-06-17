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

1. Bootstrap the stack from zero
```bash
bash scripts/bootstrap_ai_stack.sh
```

2. Optional: complete Antigravity CLI auth if `agy` is installed
```bash
bash scripts/bootstrap_ai_stack.sh --agy-login
```

3. Optional: choose a different local model
```bash
bash scripts/bootstrap_ai_stack.sh --model qwen2.5:7b-instruct
```

The bootstrap script will:
- create `.env`, `.env.bridge`, and `.env.gemini.local` from example files when missing
- start `Postgres`, `RabbitMQ`, the orchestrator, and an `Ollama` container on `127.0.0.1:11434`
- pull the configured local model
- verify `Mistral` when `MISTRAL_API_KEY` is set
- verify or launch `agy` authorization when requested

## Runtime endpoints

- Orchestrator API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`
- Full health: `http://127.0.0.1:8000/health/full`
- RabbitMQ UI: `http://127.0.0.1:15672`
- Local LLM: `http://127.0.0.1:11434`

## Environment files

- `.env.example`: local runtime defaults for the orchestrator container
- `.env.bridge.example`: provider keys, OpenAI-compatible endpoint overrides, and bridge-specific runtime settings
- `.env.gemini.local.example`: local model defaults

Remote providers remain optional. Without `OPENAI_API_KEY`, `MISTRAL_API_KEY`, or a working `agy` login, the stack still starts and serves local orchestration, database, broker, and local-model routing. The default zero-to-working local model is `qwen2.5:0.5b` to keep first deployment fast; move to a larger Ollama model once the stack is healthy.

## Development commands

Bootstrap core stack:
```bash
bash scripts/bootstrap_ai_stack.sh
```

Legacy entrypoint kept for compatibility:
```bash
./core/scripts/start_core_stack.sh
```

Tests:
```bash
python3 -m pytest core/test
```

Quick provider check:
```bash
python3 -m core.scripts.verify_openai_bridge
python3 -m core.scripts.verify_provider_stack
python3 -m core.scripts.verify_provider_stack --strict
```

## Documentation

See `docs/` for architecture, runtime flow, provider policy, and operational notes.

## License

MIT (see `LICENSE`).

## Recent updates

- GitHub CLI authentication is now bridged through the workspace token flow.
- The orchestrator can automatically read `GITHUB_API_KEY` and reuse it for `gh` and Git operations.
- The orchestrator also accepts the legacy alias `GITHUB_API` and syncs it to `GITHUB_TOKEN`/`GH_TOKEN` at runtime.
- SourceCraft repository workflows are wired into the core routing and API bridge.
- Host bridge diagnostics now allow common runtime checks such as `env`, `printenv`, `ps`, `df`, and `hostname`.
- Task intake now normalizes noisy text, removes unsafe formatting artifacts, and preserves cleaner structured task input.
- The routing stack now uses a normalized intake profile to choose safer providers, stronger review lanes, and parallel code fan-out when the request shape supports it.
- Prompt optimization and memory context now surface intake-quality and risk hints so downstream agents act with clearer guardrails.
- Local bootstrap now provisions the minimal AI stack without requiring `podman compose` or manual env-file creation.
