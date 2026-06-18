from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from typing import Any

from core.core.orchestrator import Orchestrator


def _load_diagnostic_contracts() -> Any | None:
    try:
        return importlib.import_module("core.core.diagnostic_contracts")
    except ModuleNotFoundError as exc:
        if exc.name == "core.core.diagnostic_contracts":
            return None
        raise


def _available_layers() -> list[str]:
    contracts_module = _load_diagnostic_contracts()
    available = getattr(contracts_module, "available_layers", None) if contracts_module else None
    if callable(available):
        try:
            layers = available()
        except Exception:
            return []
        if isinstance(layers, (list, tuple, set)):
            return [str(item).strip() for item in layers if str(item).strip()]
    return []


def _matrix_snapshot() -> dict[str, Any]:
    contracts_module = _load_diagnostic_contracts()
    matrix = getattr(contracts_module, "diagnostic_matrix", None) if contracts_module else None
    if callable(matrix):
        try:
            payload = matrix()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return payload if isinstance(payload, dict) else {"value": payload}
    return {"status": "unavailable"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run orchestrator self diagnostics.")
    parser.add_argument(
        "--layer",
        action="append",
        default=[],
        help="Run only selected diagnostic layers. Repeat the flag or pass comma-separated values.",
    )
    parser.add_argument("--json", action="store_true", help="Emit raw JSON output for automation.")
    parser.add_argument("--matrix-only", action="store_true", help="Print only diagnostic contract metadata without running live checks.")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if args.matrix_only:
        payload = {
            "schema_version": _matrix_snapshot().get("schema_version", "legacy-self-diagnostic/v1"),
            "layers": _available_layers(),
            "matrix": _matrix_snapshot(),
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    if not args.json:
        print("Initializing Orchestrator and running Self-Diagnostics...")
    orch = Orchestrator()

    # Give some time for async on_load tasks to finish.
    await asyncio.sleep(2)

    diag_module = orch.get_module("self_diagnostic")
    if not diag_module:
        if args.json:
            print(json.dumps({"status": "error", "error": "self_diagnostic module not found"}, ensure_ascii=True, indent=2))
        else:
            print("Error: self_diagnostic module not found!")
        return 1

    report = await diag_module.run_diagnostics(layers=args.layer or None)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print("\n=== SYSTEM SELF-DIAGNOSTIC REPORT ===")
        print(json.dumps(report, indent=2))
        print("=====================================")
    return 0


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
