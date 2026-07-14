# Host Runtime

This mode is for the Bazzite + Flatpak VS Code + Codex case where the
orchestrator must run on the host, while infra stays in Compose/Podman and the
external chat connects over WebSocket.

## Recommended topology

- `db`, `rabbitmq`, `local_llm`, `local_llm_init`, `ai_kernel` stay in `docker-compose.ai.yml`
- `go-core` orchestrator runs as a host process
- external chat connects to `ws://HOST:PORT/chat/ws`

This removes nested namespace and mount issues from the orchestration path.

## 1. Build the host binary

From repo root:

```sh
bin/build-orchestrator-host.sh
```

This produces:

```text
go-core/orchestrator
```

You can override output path:

```sh
ORCHESTRATOR_OUTPUT_BIN=/var/home/sanya/bin/orchestrator-host bin/build-orchestrator-host.sh
```

## 2. Start orchestrator on the host

Foreground:

```sh
bin/orchestrator-host.sh start-foreground
```

Background:

```sh
bin/orchestrator-host.sh start
```

Status and logs:

```sh
bin/orchestrator-host.sh status
bin/orchestrator-host.sh logs
bin/orchestrator-host.sh ws-url
```

The launcher loads `.env`, `.env.bridge`, `.env.gemini.local` and then starts:

```text
go-core/orchestrator serve --addr 0.0.0.0:PORT --ensure-ai-stack --project-root REPO_ROOT --compose-file docker-compose.ai.yml
```

`--ensure-ai-stack` starts host-facing infra services and does not require the
containerized `orchestrator` service.

## 3. Autostart with systemd user unit

Install unit:

```sh
bin/install-orchestrator-user-service.sh
systemctl --user daemon-reload
systemctl --user enable --now sourcevcode-orchestrator-host.service
```

Check status:

```sh
systemctl --user status sourcevcode-orchestrator-host.service
journalctl --user -u sourcevcode-orchestrator-host.service -f
```

## 4. External chat connection

Primary WebSocket endpoint:

```text
ws://127.0.0.1:8000/chat/ws
```

If your `.env` sets `ORCHESTRATOR_PORT=8010`, then use:

```text
ws://127.0.0.1:8010/chat/ws
```

The WebSocket handshake should include the subprotocol:

```text
Sec-WebSocket-Protocol: chat.v1
```

## 5. Minimal request for external chat

Canonical frame:

```json
{
  "type": "command",
  "request_id": "req-1",
  "action": "chat.submit",
  "ack": true,
  "data": {
    "description": "Проанализируй репозиторий и найди причину деградации latency",
    "type": "code",
    "project": "sourcevcode-orchestrator",
    "session_id": "session-1"
  }
}
```

Minimal compact frame also works. The transport normalizes it to `chat.submit`:

```json
{
  "r": "req-1",
  "message": "Проверь ошибки planner и memory context",
  "m": "session-1",
  "ack": true
}
```

Useful `data` fields:

- `description` or `message`: user request text
- `type`: usually `code`, `docs`, `review`, or `research`
- `project`: logical project name
- `session_id`: sticky conversation or workflow session
- `provider`: optional provider hint
- `model`: optional model hint
- `complexity`: optional routing hint
- `priority`: optional queue hint

## 6. Expected WebSocket flow

Typical sequence:

1. client sends `chat.submit`
2. server returns `ack`
3. server returns final `response`
4. runtime side channels can be read from `/ws/runtime/events`

Example `ack`:

```json
{
  "type": "ack",
  "request_id": "req-1",
  "action": "chat.submit",
  "ack": true,
  "data": {
    "accepted": true,
    "mode": "sync"
  }
}
```

Example final response shape:

```json
{
  "type": "response",
  "request_id": "req-1",
  "action": "chat.submit",
  "final": true,
  "data": {
    "task_id": "task-...",
    "session_id": "session-1",
    "status": "completed"
  }
}
```

Exact `data` payload can vary by workflow path, but `type=response` and
`final=true` indicate the request lifecycle is complete.

## 7. Browser example

```js
const ws = new WebSocket("ws://127.0.0.1:8010/chat/ws", "chat.v1");

ws.onopen = () => {
  ws.send(JSON.stringify({
    type: "command",
    request_id: crypto.randomUUID(),
    action: "chat.submit",
    ack: true,
    data: {
      description: "Проверь routing regressions и memory usage",
      type: "code",
      project: "sourcevcode-orchestrator",
      session_id: "web-session-1"
    }
  }));
};

ws.onmessage = (event) => {
  console.log(JSON.parse(event.data));
};
```

## 8. Host env that usually matters

- `ORCHESTRATOR_PORT`: host port for WS and HTTP APIs
- `GO_CORE_ADDR`: explicit bind address if you do not want the default
- `AI_BRIDGE_MEMORY_DATABASE_URL` or host/port variables for Postgres
- `AI_BRIDGE_RABBITMQ_URL` or host/port variables for RabbitMQ
- `AI_BRIDGE_LOCAL_LLM_ENDPOINT`: Ollama or other local LLM endpoint
- `AI_KERNEL_BASE_URL`: AI kernel base URL
- `ORCHESTRATOR_BIN`: path to a prebuilt host binary

Example:

```sh
ORCHESTRATOR_PORT=8010 \
ORCHESTRATOR_BIN=/var/home/sanya/sourcevcode-orchestrator/go-core/orchestrator \
bin/orchestrator-host.sh start
```

## 9. When to use host-process instead of containerized orchestrator

Use host-process mode when the orchestrator must:

- see the real repo path on the host
- call host `git`, `podman`, `docker`, local binaries, and local tools
- avoid Flatpak namespace and volume visibility problems
- expose one stable WS endpoint to an external chat or voice client
