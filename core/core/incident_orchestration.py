from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import Complexity, ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType


INCIDENT_TASK_BOARD_SCHEMA_VERSION = "incident-task-board.v1"


_INCIDENT_GLOBAL_CONSTRAINTS = [
    "Revoke and rotate secrets before any history rewrite or public cleanup is treated as complete.",
    "Do not assume git history cleanup alone remediates a leaked token.",
    "Do not delete audit evidence that is required for incident review.",
    "Keep repo-history work isolated from runtime secret rollout.",
]


_INCIDENT_VALIDATION_COMMANDS = [
    "python3 -m core.scripts.generate_incident_task_board --json",
    "git show --no-patch --oneline rewrite/39509cd-sanitized",
    "rg -n --hidden -S \"AIza[0-9A-Za-z_-]{20,}|github_pat_[A-Za-z0-9_]{6,}|sk-live-[A-Za-z0-9_-]+|sk-clb-[A-Za-z0-9_-]+\" .",
]


def _task(
    *,
    task_id: str,
    owner: str,
    task_type: TaskType,
    required_capability: str,
    description: str,
    files: list[str],
    constraints: list[str],
    acceptance_criteria: list[str],
    repo_path: str | None,
    branch: str | None,
    dependencies: list[str] | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        type=task_type,
        priority=Priority.CRITICAL,
        complexity=Complexity.CRITICAL,
        required_capability=required_capability,
        input=TaskInput(
            description=description,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        ),
        context=TaskContext(project="core", repo_path=repo_path, branch=branch),
        dependencies=list(dependencies or []),
        routing_hints={
            "worker_role": owner,
            "parallel_group": "secret-incident-wave-1",
            "preferred_agent_id": "orchestrator",
        },
    )


def _worker_prompt(owner: str) -> str:
    prompts = {
        "security_operator": """
Зона: revoke / rotate / audit.

Сначала:
- revoke/delete утекшие токены
- выпусти новые
- замени secrets в GitHub Actions, .env, контейнерах, VPS, k8s, registry
- собери audit findings

Не трогай git history.
Выдай: revoked secrets, rotated secrets, deployed targets, audit findings.
""".strip(),
        "git_history_surgeon": """
Зона: repo + history rewrite.

Сделай:
- поиск всех утечек в git history
- prepare replacements.txt
- git filter-repo plan
- exact force-push commands

Не трогай runtime secrets.
Учитывай локальный sanitized rewrite: rewrite/39509cd-sanitized -> edeaa70.
""".strip(),
        "public_surface_cleaner": """
Зона: GitHub / docs / CI logs / artifacts / PR comments.

Проверь:
- PR descriptions
- PR comments
- issues
- wiki
- CI logs
- artifacts
- release assets
- docs snippets

Выдай список мест с утечкой и план удаления.
""".strip(),
        "runtime_infra_rotator": """
Зона: deployment rollout.

Обнови:
- .env
- GitHub Actions secrets
- containers
- VPS
- k8s
- registry

Выдай deployment matrix и smoke results.
""".strip(),
        "validator_auditor": """
Зона: post-incident verification.

Проверь, что:
- старые токены отозваны
- history очищена
- публичные поверхности очищены
- сервисы используют новые секреты

Выдай финальный incident report и residual risks.
""".strip(),
    }
    return prompts[owner]


def _incident_roles() -> list[dict[str, str]]:
    return [
        {"owner": "security_operator", "phase": "serial", "responsibility": "Revoke, rotate, redeploy, and audit leaked secrets."},
        {"owner": "git_history_surgeon", "phase": "parallel", "responsibility": "Rewrite repository history and prepare force-push plan."},
        {"owner": "public_surface_cleaner", "phase": "parallel", "responsibility": "Remove leaked data from GitHub/public surfaces outside git history."},
        {"owner": "runtime_infra_rotator", "phase": "parallel", "responsibility": "Roll new secrets through runtime and infrastructure targets."},
        {"owner": "validator_auditor", "phase": "serial", "responsibility": "Validate incident closure and publish residual risks."},
    ]


