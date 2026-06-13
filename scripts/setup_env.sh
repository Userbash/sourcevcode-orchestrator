# Environment setup for Podman and Host Utilities
set_local_bin() {
    local local_bin="$HOME/.local/bin"
    case ":$PATH:" in
        *":$local_bin:"*) ;;
        *) export PATH="$local_bin:$PATH" ;;
    esac
}

set_local_bin

if [ -x "$HOME/.local/bin/podman" ]; then
    alias docker="$HOME/.local/bin/podman"
fi
if [ -x "$HOME/.local/bin/podman-compose" ]; then
    alias docker-compose="$HOME/.local/bin/podman-compose"
    alias podman-compose="$HOME/.local/bin/podman-compose"
fi

# Verify utilities presence
echo "Verifying environment:"
for cmd in podman podman-compose ss lsof netstat; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "✓ $cmd found"
    else
        echo "✗ $cmd missing"
    fi
done
