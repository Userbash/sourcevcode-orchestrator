# Bwrap Runtime Selector

Use this launcher when the current runtime cannot create user namespaces for `bubblewrap`.
It probes the local environment first and, if needed, starts a temporary container runtime
that can execute `bwrap` safely.

## Usage

```bash
./scripts/runtime/run_bwrap_aware_agent.sh IMAGE_NAME [command args...]
```

## Behavior

1. Probe local `bwrap` with a user namespace test.
2. If the probe passes, use the current runtime.
3. If the probe fails, create a temporary unconfined container runtime.
4. If unconfined launch is unavailable, fall back to a temporary privileged runtime.
5. If none of the above works, fail with explicit diagnostics.

## Environment Controls

Prefer Podman or Docker explicitly:

```bash
BWRAP_RUNTIME_PREFERENCE=podman ./scripts/runtime/run_bwrap_aware_agent.sh IMAGE_NAME
BWRAP_RUNTIME_PREFERENCE=docker ./scripts/runtime/run_bwrap_aware_agent.sh IMAGE_NAME
```

Disable privileged fallback:

```bash
BWRAP_ALLOW_PRIVILEGED_FALLBACK=0 ./scripts/runtime/run_bwrap_aware_agent.sh IMAGE_NAME
```

## Notes

This does not change kernel policy from inside the current session. It routes execution into a
separate temporary runtime that has the security profile required by `bwrap`.
