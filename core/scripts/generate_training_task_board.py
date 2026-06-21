from __future__ import annotations

import argparse
import json

from core.core.training_orchestration import build_experience_training_task_board


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a machine-readable orchestration board for the experience-training improvement wave.")
    parser.add_argument("--repo-path", default=None, help="Optional repo path to embed into generated task context.")
    parser.add_argument("--branch", default=None, help="Optional branch name to embed into generated task context.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    board = build_experience_training_task_board(repo_path=args.repo_path, branch=args.branch)
    print(json.dumps(board, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
