from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    test_file = root / "core" / "test" / "test_preflight_core_suite.py"
    cmd = [sys.executable, "-m", "pytest", "-q", str(test_file)]
    return subprocess.call(cmd, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
