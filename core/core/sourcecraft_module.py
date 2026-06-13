from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kernel_protocol import KernelAPI, KernelModule
from .models import RoleProfile


SOURCECRAFT_REPO_KEYWORDS = (
    "repo",
    "repository",
    "worktree",
    "branch",
    "status",
    "diff",
    "clone",
    "checkout",
)
SOURCECRAFT_PR_KEYWORDS = (
    "pull request",
    "pr ",
    " pr",
    "merge request",
    "review",
    "patch",
)
SOURCECRAFT_ISSUE_KEYWORDS = (
    "issue",
    "label",
    "milestone",
    "triage",
)
SOURCECRAFT_RELEASE_KEYWORDS = (
    "release",
    "changelog",
    "notes",
    "tag",
)
SOURCECRAFT_DOCS_KEYWORDS = (
    "docs",
    "documentation",
    "explain",
    "summary",
    "commit message",
    "commit log",
)
SOURCECRAFT_CAPABILITIES = (
    "sourcecraft",
    "repo_ops",
    "pr_flow",
    "release_flow",
    "issue_flow",
    "branch_governance",
)
PROTECTED_BRANCH_NAMES = {"main", "master", "prod", "production"}
SOURCECRAFT_BRANCH_PREFIXES = ("feat/", "feature/", "fix/", "chore/", "docs/", "refactor/", "test/", "hotfix/", "release/")
TOKEN_REDACTION_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.=]+", re.IGNORECASE),
]


SOURCECRAFT_VERIFICATION_KEYWORDS = (
    "test",
    "tests",
    "ci",
    "verification",
    "checklist",
    "health",
    "quota",
    "workflow",
)