def _incident_tasks(repo_path: str | None, branch: str | None) -> tuple[list[Task], str, str, str, str, str]:
    security_id = f"incident-security-{uuid4().hex[:8]}"
    history_id = f"incident-history-{uuid4().hex[:8]}"
    public_id = f"incident-public-{uuid4().hex[:8]}"
    runtime_id = f"incident-runtime-{uuid4().hex[:8]}"
    validate_id = f"incident-validate-{uuid4().hex[:8]}"

    tasks = [
        _task(
            task_id=security_id,
            owner="security_operator",
            task_type=TaskType.FIX,
            required_capability="ops",
            description="Revoke leaked secrets, rotate replacements, redeploy them, and capture audit evidence.",
            files=[".env", ".env.bridge", "GitHub Actions secrets", "container env", "VPS/k8s secret stores"],
            constraints=_INCIDENT_GLOBAL_CONSTRAINTS + ["Requires real provider and infrastructure access."],
            acceptance_criteria=[
                "Every known leaked secret is revoked.",
                "Replacement secrets are issued and mapped to deployment targets.",
                "Audit findings capture anomalous usage or confirm no abuse.",
            ],
            repo_path=repo_path,
            branch=branch,
        ),
        _task(
            task_id=history_id,
            owner="git_history_surgeon",
            task_type=TaskType.FIX,
            required_capability="git",
            description="Prepare and execute repository history cleanup for leaked secret literals without touching runtime rollout.",
            files=[
                ".git history",
                "core/test/test_provider_credentials.py",
                "core/test/test_gh_auth_bridge.py",
                "core/test/test_openai_provider.py",
                "core/test/test_openai_runtime_router.py",
                "core/test/test_availability.py",
            ],
            constraints=_INCIDENT_GLOBAL_CONSTRAINTS + [
                "Do not begin force-push until revoke/rotate is complete.",
                "Preserve local sanitized reference rewrite/39509cd-sanitized -> edeaa70.",
            ],
            acceptance_criteria=[
                "replacements.txt is complete for all leaked literals.",
                "All affected refs and branches are enumerated.",
                "Exact force-push commands are ready.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[security_id],
        ),
        _task(
            task_id=public_id,
            owner="public_surface_cleaner",
            task_type=TaskType.REVIEW,
            required_capability="ops",
            description="Remove leaked values from public GitHub surfaces, CI logs, artifacts, and documentation copies.",
            files=["PR descriptions", "PR comments", "issues", "wiki", "CI logs", "artifacts", "release assets", "docs snippets"],
            constraints=_INCIDENT_GLOBAL_CONSTRAINTS + ["Requires GitHub/admin access for deletion or redaction of public artifacts."],
            acceptance_criteria=[
                "Public leakage surfaces are inventoried.",
                "Each removable copy is deleted or redacted.",
                "Residual manual cleanup items are listed explicitly.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[security_id],
        ),
        _task(
            task_id=runtime_id,
            owner="runtime_infra_rotator",
            task_type=TaskType.FIX,
            required_capability="ops",
            description="Roll rotated secrets through deployment targets and verify old tokens are no longer in use.",
            files=[".env", "compose", "k8s", "systemd", "cloud vars", "registry credentials"],
            constraints=_INCIDENT_GLOBAL_CONSTRAINTS + ["Requires deployment access to containers, VPS, kubernetes, and secret stores."],
            acceptance_criteria=[
                "Deployment matrix lists every updated environment.",
                "Services restart or reload with new secrets.",
                "Smoke checks confirm the old tokens are no longer active in runtime.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[security_id],
        ),
        _task(
            task_id=validate_id,
            owner="validator_auditor",
            task_type=TaskType.TEST,
            required_capability="audit",
            description="Validate incident closure across revoke, history cleanup, public cleanup, and runtime rollout.",
            files=["incident report", "audit logs", "rewritten refs", "runtime smoke output"],
            constraints=_INCIDENT_GLOBAL_CONSTRAINTS + ["Do not mark the incident closed while any high-risk secret remains active or publicly reachable."],
            acceptance_criteria=[
                "Revoked and rotated secrets are verified.",
                "History cleanup is validated against rewritten refs.",
                "Residual risks are enumerated in the final incident report.",
            ],
            repo_path=repo_path,
            branch=branch,
            dependencies=[history_id, public_id, runtime_id],
        ),
    ]
    return tasks, security_id, history_id, public_id, runtime_id, validate_id


def build_secret_incident_execution_plan(*, repo_path: str | None = None, branch: str | None = None) -> ExecutionPlan:
    tasks, security_id, _history_id, _public_id, _runtime_id, _validate_id = _incident_tasks(repo_path, branch)
    return ExecutionPlan(
        root_task_id=security_id,
        atomic_tasks=tasks,
        draft_layers=[
            {
                "name": "secret_incident_response",
                "parallel": True,
                "objective": "Revoke leaked secrets first, then clean history/public surfaces and redeploy rotated credentials.",
            }
        ],
    )


def build_secret_incident_task_board(*, repo_path: str | None = None, branch: str | None = None) -> dict[str, Any]:
    plan = build_secret_incident_execution_plan(repo_path=repo_path, branch=branch)
    prompts = {owner["owner"]: _worker_prompt(owner["owner"]) for owner in _incident_roles()}

    task_details = {
        "security_operator": {
            "scope": ["revoke", "rotate", "audit"],
            "inputs": ["provider consoles", "secret inventory", "deployment target list", "audit log access"],
            "outputs": ["old_secret -> revoked_at", "new_secret -> deployed_targets", "audit_findings"],
            "blocking_conditions": ["No provider/admin access", "No infrastructure credential access"],
            "exact_commands": [
                "provider-specific revoke/delete in OpenAI/Mistral/GitHub/Google consoles",
                "update GitHub Actions secrets",
                "update .env and runtime secret stores",
            ],
            "done_criteria": [
                "Every leaked secret is revoked.",
                "Replacement secrets are deployed to all known targets.",
                "Audit log review is attached.",
            ],
        },
        "git_history_surgeon": {
            "scope": ["repo", "history rewrite", "force-push plan"],
            "inputs": ["leaked literal inventory", "affected refs", "rewrite/39509cd-sanitized -> edeaa70"],
            "outputs": ["replacements.txt", "rewritten_refs", "exact_force_push_commands", "branch_impact"],
            "blocking_conditions": ["Secrets not yet revoked/rotated", "Protected branches without change window"],
            "exact_commands": [
                "git clone --mirror <repo-url> repo-clean.git",
                "git filter-repo --replace-text replacements.txt --force",
                "git push --force --mirror",
                "git show --no-patch --oneline rewrite/39509cd-sanitized",
            ],
            "done_criteria": [
                "All leaked literals are removed from rewritten refs.",
                "Force-push plan is reviewed.",
                "Branch impact is documented.",
            ],
        },
        "public_surface_cleaner": {
            "scope": ["GitHub", "CI logs", "artifacts", "docs"],
            "inputs": ["repo URL", "CI systems", "release assets", "issue/PR inventory"],
            "outputs": ["public_surface_checklist", "removed_items", "manual_cleanup_items"],
            "blocking_conditions": ["No GitHub/admin access", "No log/artifact retention access"],
            "exact_commands": [
                "search PR descriptions/comments/issues/wiki for leaked literals",
                "remove or redact CI logs and artifacts",
                "delete or replace release assets and pasted snippets",
            ],
            "done_criteria": [
                "All discoverable public copies are removed or redacted.",
                "Remaining manual surfaces are explicitly listed.",
            ],
        },
        "runtime_infra_rotator": {
            "scope": ["deployment rollout", "env refresh", "smoke validation"],
            "inputs": ["new secrets", "deployment matrix", "compose/k8s/VPS access"],
            "outputs": ["updated_environment_list", "deployment_matrix", "smoke_report"],
            "blocking_conditions": ["No deployment access", "No rotated replacement secrets"],
            "exact_commands": [
                "update .env, secret stores, GitHub Actions secrets, containers, VPS, k8s, registry",
                "restart or reload services",
                "run service-specific smoke checks",
            ],
            "done_criteria": [
                "Every runtime target is updated.",
                "Old secrets are no longer in use.",
                "Smoke checks pass on updated services.",
            ],
        },
        "validator_auditor": {
            "scope": ["closure validation", "residual risk report"],
            "inputs": ["audit logs", "rewritten refs", "public cleanup report", "runtime smoke output"],
            "outputs": ["final_incident_report", "residual_risks"],
            "blocking_conditions": ["Any upstream phase incomplete"],
            "exact_commands": list(_INCIDENT_VALIDATION_COMMANDS),
            "done_criteria": [
                "History, public surfaces, and runtime are all validated.",
                "Residual risks are explicitly enumerated.",
            ],
        },
    }

    tasks = []
    for task in plan.atomic_tasks:
        owner = str(task.routing_hints.get("worker_role", ""))
        detail = task_details[owner]
        tasks.append({
            "task_id": task.task_id,
            "title": task.input.description,
            "owner": owner,
            "task_type": task.type.value,
            "required_capability": task.required_capability,
            "depends_on": list(task.dependencies),
            "parallelizable": owner in {"git_history_surgeon", "public_surface_cleaner", "runtime_infra_rotator"},
            "scope": detail["scope"],
            "inputs": detail["inputs"],
            "outputs": detail["outputs"],
            "blocking_conditions": detail["blocking_conditions"],
            "exact_commands": detail["exact_commands"],
            "done_criteria": detail["done_criteria"],
            "prompt": prompts[owner],
        })

    return {
        "schema_version": INCIDENT_TASK_BOARD_SCHEMA_VERSION,
        "objective": "Coordinate a secret-leak incident response across revoke/rotate, git history cleanup, public cleanup, runtime rollout, and final audit.",
        "context": {
            "incident_type": "secret_leak",
            "repo_path": repo_path,
            "branch": branch,
            "known_leak_commit": "39509cd",
            "sanitized_rewrite_commit": "edeaa70",
            "sanitized_rewrite_ref": "rewrite/39509cd-sanitized",
            "current_main_head": "107a4fd",
            "execution_order": [
                "security_operator",
                "git_history_surgeon",
                "public_surface_cleaner",
                "runtime_infra_rotator",
                "validator_auditor",
            ],
            "parallel_after_security_operator": [
                "git_history_surgeon",
                "public_surface_cleaner",
                "runtime_infra_rotator",
            ],
        },
        "global_constraints": list(_INCIDENT_GLOBAL_CONSTRAINTS),
        "worker_roles": _incident_roles(),
        "validation_commands": list(_INCIDENT_VALIDATION_COMMANDS),
        "tasks": tasks,
        "execution_plan": plan.as_dict(),
    }
