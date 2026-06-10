from __future__ import annotations

import os
import sys

from core.core.host_bridge import HostBridge, HostBridgeError


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    bridge = HostBridge()

    if not argv or argv[0] in {"-h", "--help"}:
        print("Usage: python -m core.scripts.host_bridge_cli <command> [args...]")
        return 1

    if argv[0] == "--init":
        bridge.ensure_whitelist()
        print(f"[OK] Whitelist initialized: {bridge.whitelist_file}")
        return 0

    try:
        bridge.validate(argv)
        mode = bridge.detect_mode()
        if argv[0] == "gh":
            bridge.gh_auth_bridge.ensure_authenticated(mode, bridge.distrobox_bridge, bridge._host_has_binary)
            
        translated = bridge._translate_gh(argv, mode) if argv[0] == "gh" else bridge.translate(argv)
    except HostBridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    os.execvp(translated[0], translated)


if __name__ == "__main__":
    raise SystemExit(main())
