# Operations Runbook

## Scope

Operational procedures for the SourceVCode Orchestrator core runtime.

## Standard Startup

1. Validate environment variables (`.env` against `.env.example`).
2. Start the core stack:
   - `bash scripts/start_core_stack.sh`
3. Confirm health:
   - Orchestrator API: `GET /health` (via `http://localhost:8000/health`)

## Standard Shutdown

- Terminate the Orchestrator process gracefully.
- Ensure no in-progress task orchestrations or data backfills before shutdown.

## Routine Checks

- Orchestrator process health (`active` expected)
- Error-rate spikes in core logs
- API latency trends
- AI bridge connectivity

## Change Deployment Procedure

1. Review changelog.
2. Run tests:
   - `npm test`
   - `python3 -m pytest core/tests`
3. Deploy using approved flow.
4. Run post-deploy smoke checks.
5. Record release manifest and rollback notes.

