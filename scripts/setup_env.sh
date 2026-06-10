# Environment setup for Podman and Host Utilities
export PATH=/var/home/sanya/.local/bin:$PATH

# Aliases for standardization
alias docker=/var/home/sanya/.local/bin/podman
alias docker-compose='/var/home/sanya/.local/bin/podman-compose'
alias podman-compose='/var/home/sanya/.local/bin/podman-compose'

# Verify utilities presence
echo "Verifying environment:"
for cmd in podman podman-compose ss lsof netstat; do
    if command -v $cmd &> /dev/null; then
        echo "✓ $cmd found"
    else
        echo "✗ $cmd missing"
    fi
done
