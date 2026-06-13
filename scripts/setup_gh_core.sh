#!/bin/bash
set -euo pipefail

echo "=== GitHub CLI Automation for AI Core ==="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env.gemini.local}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.ai.yml}"
CONTAINER_NAME="${CONTAINER_NAME:-hebrew_ai_orchestrator}"

compose_cmd() {
    if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
        podman compose -f "$COMPOSE_FILE" "$@"
        return
    fi
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE" "$@"
        return
    fi
    if command -v docker-compose >/dev/null 2>&1; then
        docker-compose -f "$COMPOSE_FILE" "$@"
        return
    fi
    if command -v podman-compose >/dev/null 2>&1; then
        podman-compose -f "$COMPOSE_FILE" "$@"
        return
    fi
    echo "ERROR: no compose provider found (podman compose, docker compose, docker-compose, podman-compose)."
    exit 1
}

container_cmd() {
    if command -v podman >/dev/null 2>&1; then
        printf "%s" podman
        return
    fi
    if command -v docker >/dev/null 2>&1; then
        printf "%s" docker
        return
    fi
    echo "ERROR: neither podman nor docker is installed."
    exit 1
}

install_gh_cmd() {
    cat <<'EOF'
if command -v gh >/dev/null 2>&1; then
    exit 0
fi
if command -v dnf >/dev/null 2>&1; then
    dnf install -y gh
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y curl
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
    apt-get update
    apt-get install -y gh
else
    echo "gh install is unsupported for this base image"
    exit 1
fi
EOF
}

CONTAINER_RUNTIME="$(container_cmd)"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    exit 1
fi

GH_TOKEN=$(grep -E '^(GH_TOKEN|GITHUB_TOKEN|GITHUB_API_KEY)=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' | tr -d "'" | head -n 1)
if [ -z "$GH_TOKEN" ]; then
    echo "ERROR: GH_TOKEN not found in $ENV_FILE."
    exit 1
fi

if ! "$CONTAINER_RUNTIME" ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Core container ($CONTAINER_NAME) is not running. Starting it..."
    compose_cmd up -d orchestrator
    echo "Waiting for container to initialize..."
    sleep 5
else
    echo "Core container is running."
fi

if ! "$CONTAINER_RUNTIME" exec "$CONTAINER_NAME" command -v gh >/dev/null 2>&1; then
    echo "Installing GitHub CLI (gh) inside the core container..."
    "$CONTAINER_RUNTIME" exec -u root "$CONTAINER_NAME" sh -lc "$(install_gh_cmd)"
else
    echo "GitHub CLI (gh) is already installed in the core container."
fi

echo "Authenticating GitHub CLI..."
"$CONTAINER_RUNTIME" exec -e TEMP_TOKEN="$GH_TOKEN" "$CONTAINER_NAME" sh -lc 'echo "$TEMP_TOKEN" | gh auth login --with-token && gh auth status'

echo "=== Setup Complete ==="
echo "GitHub CLI is installed and authenticated inside the Core container."
