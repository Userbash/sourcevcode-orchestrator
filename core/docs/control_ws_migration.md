# Control WS Migration

Primary control-plane websocket endpoint: `/control/ws`

Request frame contract:
- `type`: `command`, `subscribe`, `unsubscribe`, `cancel`
- `request_id`: caller-generated non-empty string
- `action`: websocket action name
- `data`: JSON object payload
- `ack`: optional boolean
- `timeout_ms`: optional positive integer

Legacy HTTP compatibility policy:
- `/health`, `/health/full`, `/api/health` stay HTTP-first for readiness/liveness.
- All other control-plane HTTP routes are compatibility paths and should migrate to `/control/ws`.
- Legacy HTTP routes advertise their canonical websocket action via response headers:
  - `Deprecation: true`
  - `Link: </control/ws>; rel=alternate`
  - `X-Control-WS-Endpoint: /control/ws`
  - `X-Control-WS-Action: ...`
  - `X-Control-WS-Subscribe: ...` when a streaming replacement exists

HTTP to WS mapping:
- `/stats` -> `stats.get`
- `/antigravity/status` -> `antigravity.status.get`
- `/providers/openai/runtime_inventory` -> `providers.openai.runtime_inventory.get`, `providers.openai.runtime_inventory.subscribe`
- `/providers/openai/discovery` -> `providers.openai.discovery.get`
- `/providers/openai/model_templates` -> `providers.openai.model_templates.get`
- `/providers/inventory` -> `providers.inventory.get`, `providers.inventory.subscribe`
- `/providers/{provider}/inventory` -> `providers.inventory.provider.get`
- `/providers/runtime_inventory` -> `providers.runtime_inventory.get`, `providers.runtime_inventory.subscribe`
- `/providers/{provider}/runtime_inventory` -> `providers.runtime_inventory.provider.get`, `providers.runtime_inventory.provider.subscribe`
- `/providers/models/index` -> `providers.models.index.get`, `providers.models.index.subscribe`
- `/providers/models/index/{model_name}` -> `providers.models.lookup.get`
- `/socraticode/context_compaction/status` -> `socraticode.context_compaction.status.get`, `socraticode.context_compaction.status.subscribe`
- `/providers/local_llm/residents` -> `providers.local_llm.residents.get`
- `/providers/local_llm/connect` -> `providers.local_llm.connect`
- `/providers/local_llm/disconnect` -> `providers.local_llm.disconnect`
- `/providers/local_llm/warm` -> `providers.local_llm.warm`
- `/providers/ai_kernel/gate` -> `providers.ai_kernel.gate.get`
- `/providers/ai_kernel/ensure` -> `providers.ai_kernel.ensure`
- `/health/local_models` -> `health.local_models.get`
- `/dump_memory` -> `memory.dump.get`
- `/sourcecraft` -> `sourcecraft.status.get`
- `/sourcecraft/delegate` -> `sourcecraft.delegate.get`, `sourcecraft.delegate`
- `/sourcecraft/parallel_delegate` -> `sourcecraft.parallel_delegate.get`, `sourcecraft.parallel_delegate`
- `/transport/audit` -> `transport.audit.get`
- `/diagnostics` -> `diagnostics.get`, `diagnostics.subscribe`

WS-first control-plane actions:
- `stats.get`
- `antigravity.status.get`
- `providers.inventory.get`
- `providers.inventory.subscribe`
- `providers.runtime_inventory.get`
- `providers.runtime_inventory.subscribe`
- `providers.runtime_inventory.provider.get`
- `providers.runtime_inventory.provider.subscribe`
- `providers.openai.runtime_inventory.get`
- `providers.openai.runtime_inventory.subscribe`
- `providers.openai.discovery.get`
- `providers.openai.model_templates.get`
- `providers.models.index.get`
- `providers.models.index.subscribe`
- `providers.models.lookup.get`
- `providers.local_llm.residents.get`
- `providers.local_llm.connect`
- `providers.local_llm.disconnect`
- `providers.local_llm.warm`
- `providers.ai_kernel.gate.get`
- `providers.ai_kernel.ensure`
- `health.local_models.get`
- `memory.dump.get`
- `sourcecraft.status.get`
- `sourcecraft.delegate`
- `sourcecraft.parallel_delegate`
- `transport.audit.get`
- `diagnostics.get`
- `diagnostics.subscribe`
- `socraticode.context_compaction.status.get`
- `socraticode.context_compaction.status.subscribe`

Compatibility aliases:
- `sourcecraft/delegate`
- `sourcecraft/parallel_delegate`
- `local_llm/connect`
- `local_llm/disconnect`
- `local_llm/warm`
- `ai_kernel/ensure`

Recommended migration rules:
- Use `/control/ws` for interactive, long-running, or chatty control-plane operations.
- Prefer canonical dotted action names for new clients.
- Treat dedicated `/ws/...` stream endpoints as legacy compatibility paths; prefer `subscribe` actions on `/control/ws`.

Example: parallel SourceCraft delegation
```json
{
  "type": "command",
  "request_id": "req-parallel-1",
  "action": "sourcecraft.parallel_delegate",
  "ack": true,
  "data": {
    "type": "code",
    "description": "Implement websocket migration for SourceCraft and diagnostics",
    "files": [
      "core/scripts/orchestrator_daemon.py",
      "core/core/orchestrator_ws_dispatcher.py"
    ]
  }
}
```

Expected flow:
1. `ack`
2. `event stage=accepted`
3. `event stage=brief_ready`
4. `event stage=plan_ready final=true`

Example: provider runtime inventory for one provider
```json
{
  "type": "subscribe",
  "request_id": "req-runtime-1",
  "action": "providers.runtime_inventory.provider.subscribe",
  "ack": true,
  "data": {
    "provider": "openai"
  }
}
```

Example: OpenAI runtime inventory snapshot
```json
{
  "type": "command",
  "request_id": "req-openai-runtime-1",
  "action": "providers.openai.runtime_inventory.get",
  "ack": true,
  "data": {
    "force_refresh": true,
    "probe_limit": 0
  }
}
```
