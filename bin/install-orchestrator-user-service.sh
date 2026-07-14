#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/sourcevcode-orchestrator-host.service"

mkdir -p "$UNIT_DIR"

cat >"$UNIT_FILE" <<UNIT
[Unit]
Description=SourceVCode Orchestrator Host Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
ExecStart=$ROOT_DIR/bin/orchestrator-host.sh start-foreground
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT

echo "installed=$UNIT_FILE"
echo "next:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now sourcevcode-orchestrator-host.service"
