# AI Bridge Structure

## Canonical layout

- `core/core/` - kernel runtime, routing, memory, policy, modules, adapters, and health checks.
- `core/agents/` - agent implementations only.
- `core/protocols/` - transport/protocol definitions.
- `core/scripts/` - executable entrypoints, maintenance tools, smoke tests, and host bridge shims.
- `core/db/` - persistence models and DB session helpers.
- `core/adapters/` - integration adapters for runtime, memory, orchestration, scheduling, and models.
- `core/schemas/` - JSON schema contracts.

## Canonical runtime entrypoints

- `core/scripts/orchestrator_daemon.py` - main orchestrator process.
- `core/scripts/run_orchestrator.py` - interactive/manual orchestrator runner.
- `core/scripts/verify_core.py` - consistency and module wiring probe.
- `core/core/core_healthcheck.py` - operational readiness probe.
- `core/scripts/deploy_local_llm.py` - host-side local Ollama provisioning.
- `core/scripts/host_bridge_cli.py` and `core/scripts/bridge/exec.sh` - host bridge command execution.

## Legacy entrypoints kept for compatibility

These are not part of the preferred path, but they still exist so older automation does not break:


## Deprecated but still referenced by docs or wrappers

These should be reviewed next, but not removed blindly:

- `core/scripts/prepare_clean_env.py`
- `core/scripts/pre_deploy_check.py`
- `core/scripts/pre_deploy_security_check.py`
- `core/scripts/run_healthcheck.py`
- `core/scripts/run_tests.py`
- `core/scripts/run_sourcecraft_smoke.py`
- `core/scripts/repoins.py`
- `core/scripts/check-bridges.sh`

## Current kernel modules loaded on boot

The orchestrator currently autoloads the following modules:

- `ai_activity`
- `orchestrator_control`
- `model_usage`
- `model_availability`
- `api_bridge`
- `smart_decomposer`
- `prompt_optimizer`
- `chat_bus`
- `trigger_dispatcher`
- `json_themes`
- `unified_vfs`
- `cold_boot`
- `ui_design_system`
- `ui_anti_template`
- `frontend_engineering_bridge`
- `autodev_pipeline`
- `dev_toolkit`
- `local_llm`
- `sourcecraft`
- `voice_listener`

## Notes

- The runtime path should stay: `scripts/start_core_stack.sh` for compose-based startup, or `scripts/start_manual.sh` for the older host-Podman route.
- Do not move or delete legacy scripts until their call sites are audited. Wrap them first, then retire them.
