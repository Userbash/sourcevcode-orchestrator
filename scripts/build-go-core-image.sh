#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
IMAGE_TAG=${1:-go-core-orchestrator:local}
COMMIT=${COMMIT:-$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf 'unknown')}
BUILD_TIME=${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}
VERSION=${VERSION:-$(date -u +%Y.%m.%d)-${COMMIT}}

printf 'Building %s\n' "$IMAGE_TAG"
printf 'VERSION=%s\nCOMMIT=%s\nBUILD_TIME=%s\n' "$VERSION" "$COMMIT" "$BUILD_TIME"

docker build \
  -f "$ROOT_DIR/go-core/Dockerfile" \
  --build-arg VERSION="$VERSION" \
  --build-arg COMMIT="$COMMIT" \
  --build-arg BUILD_TIME="$BUILD_TIME" \
  -t "$IMAGE_TAG" \
  "$ROOT_DIR/go-core"
