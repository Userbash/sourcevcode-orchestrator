#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_NAME="${AI_KERNEL_SERVICE_NAME:-ai-kernel.service}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
LOG_DIR="${AI_KERNEL_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/ai-kernel}"

mkdir -p "$UNIT_DIR" "$LOG_DIR"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=SourceVCode AI Kernel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
EnvironmentFile=-$PROJECT_ROOT/.env
EnvironmentFile=-$PROJECT_ROOT/.env.bridge
EnvironmentFile=-$PROJECT_ROOT/.env.gemini.local
Environment=AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE=false
Environment=AI_KERNEL_HOST=0.0.0.0
Environment=AI_KERNEL_PORT=${AI_KERNEL_PORT:-8012}
Environment=AI_KERNEL_LOG_PATH=$LOG_DIR/server.log
Environment=AI_KERNEL_PID_PATH=/tmp/ai-kernel-server.pid
ExecStart=$PROJECT_ROOT/scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh
Restart=on-failure
RestartSec=5
TimeoutStartSec=${AI_KERNEL_STARTUP_TIMEOUT_SEC:-300}
StandardOutput=append:$LOG_DIR/service.out.log
StandardError=append:$LOG_DIR/service.err.log

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
systemctl --user status "$SERVICE_NAME" --no-pager || true
