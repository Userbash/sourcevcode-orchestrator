#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 IMAGE [command args...]" >&2
  exit 2
fi

image="$1"
shift

workspace="${PWD}"
command_args=("$@")

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

probe_local_bwrap() {
  if ! have_cmd bwrap; then
    return 1
  fi
  bwrap --unshare-user --ro-bind / / true >/dev/null 2>&1
}

run_local() {
  if [[ ${#command_args[@]} -eq 0 ]]; then
    exec "$image"
  fi
  exec "$image" "${command_args[@]}"
}

run_podman_unconfined() {
  exec podman run --rm -it \
    --security-opt seccomp=unconfined \
    --security-opt label=disable \
    -v "$workspace":/workspace:Z \
    -w /workspace \
    "$image" "${command_args[@]}"
}

run_podman_privileged() {
  exec podman run --rm -it \
    --privileged \
    -v "$workspace":/workspace:Z \
    -w /workspace \
    "$image" "${command_args[@]}"
}

run_docker_unconfined() {
  exec docker run --rm -it \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    -v "$workspace":/workspace \
    -w /workspace \
    "$image" "${command_args[@]}"
}

run_docker_privileged() {
  exec docker run --rm -it \
    --privileged \
    -v "$workspace":/workspace \
    -w /workspace \
    "$image" "${command_args[@]}"
}

if probe_local_bwrap; then
  echo "local bwrap probe passed; using current runtime" >&2
  run_local
fi

echo "local bwrap probe failed; creating a temporary runtime that can execute bwrap" >&2

runtime_preference="${BWRAP_RUNTIME_PREFERENCE:-auto}"
privileged_fallback="${BWRAP_ALLOW_PRIVILEGED_FALLBACK:-1}"

if [[ "$runtime_preference" == "auto" || "$runtime_preference" == "podman" ]]; then
  if have_cmd podman; then
    echo "starting temporary Podman runtime with seccomp=unconfined" >&2
    run_podman_unconfined
  fi
fi

if [[ "$runtime_preference" == "auto" || "$runtime_preference" == "docker" ]]; then
  if have_cmd docker; then
    echo "starting temporary Docker runtime with seccomp=unconfined" >&2
    run_docker_unconfined
  fi
fi

if [[ "$privileged_fallback" == "1" ]]; then
  if [[ "$runtime_preference" == "auto" || "$runtime_preference" == "podman" ]]; then
    if have_cmd podman; then
      echo "unconfined runtime unavailable; falling back to temporary privileged Podman runtime" >&2
      run_podman_privileged
    fi
  fi

  if [[ "$runtime_preference" == "auto" || "$runtime_preference" == "docker" ]]; then
    if have_cmd docker; then
      echo "unconfined runtime unavailable; falling back to temporary privileged Docker runtime" >&2
      run_docker_privileged
    fi
  fi
fi

cat >&2 <<'EOF'
Unable to create a bwrap-capable temporary runtime.

Expected one of the following:
- local runtime where `bwrap --unshare-user --ro-bind / / true` succeeds
- Podman available for `--security-opt seccomp=unconfined --security-opt label=disable`
- Docker available for `--security-opt seccomp=unconfined --security-opt apparmor=unconfined`
- privileged fallback explicitly allowed

Run `python3 scripts/runtime/preflight_userns.py` for detailed diagnostics.
EOF

exit 1
