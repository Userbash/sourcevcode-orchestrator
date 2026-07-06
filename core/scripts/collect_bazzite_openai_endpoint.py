from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.core.openai_bazzite_endpoint import write_openai_endpoint_discovery


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect OpenAI/Codex endpoint data from a Bazzite/Flatpak VS Code environment.")
    parser.add_argument("--output", default="", help="Output JSON path. Defaults to OPENAI_ENDPOINT_DISCOVERY_PATH or reports/openai_endpoint_discovery.json")
    parser.add_argument("--home", default="", help="Override HOME when collecting from another runtime root")
    args = parser.parse_args()

    payload = write_openai_endpoint_discovery(
        output_path=Path(args.output) if args.output else None,
        home=Path(args.home).expanduser() if args.home else None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("usable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
