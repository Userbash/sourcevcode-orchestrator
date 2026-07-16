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

These commands make it easier to prepare a development or operator environment without relying entirely on ad hoc shell scripts.

## Environment and configuration

The runtime depends on environment-based configuration for:

- listen address
- provider credentials and base URLs
- database and memory storage
- message bus backend
- routing limits and concurrency controls
- model inventory refresh and validation

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

## Publication guidance

The repository is prepared for publication in terms of structure and documentation, but publication itself still requires a final review.

Before pushing to GitHub or sharing containers, review:

- secrets and API keys
- environment-specific hostnames or internal addresses
- any local-only bootstrap assumptions
- provider defaults that should not be public defaults
- service names or labels that are specific to one machine

## Suggested publication checklist

1. Confirm that no credentials are committed.
2. Confirm that `README.md` reflects the current architecture.
3. Confirm that `docs/` is the only active documentation location.
4. Confirm that the compose stack starts with sane defaults.
5. Confirm that `go test ./...` passes in `go-core`.
6. Confirm that `/health` and `/chat/ws` work in a fresh container.
7. Confirm that model inventory endpoints report expected providers.
8. Confirm that the chat gateway forwards to the internal orchestrator correctly.

## Current operational result

The rebuilt runtime has already passed a live transport check:

- the health endpoint responded successfully on `127.0.0.1:8010`
- WebSocket chat connection succeeded
- task intake and planning completed
- routing stayed aligned with the selected provider
- the runtime returned a final response through the same WebSocket channel

That does not replace a full release test matrix, but it is a strong indication that the core orchestration path is working as intended.

