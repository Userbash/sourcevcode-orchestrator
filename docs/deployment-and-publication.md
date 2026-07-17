# Deployment and Publication

## Purpose

This document explains how the repository is packaged, how the runtime is deployed locally, and what should be reviewed before publishing the project on GitHub or distributing containers more widely.

## Container layout

The repository includes a containerized runtime stack built around `docker-compose.yml`.

The stack currently contains:

- `postgresql`
- `rabbitmq`
- `local_llm`
- `ai_kernel`
- `go_core`
- `chat_gateway`

The internal orchestrator service runs on port `8010`. The external chat gateway runs on port `80` and forwards traffic into the orchestrator.

## `go_core` container

The orchestrator image is built from `go-core/Dockerfile`.

Key packaging characteristics:

- multi-stage Go build
- runtime image based on a distroless non-root container
- explicit backup volume support through `/app/db_backups`

This keeps the runtime container small and focused while still providing room for operational database exports.

## Chat gateway container

The external proxy is built from `scripts/chat-proxy/Dockerfile`.

Its job is intentionally narrow:

- expose a stable external HTTP entrypoint
- forward chat traffic to the internal runtime
- keep orchestration logic inside `go_core`

This separation is useful because it allows the external chat layer to remain simple while the internal runtime stays authoritative.

## Local deployment

### Compose stack

A local stack can be started from the project root:

```sh
docker compose up --build
```

The runtime can also be built and replaced independently when only the orchestrator changes.

### Direct runtime

The Go runtime can be started without the full stack:

```sh
cd go-core
go run ./cmd/orchestrator serve
```

This mode is useful for focused development work when external dependencies are already available.

## Runtime bootstrap and preflight

The orchestrator binary includes operational commands for bringing the system into a usable state.

Important commands include:

- `bootstrap`
- `runtime-preflight`
- `runtime-agent`
- `runtime-agent-auto`
- `ai-kernel-provision`
- `ai-kernel-install-service`
- `db-backup`
- `db-restore`
- `import-core-sql`
- `import-fable-traces`

These commands make it easier to prepare a development or operator environment without relying entirely on ad hoc shell scripts.

### What bootstrap now does

Bootstrap is no longer only a thin convenience wrapper.

Depending on configuration, it can now:

- resolve runtime configuration and `.env` files
- verify required tools such as `curl` and `podman`
- ensure core infrastructure exists
- provision or recreate the local LLM runtime
- provision, start, and verify AI-kernel service state
- build the orchestrator container image
- start the orchestrator container with the required mounts and runtime environment
- verify external provider login state when applicable

This makes `bootstrap` closer to an environment bring-up command than a single container start script.

## AI-kernel lifecycle

The repository now contains a more complete operational story for `ai_kernel`.

Provisioning can:

- create the required project and model directories
- create or repair the Python virtual environment
- ensure `pip`, `setuptools`, and `wheel` exist
- install `llama-cpp-python[server]` when needed
- download the selected model and multimodal projection files when they are missing

Serving can:

- launch `llama_cpp.server`
- configure context size, thread count, GPU layers, chat-template arguments, and optional multimodal support
- isolate service credentials by rewriting runtime API-key exposure

The runtime also includes support for installing AI-kernel as a user-level systemd service and waiting for `/v1/models` readiness before treating the provider as live.

## Database protection

The operations layer now includes managed database protection in addition to manual backup and restore commands.

The database protector can:

- create periodic JSON snapshots of managed tables
- prune older snapshots according to retention settings
- watch the database on a guard interval
- restore the latest snapshot automatically when a managed database is unexpectedly empty

This is intended as operational protection for the orchestrator's own state and memory data, not as a substitute for a full external backup strategy.

## Import and migration commands

Two new import commands exist for data migration and corpus bootstrapping.

### `import-core-sql`

This command imports legacy SQL-backed runtime data into the current store and memory layout.

It can migrate data derived from tables such as:

- `memories`
- `trained_memories`
- `commands`
- `sessions`
- `vfs_files`

During import, the command normalizes metadata, scope, owner, branch, and repository identity. It can also synthesize stable ids and vector embeddings for legacy content so the imported records remain usable in the current retrieval layer.

### `import-fable-traces`

This command imports external reasoning-trace datasets from JSON, JSONL, or directory sources.

