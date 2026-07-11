#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 IMAGE [extra args...]" >&2
  exit 2
fi

image="$1"
shift

exec podman run --rm -it \
  --privileged \
  -v "$PWD":/workspace:Z \
  -w /workspace \
  "$image" "$@"
