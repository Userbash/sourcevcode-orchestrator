# Bwrap Runtime Recovery Runbook

## Problem

The local coding environment cannot use `bubblewrap` sandboxing when the runtime blocks
user namespaces. The practical symptoms are:

- `unshare -Ur true` fails with `Operation not permitted`
- `bwrap --unshare-user --ro-bind / / true` fails before the command starts
- `apply_patch` can add a new file but cannot reliably update or delete an existing file
- sandboxed command execution fails before the target command starts

This is not a repository code bug. It is a runtime policy problem.

## What Must Work

The environment is considered ready only if all checks pass:

1. `unshare -Ur true`
2. `bwrap --unshare-user --ro-bind / / true`
3. seccomp is not blocking the required namespace call path
4. targeted tests can run
5. create, update, and delete on a temporary file all work in the same runtime

## Fastest Safe Path

For Bazzite, Fedora Silverblue, Fedora CoreOS, and similar immutable hosts:

1. Do not rely on `kernel.unprivileged_userns_clone` as the main fix
2. Start the agent container with `seccomp=unconfined`
3. For Podman, also disable SELinux relabel interference with `--security-opt label=disable`
4. If that still fails, use `--privileged` as a temporary unblocker
5. Run preflight before any code generation

## Podman

Unconfined:

```bash
./scripts/runtime/run_podman_agent_unconfined.sh IMAGE_NAME
```

Temporary fallback:

```bash
./scripts/runtime/run_podman_agent_privileged.sh IMAGE_NAME
```

Recommended manual form:

```bash
podman run --rm -it \
  --security-opt seccomp=unconfined \
  --security-opt label=disable \
  -v "$PWD":/workspace:Z \
  -w /workspace \
  IMAGE_NAME
```

## Docker

Unconfined:

```bash
./scripts/runtime/run_docker_agent_unconfined.sh IMAGE_NAME
```

Temporary fallback:

```bash
./scripts/runtime/run_docker_agent_privileged.sh IMAGE_NAME
```

Recommended manual form:

```bash
docker run --rm -it \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=unconfined \
  -v "$PWD":/workspace \
  -w /workspace \
  IMAGE_NAME
```

## Compose

Use the provided override file:

```bash
docker compose -f docker-compose.ai.yml -f docker-compose.ai.unconfined.override.yml up
```

## Preflight

Run:

```bash
python3 scripts/runtime/preflight_userns.py
```

Interpretation:

- `READY`: runtime is good enough for sandboxed agent work
- `BLOCKED`: runtime policy is still preventing user namespace sandboxing

A valid `READY` result requires all of the following in the same runtime:

- `unshare -Ur true` succeeds
- `bwrap` user namespace probe succeeds
- temporary file create, update, and delete all succeed

## Host-Level Fallbacks

Only use these when you control the host and cannot switch to an unconfined container profile.

Temporary runtime tweak:

```bash
sudo sysctl kernel.unprivileged_userns_clone=1
```

Persistent configuration:

```bash
echo 'kernel.unprivileged_userns_clone=1' | sudo tee /etc/sysctl.d/99-userns.conf
sudo sysctl --system
```

These host changes are secondary to container security configuration on immutable Fedora-family systems.

## Exit Criteria

The environment is ready only when:

- sandboxed command execution starts normally
- `apply_patch` no longer fails intermittently on update/delete style operations
- targeted tests complete in the same runtime profile
