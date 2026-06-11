#!/bin/bash
set -e

echo "=== GitHub CLI Automation for AI Core ==="

ENV_FILE="/var/home/sanya/wisper/.env.gemini.local"
COMPOSE_FILE="/var/home/sanya/wisper/docker-compose.ai.yml"
CONTAINER_NAME="hebrew_ai_orchestrator"

# 1. Check if token exists
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    exit 1
fi

GH_TOKEN=$(grep -E '^(GH_TOKEN|GITHUB_TOKEN|GITHUB_API_KEY)=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' | tr -d "'" | head -n 1)

if [ -z "$GH_TOKEN" ]; then
    echo "ERROR: GH_TOKEN not found in $ENV_FILE."
    exit 1
fi

# 2. Check if container is running; if not, bring it up
if ! podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Core container ($CONTAINER_NAME) is not running. Starting it..."
    podman-compose -f "$COMPOSE_FILE" up -d orchestrator
    echo "Waiting for container to initialize..."
    sleep 5
else
    echo "Core container is running."
fi

# 3. Install gh inside the container if it doesn't exist
if ! podman exec "$CONTAINER_NAME" command -v gh >/dev/null 2>&1; then
    echo "Installing GitHub CLI (gh) inside the core container..."
    podman exec -u root "$CONTAINER_NAME" sh -c "apt-get update && apt-get install -y curl && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && apt-get update && apt-get install -y gh"
else
    echo "GitHub CLI (gh) is already installed in the core container."
fi

# 4. Authenticate gh using the token
echo "Authenticating GitHub CLI..."
podman exec -e TEMP_TOKEN="$GH_TOKEN" "$CONTAINER_NAME" sh -c "echo \"\$TEMP_TOKEN\" | gh auth login --with-token && gh auth status"

echo "=== Setup Complete ==="
echo "GitHub CLI is installed and authenticated inside the Core container."