It stores imported content as:

- `RAGDocument` records for retrieval
- `ReasoningTrace` records for reasoning-memory use

This is useful when bootstrapping a memory corpus or seeding future trace-driven self-learning workflows.

## Environment and configuration

The runtime depends on environment-based configuration for:

- listen address
- provider credentials and base URLs
- database and memory storage
- message bus backend
- routing limits and concurrency controls
- model inventory refresh, readiness, and cooldown behavior
- AI-kernel model and serving configuration
- database protection and snapshot retention

Especially relevant variables include:

- `GO_CORE_ADDR`
- `AI_BRIDGE_API_HOST`
- `AI_BRIDGE_API_PORT`
- `AI_BRIDGE_MEMORY_DATABASE_URL`
- `GO_CORE_MESSAGE_BUS_BACKEND`
- `AI_BRIDGE_MESSAGE_BUS_BACKEND`
- `AI_BRIDGE_RABBITMQ_URL`
- `GO_CORE_MAX_CONCURRENT_TASKS`
- `GO_CORE_MAX_CONCURRENT_PER_AGENT`
- `GO_CORE_MAX_CONCURRENT_PER_MODEL`
- `AI_BRIDGE_MODEL_REFRESH_ENABLED`
- `AI_BRIDGE_MODEL_REFRESH_INTERVAL`
- `AI_BRIDGE_MODEL_REFRESH_TIMEOUT`
- `AI_BRIDGE_MODEL_VALIDATE_MODELS`
- `AI_BRIDGE_MODEL_PENDING_TTL`
- `AI_BRIDGE_MODEL_CONFIRMATION_TTL`
- `AI_BRIDGE_MODEL_RETRY_COOLDOWN`
- `AI_BRIDGE_MODEL_QUEUE_LIMIT`
- `AI_BRIDGE_LIVE_MODEL_PROBE`
- `GO_CORE_DB_BACKUP_DIR`
- `GO_CORE_DB_BACKUP_INTERVAL`
- `GO_CORE_DB_GUARD_INTERVAL`
- `GO_CORE_DB_BACKUP_KEEP`
- `AI_KERNEL_MODEL_ALIAS`
- `AI_KERNEL_MODEL_ID`
- `AI_KERNEL_MODEL_FILE`
- `AI_KERNEL_MMPROJ_FILE`
- `AI_KERNEL_MODEL_DIR`
- `AI_KERNEL_VENV_DIR`
- `AI_KERNEL_REASONING_PROFILE`
- `AI_KERNEL_CHAT_TEMPLATE_KWARGS`
- `AI_KERNEL_ENABLE_THINKING`
- `AI_KERNEL_MODEL_PATH`
- `AI_KERNEL_PORT`

Compatibility variables under the `GO_CORE_MODEL_REGISTRY_*` prefix are also supported for registry behavior.

## Publication guidance

The repository is prepared for publication in terms of structure and documentation, but publication itself still requires a final review.

Before pushing to GitHub or sharing containers, review:

- secrets and API keys
- environment-specific hostnames or internal addresses
- any local-only bootstrap assumptions
- provider defaults that should not be public defaults
- service names or labels that are specific to one machine
- local AI-kernel model files, download caches, and snapshot directories

## Suggested publication checklist

1. Confirm that no credentials are committed.
2. Confirm that `README.md` reflects the current architecture.
3. Confirm that `docs/` is the only active documentation location.
4. Confirm that the compose stack starts with sane defaults.
5. Confirm that `go test ./...` passes in `go-core`.
6. Confirm that `/health` and `/chat/ws` work in a fresh container.
7. Confirm that model inventory endpoints report expected providers.
8. Confirm that the chat gateway forwards to the internal orchestrator correctly.
9. Confirm that database protection paths and retention settings are safe for the target environment.
10. Confirm that AI-kernel provisioning defaults do not expose local-only paths or credentials.

## Current operational result

The rebuilt runtime has already passed a live transport check:

- the health endpoint responded successfully on `127.0.0.1:8010`
- WebSocket chat connection succeeded
- task intake and planning completed
- routing stayed aligned with the selected provider
- the runtime returned a final response through the same WebSocket channel

That does not replace a full release test matrix, but it is a strong indication that the core orchestration path is working as intended.
