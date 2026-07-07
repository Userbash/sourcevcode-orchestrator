# Empty Provider Guardrails Plan

## Objective

Prevent the orchestrator from:

1. sending empty or scaffold-only requests to OpenAI-compatible providers
2. treating `200 OK` responses with no assistant content and no tool calls as successful executions

## Invariants

- Request guard: reject prompts and `input` payloads that normalize down to empty text or label-only scaffolding such as `OBJECTIVE:` and `FILES:`.
- Response guard: accept a provider response only when it contains assistant text or explicit tool-call output.
- Error contract: use stable errors so routing, fallback, telemetry, and tests classify these cases consistently.
  - `Refusing to send empty provider request`
  - `Provider returned no assistant content or tool calls`

## Implementation

Shared logic lives in `core/core/openai_payload_guard.py`.

- Request normalization:
  - trim string and structured content payloads
  - strip uppercase label prefixes like `OBJECTIVE:` before checking for meaningful alphanumeric content
- Response normalization:
  - extract assistant text from Chat Completions and Responses payloads
  - detect tool calls for providers that legitimately answer with tools before text

## Covered execution paths

- OpenAI-compatible agents:
  - `AIKernelAgent`
  - `CodexAgent`
  - `MistralAgent`
- Native/provider wrappers:
  - `invoke_mimo_native`
  - `invoke_antigravity_native`
- Runtime modules:
  - `OpenAIResponsesRuntime`
  - `ReasoningModule`
  - `VoiceListenerModule`
  - `ExternalAIBridge`
  - `AntigravityManager` generation probe

## Verification

Targeted regression tests cover both failure modes:

- empty request is rejected before network execution
- empty assistant response is surfaced as an error after a `200 OK`

Primary test files:

- `core/test/test_mimo_provider.py`
- `core/test/test_antigravity_provider.py`
- `core/test/test_mistral_agent.py`
- `core/test/test_ai_kernel_integration.py`
- `core/test/test_codex_agent_mistral_guards.py`
- `core/test/test_antigravity_manager.py`
- `core/test/test_openai_responses_runtime.py`
- `core/test/test_external_ai_bridge.py`

## Non-goals

- This work does not redefine provider-specific auth, quota, or transport failures.
- Operational scripts may still use lighter-weight probing unless they are part of orchestrator execution or readiness decisions.
