# Gemini CLI Orchestrator Contract

Gemini CLI is integrated with the local Orchestrator through the chat ingress, not through a standalone task submission HTTP API.

## Active Transport

- Primary ingress: `ws://localhost:8000/chat/ws`
- Message shape: send a JSON envelope with `type: "chat"`, a `session_id`, and the user text in `message`
- Normalized source: the orchestrator treats this ingress as `source=websocket`

Example payload:

```json
{
  "type": "chat",
  "session_id": "gemini-cli-session",
  "message": "PLAN: inspect the failing auth flow"
}
```

## Routing Contract

- `PLAN:`, `BUILD:`, `FIX:`, `REVIEW:`, `TEST:`, `RESEARCH:` and similar trigger prefixes are interpreted inside the orchestrator after chat ingress normalization.
- Gemini CLI should not invent a `/tasks/submit` fallback unless that endpoint is implemented explicitly.
- If the ingress needs to force orchestrator ownership, it must carry that through task payload hints such as `route_mode: "orchestrator"` or `routing_hints.force_orchestrator = true`.

## Runtime Notes

- Prefer the websocket chat path for normal Gemini CLI traffic.
- `external_chat` is an alias of websocket-style ingress and is normalized to the same routing path.
- For provider diagnostics, use orchestrator-native tooling instead of opening a separate auth flow.