@dataclass(slots=True)
class SourceCraftModule(KernelModule):
    name: str = "sourcecraft"
    _api: KernelAPI | None = None
    _binary: str | None = None
    _version: str | None = None
    _status: str = "idle"
    _last_error: str | None = None
    _last_probe: dict[str, Any] = field(default_factory=dict)
    _host_bridge: Any | None = None
    _runtime_health: dict[str, Any] = field(default_factory=dict)
    _preview_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    _audit_log: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _timeout_sec() -> float:
        raw = os.getenv("SOURCECRAFT_CLI_TIMEOUT_SEC", "10").strip()
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 10.0

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _candidate_bins(self) -> list[str]:
        env_bin = os.getenv("SOURCECRAFT_CLI_BIN", "").strip()
        if env_bin:
            return [env_bin]

        candidates = []
        candidates.append(str(self._repo_root() / ".tooling" / "sourcecraft" / "bin" / "src"))
        resolved = shutil.which("src")
        if resolved:
            candidates.append(resolved)
        return [candidate for candidate in candidates if candidate]

    def _resolve_binary(self) -> str | None:
        for candidate in self._candidate_bins():
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return None

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if not self._binary:
            raise FileNotFoundError("SourceCraft CLI binary not resolved")
        return subprocess.run(
            [self._binary, *args],
            capture_output=True,
            text=True,
            timeout=self._timeout_sec(),
            check=False,
        )

    def _probe_version(self) -> dict[str, Any]:
        attempts = [["version"], ["--version"], ["-v"]]
        errors: list[str] = []
        for args in attempts:
            try:
                proc = self._run(args)
            except Exception as exc:
                errors.append(f"{shlex.join([self._binary or 'src', *args])}: {exc}")
                continue

            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            if proc.returncode == 0 and stdout:
                return {
                    "ok": True,
                    "command": args,
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": proc.returncode,
                }

            errors.append(
                f"{shlex.join([self._binary or 'src', *args])}: rc={proc.returncode} stdout={stdout[:200]} stderr={stderr[:200]}"
            )

        return {"ok": False, "errors": errors}

    @staticmethod
    def _use_cases() -> list[dict[str, str]]:
        return [
            {"task": "repo ops", "fit": "clone, init, repo, browse, status"},
            {"task": "PR flow", "fit": "pr, prdescription, codereview"},
            {"task": "issue flow", "fit": "issue, label, milestone, triage"},
            {"task": "release flow", "fit": "release, changelog, tag, publish"},
            {"task": "agentic code", "fit": "code, do, skill, run"},
            {"task": "governance", "fit": "quota, update, auth, envs"},
        ]

    @staticmethod
    def _delegation_matrix() -> list[dict[str, str]]:
        return [
            {"task_family": "repo status / worktree diff", "handler": "SourceCraft"},
            {"task_family": "PR draft / PR description", "handler": "SourceCraft"},
            {"task_family": "issue triage / labels / milestones", "handler": "SourceCraft"},
            {"task_family": "release notes / changelog", "handler": "SourceCraft"},
            {"task_family": "branch governance / protected branch policy", "handler": "SourceCraft"},
            {"task_family": "feature code implementation", "handler": "codex-main or frontend-dev-1"},
            {"task_family": "tests / CI verification", "handler": "tester-1 or codex-main"},
            {"task_family": "review / security review", "handler": "reviewer-1"},
            {"task_family": "docs / UI design", "handler": "frontend-design-1 or antigravity-cli-1"},
        ]

    @staticmethod
    def _role_profile() -> RoleProfile:
        return RoleProfile(
            name="sourcecraft",
            title="SourceCraft Developer Copilot",
            description="Developer assistant role for SourceCraft flows that coordinates repository operations, PR work, and delivery hygiene.",
            summary="Developer assistant role for SourceCraft flows that coordinates repository operations, PR work, and delivery hygiene.",
            capabilities=list(SOURCECRAFT_CAPABILITIES) + ["governance"],
            responsibilities=[
                "summarize repository state and worktree changes",
                "draft and review pull requests and descriptions",
                "triage issues, milestones, labels, and release notes",
                "enforce branch naming and protected branch governance before publish flows",
                "prepare task breakdowns for code, docs, tests, and repo maintenance",
                "surface CI, quota, and workflow status to the orchestrator",
            ],
            supported_task_types=["code", "fix", "review", "docs", "research", "plan"],
            supported_capabilities=list(SOURCECRAFT_CAPABILITIES) + ["governance"],
            pipeline_stages=["intake", "repo_analysis", "task_planning", "drafting", "review_handoff", "delivery_handoff"],
            guardrails=[
                "do not bypass security gates or destructive confirmations",
                "keep repository mutations explicit and reviewable",
                "prefer concise, traceable diffs and PR descriptions",
                "escalate production, secret, and billing changes to the orchestrator",
            ],
        )

    @staticmethod
    def _task_text(task: Any, context: dict[str, Any] | None = None) -> str:
        pieces: list[str] = []
        if context:
            for key in ("description", "objective", "message", "prompt", "summary"):
                value = context.get(key)
                if isinstance(value, str) and value.strip():
                    pieces.append(value.strip())
        description = str(getattr(getattr(task, "input", None), "description", "") or "").strip()
        if description:
            pieces.append(description)
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).strip()
        if task_type:
            pieces.append(task_type)
        files = getattr(getattr(task, "input", None), "files", []) or []
        if isinstance(files, list):
            pieces.extend(str(item) for item in files if str(item).strip())
        constraints = getattr(getattr(task, "input", None), "constraints", []) or []
        if isinstance(constraints, list):
            pieces.extend(str(item) for item in constraints if str(item).strip())
        return " ".join(pieces).lower()

    @staticmethod
    def _task_family(task_text: str, required_capability: str | None = None) -> str:
        capability = str(required_capability or "").strip().lower()
        if capability in {"repo_ops", "pr_flow", "release_flow", "issue_flow", "branch_governance"}:
            return capability
        if any(keyword in task_text for keyword in SOURCECRAFT_REPO_KEYWORDS):
            return "repo_ops"
        if any(keyword in task_text for keyword in SOURCECRAFT_PR_KEYWORDS):
            return "pr_flow"
        if any(keyword in task_text for keyword in SOURCECRAFT_RELEASE_KEYWORDS):
            return "release_flow"
        if any(keyword in task_text for keyword in SOURCECRAFT_ISSUE_KEYWORDS):
            return "issue_flow"
        if any(keyword in task_text for keyword in ("branch policy", "governance", "protected branch", "branch naming")):
            return "branch_governance"
        if any(keyword in task_text for keyword in SOURCECRAFT_DOCS_KEYWORDS):
            return "docs_workflow"
        if any(keyword in task_text for keyword in SOURCECRAFT_VERIFICATION_KEYWORDS):
            return "verification"
        return "general"

    @staticmethod
    def _recommended_actions(task_family: str) -> list[str]:
        mapping = {
            "repo_ops": [
                "summarize repository state",
                "prepare a clean worktree summary",
                "list the next repository actions",
            ],
            "pr_flow": [
                "draft the pull request description",
                "summarize the code changes for reviewers",
                "highlight review and merge risks",
            ],
            "release_flow": [
                "draft release notes",
                "compare release candidate inputs",
                "prepare a safe publish checklist",
            ],
            "issue_flow": [
                "triage issue state",
                "group labels and milestones",
                "prepare a concise issue handoff summary",
            ],
            "branch_governance": [
                "validate branch naming policy",
                "highlight protected branch risks",
                "prepare a merge policy summary",
            ],
            "docs_workflow": [
                "produce a readable explanation of the change",
                "draft documentation or commit text",
                "summarize architecture or workflow impact",
            ],
            "verification": [
                "build a test plan",
                "list the checks that should run in the core",
                "prepare a human-readable health summary",
            ],
        }
        return mapping.get(task_family, [
            "summarize the task",
            "prepare a handoff for the core",
        ])

    @staticmethod
    def _core_retained_actions() -> list[str]:
        return [
            "security enforcement",
            "provider routing",
            "scheduler decisions",
            "budget controls",
            "mutating execution",
            "failover and retries",
        ]

    @staticmethod
    def _supported_actions() -> list[str]:
        return [
            "repo_summary",
            "status",
            "current_branch",
            "list_branches",
            "remote_branches",
            "sync_with_main",
            "prepare_feature_branch",
            "create_branch",
            "checkout_branch",
            "push_branch",
            "create_pr",
            "open_pr_with_template",
            "pr_checks",
            "merge_branch",
            "merge_pr",
            "merge_pr_safely",
            "repo_governance_report",
            "release_prepare",
        ]

    @staticmethod
    def _mutating_actions() -> set[str]:
        return {
            "create_branch",
            "checkout_branch",
            "prepare_feature_branch",
            "push_branch",
            "create_pr",
            "open_pr_with_template",
            "merge_branch",
            "merge_pr",
            "merge_pr_safely",
        }

    @staticmethod
    def _preview_required_actions() -> set[str]:
        return {"push_branch", "create_pr", "open_pr_with_template", "merge_pr", "merge_pr_safely"}

    def _bridge_mode(self) -> str:
        if self._host_bridge and hasattr(self._host_bridge, "detect_mode"):
            try:
                return str(self._host_bridge.detect_mode())
            except Exception:
                return "unknown"
        return "direct"

    def _command_availability(self) -> dict[str, bool]:
        has_dh_alias = bool(self._host_bridge and hasattr(self._host_bridge, "distrobox_bridge")) or shutil.which("dh") is not None
        tools = {
            "src": bool(self._binary and Path(self._binary).is_file()),
            "git": shutil.which("git") is not None,
            "gh": shutil.which("gh") is not None,
            "dh": has_dh_alias,
        }
        return tools

    @staticmethod
    def _redact_text(value: str) -> str:
        redacted = value or ""
        for pattern in TOKEN_REDACTION_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def _redact_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        for key in ("stdout", "stderr"):
            if key in redacted and isinstance(redacted[key], str):
                redacted[key] = self._redact_text(redacted[key])
        return redacted

    @staticmethod
    def _protected_branch(branch: str | None) -> bool:
        normalized = str(branch or "").strip().lower()
        return normalized in PROTECTED_BRANCH_NAMES or normalized.startswith("release/")

    @staticmethod
    def _branch_policy(branch: str | None) -> dict[str, Any]:
        normalized = str(branch or "").strip()
        valid = bool(normalized) and (normalized in PROTECTED_BRANCH_NAMES or normalized.startswith(SOURCECRAFT_BRANCH_PREFIXES))
        return {
            "branch": normalized or None,
            "valid": valid,
            "protected": SourceCraftModule._protected_branch(normalized),
            "recommended_prefixes": list(SOURCECRAFT_BRANCH_PREFIXES),
        }

    def _record_preview(self, *, action: str, repo_path: str, command: Any, branch: str | None, target_branch: str | None, repo_slug: str | None) -> str:
        raw = json.dumps(
            {
                "action": action,
                "repo_path": str(Path(repo_path).resolve()),
                "command": command,
                "branch": branch,
                "target_branch": target_branch,
                "repo_slug": repo_slug,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        token = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        self._preview_tokens[token] = {
            "action": action,
            "repo_path": str(Path(repo_path).resolve()),
            "command": command,
            "branch": branch,
            "target_branch": target_branch,
            "repo_slug": repo_slug,
            "created_at": datetime.now(UTC).isoformat(),
        }
        return token

    def _preview_token_valid(self, *, preview_token: str | None, action: str, repo_path: str, command: Any, branch: str | None, target_branch: str | None, repo_slug: str | None) -> bool:
        if not preview_token:
            return False
        expected = self._preview_tokens.get(preview_token)
        if not expected:
            return False
        probe = {
            "action": action,
            "repo_path": str(Path(repo_path).resolve()),
            "command": command,
            "branch": branch,
            "target_branch": target_branch,
            "repo_slug": repo_slug,
        }
        return all(expected.get(key) == value for key, value in probe.items())

    def _audit_repo_action(self, *, session_id: str | None, actor_id: str, action: str, command: Any, result: dict[str, Any], success: bool) -> None:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "command": command,
            "result": self._redact_result(result),
            "success": success,
        }
        self._audit_log.append(event)
        if len(self._audit_log) > 50:
            self._audit_log = self._audit_log[-50:]
        if not self._api:
            return
        try:
            memory = self._api.get_memory()
        except Exception:
            memory = None
        if memory is not None and hasattr(memory, "hybrid") and hasattr(memory.hybrid, "remember_command"):
            try:
                memory.hybrid.remember_command(
                    session_id=session_id or "sourcecraft-runtime",
                    agent_id=actor_id,
                    command=json.dumps(command, ensure_ascii=True) if not isinstance(command, str) else command,
                    result=self._redact_result(result),
                    success=success,
                )
            except Exception:
                pass

    def _run_command(self, command: list[str], *, repo_path: str = ".", timeout_sec: float | None = None) -> dict[str, Any]:
        resolved_repo = str(Path(repo_path or ".").resolve())
        if self._host_bridge and hasattr(self._host_bridge, "execute"):
            proc = self._host_bridge.execute(
                command,
                cwd=resolved_repo,
                timeout=int(timeout_sec or self._timeout_sec()),
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            proc = subprocess.run(
                command,
                cwd=resolved_repo,
                capture_output=True,
                text=True,
                timeout=timeout_sec or self._timeout_sec(),
                check=False,
            )
        return {
            "command": command,
            "repo_path": resolved_repo,
            "returncode": proc.returncode,
            "stdout": self._redact_text((proc.stdout or "").strip()),
            "stderr": self._redact_text((proc.stderr or "").strip()),
            "ok": proc.returncode == 0,
        }

    def _run_sourcecraft_command(self, args: list[str], *, repo_path: str = ".", timeout_sec: float | None = None) -> dict[str, Any]:
        if not self._binary:
            return {
                "command": ["src", *args],
                "repo_path": str(Path(repo_path or ".").resolve()),
                "returncode": 127,
                "stdout": "",
                "stderr": "SourceCraft CLI binary not resolved",
                "ok": False,
            }
        return self._run_command([self._binary, *args], repo_path=repo_path, timeout_sec=timeout_sec)

    def _resolve_repo_slug(self, repo_path: str) -> str | None:
        remote = self._run_command(["git", "remote", "get-url", "origin"], repo_path=repo_path)
        if not remote.get("ok"):
            return None
        raw = str(remote.get("stdout") or "").strip()
        if not raw:
            return None
        normalized = raw
        if normalized.endswith('.git'):
            normalized = normalized[:-4]
        if 'github.com/' in normalized:
            normalized = normalized.split('github.com/', 1)[1]
        elif 'github.com:' in normalized:
            normalized = normalized.split('github.com:', 1)[1]
        normalized = normalized.strip('/').strip()
        if normalized.count('/') >= 1:
            owner, repo = normalized.split('/', 1)
            return f"{owner}/{repo}"
        return None

    def ensure_ready(self, *, repo_path: str = ".") -> dict[str, Any]:
        repo_slug = self._resolve_repo_slug(repo_path)
        try:
            ghbox_probe = self._run_command(["dh", "sh", "-lc", "command -v gh >/dev/null 2>&1"], repo_path=repo_path)
        except Exception:
            ghbox_probe = {"ok": False, "stderr": "ghbox bridge unavailable"}
        try:
            gh_auth = self._run_command(["dh", "gh", "auth", "status"], repo_path=repo_path)
        except Exception:
            gh_auth = {"ok": shutil.which("gh") is not None, "stdout": "direct gh available" if shutil.which("gh") else "", "stderr": ""}
        try:
            git_name = self._run_command(["dh", "git", "config", "--global", "user.name"], repo_path=repo_path)
            git_email = self._run_command(["dh", "git", "config", "--global", "user.email"], repo_path=repo_path)
        except Exception:
            git_name = self._run_command(["git", "config", "user.name"], repo_path=repo_path)
            git_email = self._run_command(["git", "config", "user.email"], repo_path=repo_path)
        branch_probe = self._run_command(["git", "symbolic-ref", "--short", "HEAD"], repo_path=repo_path)
        try:
            repo_access = self._run_command(["dh", "gh", "repo", "view", repo_slug], repo_path=repo_path) if repo_slug else {"ok": False, "stderr": "repo slug unavailable"}
        except Exception:
            repo_access = {"ok": False, "stderr": "repo access check unavailable"}
        report = {
            "status": "ready",
            "checked_at": datetime.now(UTC).isoformat(),
            "src_ready": bool(self._binary),
            "src_version": self._version,
            "ghbox_ready": bool(ghbox_probe.get("ok")),
            "gh_auth_ready": bool(gh_auth.get("ok")),
            "token_scope_ok": bool(repo_access.get("ok")) if repo_slug else bool(gh_auth.get("ok")),
            "git_identity": {
                "name": str(git_name.get("stdout") or "").strip() or None,
                "email": str(git_email.get("stdout") or "").strip() or None,
                "configured": bool((git_name.get("stdout") or "").strip() and (git_email.get("stdout") or "").strip()),
            },
            "origin_ready": bool(repo_slug),
            "repo_slug": repo_slug,
            "detached_head": not bool((branch_probe.get("stdout") or "").strip()),
            "current_branch": str(branch_probe.get("stdout") or "").strip() or None,
            "warnings": [],
        }
        if not report["src_ready"]:
            report["status"] = "error"
            report["warnings"].append("src binary not available")
        if not report["ghbox_ready"]:
            report["status"] = "degraded"
            report["warnings"].append("ghbox bridge is not ready")
        if not report["gh_auth_ready"]:
            report["status"] = "degraded"
            report["warnings"].append("GitHub authentication is not ready")
        if not report["git_identity"]["configured"]:
            report["status"] = "degraded"
            report["warnings"].append("Git identity is not configured")
        if not report["origin_ready"]:
            report["status"] = "degraded"
            report["warnings"].append("origin remote could not be resolved")
        if report["detached_head"]:
            report["status"] = "degraded"
            report["warnings"].append("repository is in detached HEAD state")
        self._runtime_health = report
        return report

    def build_delegation_profile(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        text = self._task_text(task, context)
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).lower()
        required_capability = str(getattr(task, "required_capability", "") or "").strip().lower()
        task_family = self._task_family(text, required_capability)
        is_sourcecraft_task = required_capability in SOURCECRAFT_CAPABILITIES or task_family != "general" or task_type in {"plan", "docs", "research"}
        should_delegate = task_family in {"repo_ops", "pr_flow", "release_flow", "issue_flow", "branch_governance", "docs_workflow", "verification"} or required_capability in SOURCECRAFT_CAPABILITIES
        return {
            "enabled": self._status in {"ready", "degraded"},
            "status": self._status,
            "task_family": task_family,
            "task_type": task_type or None,
            "is_sourcecraft_task": is_sourcecraft_task,
            "should_delegate": should_delegate,
            "recommended_owner": "sourcecraft" if should_delegate else "core",
            "delegation_mode": "execution_service" if should_delegate else "core",
            "core_retained_actions": self._core_retained_actions(),
            "sourcecraft_actions": self._recommended_actions(task_family),
            "sourcecraft_role": self._role_profile().as_dict(),
            "runtime_status": self._runtime_health.get("status", self._status),
        }

    def build_execution_plan(self, task: Any, context: dict[str, Any] | None = None):
        from .models import ExecutionPlan, Task, TaskInput, TaskType
        text = self._task_text(task, context)
        required_capability = str(getattr(task, "required_capability", "") or "").strip().lower()
        task_family = self._task_family(text, required_capability)
        if required_capability not in SOURCECRAFT_CAPABILITIES and task_family == "general":
            return None
        root = Task(
            TaskType.PLAN,
            TaskInput(f"SourceCraft bootstrap: {getattr(getattr(task, 'input', None), 'description', '')}", acceptance_criteria=["SourceCraft runtime checked"]),
            task.context,
            priority=task.priority,
            parent_task_id=getattr(task, 'task_id', None),
        )
        root.required_capability = "sourcecraft"
        root.routing_hints = {"source": "sourcecraft_runtime", "task_family": task_family}
        repo_scan = Task(
            TaskType.RESEARCH,
            TaskInput("Inspect repository state, branch topology, and remote metadata", acceptance_criteria=["repository state summarized"]),
            task.context,
            priority=task.priority,
            parent_task_id=getattr(task, 'task_id', None),
            dependencies=[root.task_id],
        )
        repo_scan.required_capability = "sourcecraft"
        repo_scan.routing_hints = {"source": "sourcecraft_runtime", "parallel_group": "sourcecraft_intake"}
        policy_audit = Task(
            TaskType.REVIEW,
            TaskInput("Validate branch naming, protected branch policy, auth readiness, and mutation safety", acceptance_criteria=["policy risks reported"]),
            task.context,
            priority=task.priority,
            parent_task_id=getattr(task, 'task_id', None),
            dependencies=[root.task_id],
        )
        policy_audit.required_capability = "sourcecraft"
        policy_audit.routing_hints = {"source": "sourcecraft_runtime", "parallel_group": "sourcecraft_intake"}
        workflow = Task(
            TaskType.PLAN,
            TaskInput(f"Plan SourceCraft workflow for {task_family}", acceptance_criteria=["workflow plan approved"]),
            task.context,
            priority=task.priority,
            parent_task_id=getattr(task, 'task_id', None),
            dependencies=[repo_scan.task_id, policy_audit.task_id],
        )
        workflow.required_capability = "sourcecraft"
        workflow.routing_hints = {"source": "sourcecraft_runtime", "task_family": task_family}
        verification = Task(
            TaskType.TEST,
            TaskInput("Prepare dry-run verification, checks, and audit expectations", acceptance_criteria=["verification strategy prepared"]),
            task.context,
            priority=task.priority,
            parent_task_id=getattr(task, 'task_id', None),
            dependencies=[workflow.task_id],
        )
        verification.required_capability = "sourcecraft"
        verification.routing_hints = {"source": "sourcecraft_runtime", "task_family": task_family}
        return ExecutionPlan(
            root_task_id=getattr(task, 'task_id', root.task_id),
            atomic_tasks=[root, repo_scan, policy_audit, workflow, verification],
            draft_layers=[
                {"name": "sourcecraft_runtime", "objective": root.input.description, "parallel": False},
                {"name": "sourcecraft_intake", "objective": "repository scan and policy audit", "parallel": True},
                {"name": "sourcecraft_workflow", "objective": workflow.input.description, "parallel": False},
                {"name": "sourcecraft_verification", "objective": verification.input.description, "parallel": False},
            ],
        )

    def _workflow_commands(self, action: str, *, branch: str | None, target_branch: str | None, remote: str) -> list[list[str]]:
        if action == "prepare_feature_branch":
            return [
                ["git", "status", "--short"],
                ["git", "fetch", remote, target_branch or "main"],
                ["git", "checkout", "-b", branch or ""],
            ]
        if action == "sync_with_main":
            return [
                ["git", "fetch", remote, target_branch or "main"],
                ["git", "rev-list", "--left-right", "--count", f"HEAD...{remote}/{target_branch or 'main'}"],
            ]
        return []

    def execute_repo_action(
        self,
        action: str,
        *,
        repo_path: str = ".",
        branch: str | None = None,
        target_branch: str | None = None,
        remote: str = "origin",
        allow_mutation: bool = False,
        dry_run: bool = False,
        extra_args: list[str] | None = None,
        repo_slug: str | None = None,
        title: str | None = None,
        description: str | None = None,
        pr_slug: str | None = None,
        reviewers: list[str] | None = None,
        draft: bool = False,
        squash: bool = False,
        rebase: bool = False,
        delete_branch: bool = False,
        wait: bool = True,
        preview_token: str | None = None,
        allow_production_repo: bool = False,
        allow_default_branch_merge: bool = False,
        session_id: str | None = None,
        actor_id: str = "sourcecraft",
    ) -> dict[str, Any]:
        normalized = str(action or "").strip().lower()
        extra = [str(item) for item in (extra_args or []) if str(item).strip()]
        resolved_repo_path = str(Path(repo_path or ".").resolve())
        availability = self._command_availability()
        resolved_branch = branch
        if not resolved_branch:
            current = self._run_command(["git", "branch", "--show-current"], repo_path=repo_path)
            if current.get("ok") and current.get("stdout"):
                resolved_branch = str(current["stdout"]).strip()
        resolved_repo_slug = repo_slug or self._resolve_repo_slug(repo_path)
        branch_policy = self._branch_policy(resolved_branch)
        target_branch_policy = self._branch_policy(target_branch)

        if normalized not in self._supported_actions():
            return {
                "status": "error",
                "action": normalized,
                "reason": "unsupported SourceCraft repo action",
                "supported_actions": self._supported_actions(),
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
            }

        if normalized in self._mutating_actions() and not allow_mutation:
            return {
                "status": "rejected",
                "action": normalized,
                "reason": "mutating SourceCraft action requires allow_mutation=true",
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                "repo_slug": resolved_repo_slug,
            }

        if normalized in {"prepare_feature_branch", "create_branch", "push_branch"} and resolved_branch and not branch_policy["valid"]:
            return {
                "status": "rejected",
                "action": normalized,
                "reason": "branch name does not follow the recommended trunk-based naming policy",
                "branch_policy": branch_policy,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
            }

        if (not dry_run) and target_branch_policy["protected"] and normalized in {"create_pr", "open_pr_with_template", "merge_pr", "merge_pr_safely", "merge_branch"} and not allow_production_repo:
            return {
                "status": "rejected",
                "action": normalized,
                "reason": "protected target branches require allow_production_repo=true",
                "target_branch_policy": target_branch_policy,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
            }
        if (not dry_run) and target_branch_policy["protected"] and normalized in {"merge_pr", "merge_pr_safely", "merge_branch"} and not allow_default_branch_merge:
            return {
                "status": "rejected",
                "action": normalized,
                "reason": "default branch merges require allow_default_branch_merge=true",
                "target_branch_policy": target_branch_policy,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
            }

        runner = "git"
        command: list[str] | None = None
        workflow: list[list[str]] | None = None

        if normalized in {"repo_summary", "status"}:
            command = ["git", "status", "--short", "--branch"]
        elif normalized == "current_branch":
            command = ["git", "branch", "--show-current"]
        elif normalized == "list_branches":
            command = ["git", "branch", "--format=%(refname:short)"]
        elif normalized == "remote_branches":
            if not resolved_repo_slug:
                return {"status": "error", "action": normalized, "reason": "repository slug could not be resolved from origin remote", "bridge_mode": self._bridge_mode(), "tools": availability}
            runner = "src"
            command = ["repo", "list-branches", "-R", resolved_repo_slug, "--json"]
        elif normalized == "sync_with_main":
            runner = "workflow"
            workflow = self._workflow_commands(normalized, branch=resolved_branch, target_branch=target_branch, remote=remote)
        elif normalized == "prepare_feature_branch":
            runner = "workflow"
            workflow = self._workflow_commands(normalized, branch=resolved_branch, target_branch=target_branch, remote=remote)
        elif normalized == "create_branch":
            if not resolved_branch:
                return {"status": "error", "action": normalized, "reason": "branch is required", "bridge_mode": self._bridge_mode(), "tools": availability}
            command = ["git", "checkout", "-b", resolved_branch]
        elif normalized == "checkout_branch":
            if not resolved_branch:
                return {"status": "error", "action": normalized, "reason": "branch is required", "bridge_mode": self._bridge_mode(), "tools": availability}
            command = ["git", "checkout", resolved_branch]
        elif normalized == "push_branch":
            if not resolved_branch:
                return {"status": "error", "action": normalized, "reason": "branch is required", "bridge_mode": self._bridge_mode(), "tools": availability}
            runner = "dh"
            command = ["dh", "git", "push", "-u", remote, resolved_branch]
        elif normalized in {"create_pr", "open_pr_with_template"}:
            if not resolved_repo_slug:
                return {"status": "error", "action": normalized, "reason": "repository slug could not be resolved from origin remote", "bridge_mode": self._bridge_mode(), "tools": availability}
            runner = "src"
            pr_title = title or f"{resolved_branch or 'feature'}: update repository workflow"
            pr_description = description or "## Summary\n\n- prepare the repository change set\n- validate branch policy and checks\n- keep merge flow explicit and reviewable"
            command = ["pr", "create", "-R", resolved_repo_slug, "--title", pr_title, "--base", target_branch or "main"]
            if resolved_branch:
                command.extend(["--head", resolved_branch])
            command.extend(["--description", pr_description])
            if draft:
                command.append("--draft")
            for reviewer in reviewers or []:
                command.extend(["--reviewer", reviewer])
        elif normalized == "pr_checks":
            runner = "src"
            command = ["pr", "checks"]
            if pr_slug:
                command.append(pr_slug)
        elif normalized == "merge_branch":
            if not resolved_branch or not target_branch:
                return {"status": "error", "action": normalized, "reason": "branch and target_branch are required", "bridge_mode": self._bridge_mode(), "tools": availability}
            command = ["git", "merge", "--no-ff", resolved_branch]
        elif normalized in {"merge_pr", "merge_pr_safely"}:
            runner = "src"
            if normalized == "merge_pr_safely":
                checks = self.execute_repo_action(
                    "pr_checks",
                    repo_path=repo_path,
                    pr_slug=pr_slug,
                    dry_run=False,
                    allow_mutation=False,
                    session_id=session_id,
                    actor_id=actor_id,
                )
                if checks.get("status") != "ok":
                    return {
                        "status": "rejected",
                        "action": normalized,
                        "reason": "pull request checks must pass before merge",
                        "checks": checks,
                        "bridge_mode": self._bridge_mode(),
                        "tools": availability,
                    }
            command = ["pr", "merge"]
            if pr_slug:
                command.append(pr_slug)
            if squash:
                command.append("--squash")
            if rebase:
                command.append("--rebase")
            if delete_branch:
                command.append("--delete-branch")
            if wait:
                command.append("--wait")
        elif normalized == "repo_governance_report":
            status = self._run_command(["git", "status", "--short", "--branch"], repo_path=repo_path)
            branches = self._run_command(["git", "branch", "--format=%(refname:short)"], repo_path=repo_path)
            return {
                "status": "ok",
                "action": normalized,
                "repo_path": resolved_repo_path,
                "repo_slug": resolved_repo_slug,
                "branch_policy": branch_policy,
                "target_branch_policy": target_branch_policy,
                "working_tree": status.get("stdout", ""),
                "local_branches": [item for item in str(branches.get("stdout") or "").splitlines() if item.strip()],
                "runtime": self._runtime_health,
            }
        elif normalized == "release_prepare":
            return {
                "status": "dry_run",
                "action": normalized,
                "runner": "src",
                "command": ["release", "create", "-R", resolved_repo_slug or "<owner/repo>", "--help"],
                "repo_path": resolved_repo_path,
                "repo_slug": resolved_repo_slug,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                "note": "Release publishing remains preview-only until explicit publish policy is added.",
            }

        payload_meta = {
            "branch_policy": branch_policy,
            "target_branch_policy": target_branch_policy,
            "repo_slug": resolved_repo_slug,
            "branch": resolved_branch,
            "target_branch": target_branch,
        }

        preview_subject: Any = workflow if workflow is not None else command
        if dry_run:
            preview = {
                "status": "dry_run",
                "action": normalized,
                "runner": runner,
                "command": command,
                "workflow": workflow,
                "repo_path": resolved_repo_path,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                **payload_meta,
            }
            if normalized in self._preview_required_actions():
                preview["preview_token"] = self._record_preview(
                    action=normalized,
                    repo_path=resolved_repo_path,
                    command=preview_subject,
                    branch=resolved_branch,
                    target_branch=target_branch,
                    repo_slug=resolved_repo_slug,
                )
            return preview

        if normalized in self._preview_required_actions() and not self._preview_token_valid(
            preview_token=preview_token,
            action=normalized,
            repo_path=resolved_repo_path,
            command=preview_subject,
            branch=resolved_branch,
            target_branch=target_branch,
            repo_slug=resolved_repo_slug,
        ):
            return {
                "status": "rejected",
                "action": normalized,
                "reason": "preview_token from a matching dry-run is required before mutating this workflow",
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                **payload_meta,
            }

        if runner == "dh" and not (self._host_bridge and hasattr(self._host_bridge, "execute")):
            return {
                "status": "error",
                "action": normalized,
                "reason": "dh bridge requires host bridge runtime",
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                **payload_meta,
            }

        if runner == "workflow" and workflow is not None:
            if normalized == "prepare_feature_branch":
                clean = self._run_command(workflow[0], repo_path=repo_path)
                if not clean.get("ok") or clean.get("stdout"):
                    return {
                        "status": "rejected",
                        "action": normalized,
                        "reason": "working tree must be clean before preparing a feature branch",
                        "working_tree": clean.get("stdout", ""),
                        **payload_meta,
                    }
            results = [self._run_command(step, repo_path=repo_path) for step in workflow]
            ok = all(item.get("ok") for item in results)
            response = {
                "status": "ok" if ok else "error",
                "action": normalized,
                "runner": runner,
                "workflow": workflow,
                "steps": results,
                "repo_path": resolved_repo_path,
                "bridge_mode": self._bridge_mode(),
                "tools": availability,
                **payload_meta,
            }
            self._audit_repo_action(session_id=session_id, actor_id=actor_id, action=normalized, command=workflow, result=response, success=ok)
            return response

        original_branch = None
        if normalized == "merge_branch" and target_branch:
            original_branch_result = self._run_command(["git", "branch", "--show-current"], repo_path=repo_path)
            if original_branch_result["ok"]:
                original_branch = original_branch_result["stdout"]
            checkout = self._run_command(["git", "checkout", target_branch], repo_path=repo_path)
            if not checkout["ok"]:
                return {
                    "status": "error",
                    "action": normalized,
                    "command": checkout["command"],
                    "runner": "git",
                    "repo_path": checkout["repo_path"],
                    "bridge_mode": self._bridge_mode(),
                    "tools": availability,
                    "stdout": checkout["stdout"],
                    "stderr": checkout["stderr"],
                    "returncode": checkout["returncode"],
                    **payload_meta,
                }

        if runner == "src":
            result = self._run_sourcecraft_command(command or [], repo_path=repo_path)
        else:
            result = self._run_command(command or [], repo_path=repo_path)

        payload = {
            "status": "ok" if result["ok"] else "error",
            "action": normalized,
            "runner": runner,
            "command": result["command"],
            "repo_path": result["repo_path"],
            "bridge_mode": self._bridge_mode(),
            "tools": availability,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "returncode": result["returncode"],
            **payload_meta,
        }
        if original_branch:
            payload["original_branch"] = original_branch
        self._audit_repo_action(session_id=session_id, actor_id=actor_id, action=normalized, command=command or [], result=payload, success=result["ok"])
        return payload

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        try:
            self._host_bridge = api.get_context("host_bridge")
        except Exception:
            self._host_bridge = None
        self._binary = self._resolve_binary()
        if not self._binary:
            self._status = "error"
            self._last_error = "SourceCraft CLI binary not found"
            self._last_probe = {"ok": False, "error": self._last_error, "binary_candidates": self._candidate_bins()}
            self._runtime_health = {"status": "error", "warnings": [self._last_error], "src_ready": False}
            api.log("warning", "[SOURCECRAFT] src binary not found; module loaded in degraded mode.")
            return

        self._last_probe = self._probe_version()
        if self._last_probe.get("ok"):
            self._version = str(self._last_probe.get("stdout") or "").splitlines()[0].strip()
            self._status = "ready"
            self._last_error = None
            api.log("info", f"[SOURCECRAFT] src ready: {self._version}")
            self.ensure_ready(repo_path=".")
        else:
            self._status = "degraded"
            self._last_error = "; ".join(self._last_probe.get("errors", []))
            self._runtime_health = {"status": "degraded", "warnings": [self._last_error], "src_ready": True}
            api.log("warning", f"[SOURCECRAFT] src probe degraded: {self._last_error}")

    def on_unload(self) -> None:
        self._status = "idle"
        self._last_error = None
        self._last_probe = {}
        self._runtime_health = {}
        self._preview_tokens = {}
        self._audit_log = []

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        description = str(getattr(getattr(task, "input", None), "description", "") or context.get("description") or "").lower()
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).lower()
        likely_repo_work = any(
            keyword in description
            for keyword in ("repo", "repository", "pr", "pull request", "issue", "release", "clone", "branch", "status", "quota", "sourcecraft", "src")
        ) or task_type in {"plan", "code", "fix", "review", "docs", "research"}

        role_profile = self._role_profile()
        delegation = self.build_delegation_profile(task, context)
        runtime = self._runtime_health or self.ensure_ready(repo_path=str(getattr(getattr(task, "context", None), "repo_path", ".") or "."))
        context["sourcecraft"] = {
            "enabled": self._status in {"ready", "degraded"},
            "binary": self._binary,
            "version": self._version,
            "status": self._status,
            "likely_repo_work": likely_repo_work,
            "use_cases": self._use_cases(),
            "delegation_matrix": self._delegation_matrix(),
            "role": role_profile.as_dict(),
            "delegation": delegation,
            "runtime": runtime,
            "execution": {
                "bridge_mode": self._bridge_mode(),
                "supported_actions": self._supported_actions(),
                "tools": self._command_availability(),
            },
        }

        if likely_repo_work:
            context["sourcecraft"]["recommended_flow"] = [
                "src status",
                "src repo",
                "src pr",
                "src issue",
                "src do",
            ]
        if delegation["should_delegate"]:
            context["sourcecraft"]["automation"] = {
                "owner": "sourcecraft",
                "task_family": delegation["task_family"],
                "actions": delegation["sourcecraft_actions"],
                "core_retained_actions": delegation["core_retained_actions"],
            }

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        sourcecraft = context.get("sourcecraft")
        if not isinstance(sourcecraft, dict):
            return
        output = getattr(result, "output", {})
        summary = ""
        if isinstance(output, dict):
            summary = str(output.get("summary", "") or "")
        sourcecraft["last_result"] = {
            "task_id": getattr(task, "task_id", None),
            "status": getattr(getattr(result, "status", None), "value", getattr(result, "status", None)),
            "summary": summary,
            "next_recommendations": list(getattr(result, "next_recommendations", []) or []),
        }

    def finalize(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "binary": self._binary,
            "version": self._version,
            "last_error": self._last_error,
            "probe": self._last_probe,
            "use_cases": self._use_cases(),
            "delegation_matrix": self._delegation_matrix(),
            "execution": {
                "bridge_mode": self._bridge_mode(),
                "supported_actions": self._supported_actions(),
                "tools": self._command_availability(),
            },
            "runtime": self._runtime_health,
            "audit_log": list(self._audit_log[-20:]),
            "delegation_examples": {
                "repo_ops": self._recommended_actions("repo_ops"),
                "pr_flow": self._recommended_actions("pr_flow"),
                "release_flow": self._recommended_actions("release_flow"),
                "issue_flow": self._recommended_actions("issue_flow"),
                "branch_governance": self._recommended_actions("branch_governance"),
                "docs_workflow": self._recommended_actions("docs_workflow"),
                "verification": self._recommended_actions("verification"),
            },
            "role": self._role_profile().as_dict(),
            "binary_hint": str(self._repo_root() / ".tooling" / "sourcecraft" / "bin" / "src"),
        }
