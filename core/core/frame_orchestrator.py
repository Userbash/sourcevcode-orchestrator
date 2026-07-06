from __future__ import annotations

from collections import defaultdict
import re
from typing import Any
from xml.sax.saxutils import escape

from pydantic import Field

from .models import CompatModel, Task


_TRANSPORT_NOISE_KEYS = {
    "ack",
    "action",
    "channel",
    "correlation_id",
    "final",
    "idempotency_key",
    "interactive",
    "request_id",
    "source",
    "timeout_ms",
    "type",
    "ws_priority",
}

_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("websocket", ("websocket", "ws ", " ws", "socket")),
    ("database", ("database", "db", "schema", "migration", "repository", "postgres", "sql", "cache")),
    ("validation", ("validate", "validation", "schema", "sanitize", "regex", "input")),
    ("security", ("security", "auth", "permission", "middleware", "try-catch", "try/catch", "guard")),
    ("testing", ("test", "qa", "assert", "mock", "integration")),
    ("api", ("api", "endpoint", "controller", "route", "handler")),
    ("frontend", ("frontend", "ui", "react", "view", "component")),
    ("backend", ("backend", "service", "worker", "domain")),
)

_ROLE_CAPABILITIES: dict[str, list[str]] = {
    "core_logic": ["code", "fix"],
    "database_storage": ["code", "fix"],
    "validation_security": ["review", "code", "fix"],
    "qa_test_automation": ["test", "code"],
}


class FrameIngestResult(CompatModel):
    raw_text: str
    stripped_text: str
    extracted_entities: dict[str, list[str]] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    transport_noise_removed: list[str] = Field(default_factory=list)


class FrameContextResult(CompatModel):
    base_layer_mapping: dict[str, list[str]] = Field(default_factory=dict)
    context_sync: dict[str, Any] = Field(default_factory=dict)
    collision_exclusion: dict[str, list[str]] = Field(default_factory=dict)
    merged_array_output: list[str] = Field(default_factory=list)


class FrameSemanticGapResult(CompatModel):
    text_only_audit: dict[str, Any] = Field(default_factory=dict)
    dependency_mapping: dict[str, list[str]] = Field(default_factory=dict)
    gap_scanner: list[str] = Field(default_factory=list)
    edge_case_check: list[str] = Field(default_factory=list)
    critical_voids_output: list[str] = Field(default_factory=list)


class FrameSocratiCodeResult(CompatModel):
    status: str = "unavailable"
    coverage_score: float = 0.0
    coverage_status: str = "low"
    prefer_low_cost_lanes: bool = False
    preferred_provider: str | None = None
    recommended_parallel_branches: int | None = None
    shared_index_ready: bool = False
    tools_used: list[str] = Field(default_factory=list)
    covered_files: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)
    compact_context_summary: str = ""


class FrameSocratiCodeContextCompactionResult(CompatModel):
    status: str = "disabled"
    compaction_mode: str = "raw_prompt"
    prompt_context_source: str = "task_description"
    raw_file_dump_allowed: bool = True
    token_reduction_expected: str = "low"
    recommended_prompt_strategy: str = "use standard file-bounded prompting"
    evidence: list[str] = Field(default_factory=list)


class FrameWorkerRole(CompatModel):
    role: str
    target_capability: str
    file_targets: list[str] = Field(default_factory=list)
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    focus_prompt: str = ""


class FrameValidationResult(CompatModel):
    best_practices_generation: list[str] = Field(default_factory=list)
    intent_match_cross_validation: dict[str, Any] = Field(default_factory=dict)
    architectural_fixes: list[str] = Field(default_factory=list)
    worker_roles: list[FrameWorkerRole] = Field(default_factory=list)
    xml_orchestrator_package_output: str = ""


class FrameOrchestratorPackage(CompatModel):
    status: str = "validated"
    ingest: FrameIngestResult
    context: FrameContextResult
    semantic_gap: FrameSemanticGapResult
    socraticode: FrameSocratiCodeResult = Field(default_factory=FrameSocratiCodeResult)
    socraticode_context_compaction: FrameSocratiCodeContextCompactionResult = Field(default_factory=FrameSocratiCodeContextCompactionResult)
    validation: FrameValidationResult
    orchestrator_roles: dict[str, Any] = Field(default_factory=dict)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _extract_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    for tag, markers in _TAG_RULES:
        if any(marker in lowered for marker in markers):
            tags.append(tag)
    return tags


def _extract_entities(*, description: str, files: list[str], constraints: list[str], acceptance_criteria: list[str]) -> dict[str, list[str]]:
    components = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b", description)
    verbs = re.findall(r"\b(?:implement|build|generate|validate|secure|test|merge|inject|map|scan|strip|extract)\b", description.lower())
    file_exts = sorted({item.rsplit(".", 1)[-1].lower() for item in files if "." in item})
    domains = _extract_tags(" ".join([description, *files, *constraints, *acceptance_criteria]))
    return {
        "components": _unique(components[:16]),
        "verbs": _unique(verbs),
        "files": _unique(files),
        "constraints": _unique(constraints),
        "acceptance_criteria": _unique(acceptance_criteria),
        "file_extensions": file_exts,
        "domains": domains,
    }


def _layer_for_file(file_path: str) -> str:
    lowered = file_path.lower()
    if any(token in lowered for token in ("test", "spec", "__tests__")):
        return "qa_test_automation"
    if any(token in lowered for token in ("db/", "database", "migration", "schema", "repo", "repository", "cache")):
        return "database_storage"
    if any(token in lowered for token in ("security", "auth", "validator", "validation", "middleware", "guard")):
        return "validation_security"
    return "core_logic"


def _base_layer_mapping(files: list[str], tags: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for file_path in files:
        buckets[_layer_for_file(file_path)].append(file_path)
    if not buckets:
        buckets["core_logic"] = []
    if "database" in tags and "database_storage" not in buckets:
        buckets["database_storage"] = []
    if any(tag in tags for tag in ("validation", "security")) and "validation_security" not in buckets:
        buckets["validation_security"] = []
    if "testing" in tags and "qa_test_automation" not in buckets:
        buckets["qa_test_automation"] = []
    return {key: sorted(value) for key, value in buckets.items()}


def _collision_exclusion(layer_mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = defaultdict(list)
    for layer, files in layer_mapping.items():
        for file_path in files:
            owners[file_path].append(layer)
    collisions = {file_path: layers for file_path, layers in owners.items() if len(layers) > 1}
    return {key: sorted(value) for key, value in collisions.items()}


def _dependency_mapping(tags: list[str], acceptance_criteria: list[str], files: list[str]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {"core_logic": []}
    if "database" in tags or any(_layer_for_file(item) == "database_storage" for item in files):
        mapping["core_logic"].append("database_storage")
    if any(tag in tags for tag in ("validation", "security")):
        mapping["core_logic"].append("validation_security")
    if "testing" in tags or any("test" in item.lower() for item in acceptance_criteria):
        mapping["qa_test_automation"] = ["core_logic"]
    if mapping["core_logic"]:
        mapping["validation_security"] = ["core_logic"]
    return {key: _unique(value) for key, value in mapping.items()}


def _semantic_gaps(*, tags: list[str], files: list[str], acceptance_criteria: list[str], constraints: list[str]) -> tuple[list[str], list[str], list[str]]:
    gaps: list[str] = []
    edge_cases: list[str] = []
    critical_voids: list[str] = []

    lowered_acceptance = " ".join(acceptance_criteria).lower()
    lowered_constraints = " ".join(constraints).lower()
    has_test_files = any(_layer_for_file(item) == "qa_test_automation" for item in files)
    has_explicit_test_acceptance = any(
        marker in item.lower()
        for item in acceptance_criteria
        for marker in ("unit test", "integration", "mock", "assert", "qa", "fixture")
    )

    if not has_test_files and not has_explicit_test_acceptance:
        gaps.append("missing_explicit_test_lane")
        critical_voids.append("qa_coverage_not_explicit")
    if "validation" not in tags and "security" not in tags and "validate" not in lowered_constraints:
        gaps.append("missing_validation_or_security_lane")
    if "database" in tags and not any(_layer_for_file(item) == "database_storage" for item in files):
        gaps.append("database_intent_without_schema_targets")
        critical_voids.append("database_contract_not_mapped")
    if files and len(files) > len(set(files)):
        edge_cases.append("duplicate_file_targets_detected")
    if not files:
        edge_cases.append("text_only_request_without_file_boundaries")
    if "websocket" in tags:
        edge_cases.append("ws_transport_noise_requires_replay_safe_handling")

    return _unique(gaps), _unique(edge_cases), _unique(critical_voids)


def _socraticode_frame(task: Task) -> FrameSocratiCodeResult:
    hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
    annotation = hints.get("socraticode") if isinstance(hints.get("socraticode"), dict) else {}
    coverage = annotation.get("context_coverage") if isinstance(annotation.get("context_coverage"), dict) else {}
    cost = annotation.get("cost_downgrade") if isinstance(annotation.get("cost_downgrade"), dict) else {}
    parallel = annotation.get("parallelism") if isinstance(annotation.get("parallelism"), dict) else {}
    routing = annotation.get("routing_recommendations") if isinstance(annotation.get("routing_recommendations"), dict) else {}
    compact = annotation.get("compact_context") if isinstance(annotation.get("compact_context"), dict) else {}

    score_raw = coverage.get("score", coverage.get("coverage_ratio", coverage.get("ratio", 0.0)))
    try:
        score = round(float(score_raw), 4)
    except (TypeError, ValueError):
        score = 0.0

    recommended_raw = parallel.get("recommended_parallel_branches", routing.get("target_parallel_branches"))
    try:
        recommended_parallel = int(recommended_raw) if recommended_raw is not None else None
    except (TypeError, ValueError):
        recommended_parallel = None

    summary = str(coverage.get("summary") or compact.get("text") or "").strip()
    return FrameSocratiCodeResult(
        status=str(annotation.get("status") or ("applied" if annotation else "unavailable")).strip() or "unavailable",
        coverage_score=score,
        coverage_status=str(coverage.get("status") or "low").strip() or "low",
        prefer_low_cost_lanes=bool(cost.get("eligible") or routing.get("prefer_low_cost_lanes")),
        preferred_provider=str(cost.get("preferred_provider") or routing.get("prefer_provider") or "").strip() or None,
        recommended_parallel_branches=recommended_parallel,
        shared_index_ready=bool(routing.get("shared_index_ready") or coverage.get("indexed")),
        tools_used=_unique([str(item).strip() for item in (compact.get("tools_used") or []) if str(item).strip()]),
        covered_files=_unique([str(item).strip() for item in (coverage.get("covered_files") or []) if str(item).strip()]),
        missing_files=_unique([str(item).strip() for item in (coverage.get("missing_files") or []) if str(item).strip()]),
        compact_context_summary=summary[:900],
    )


def _apply_socraticode_gap_hints(task: Task, socraticode: FrameSocratiCodeResult, gaps: list[str], edge_cases: list[str], critical_voids: list[str]) -> tuple[list[str], list[str], list[str]]:
    task_type = str(getattr(task.type, "value", task.type) or "").strip().lower()
    if task_type not in {"code", "review", "test", "plan"}:
        return _unique(gaps), _unique(edge_cases), _unique(critical_voids)
    if socraticode.status != "applied":
        gaps.append("socraticode_context_not_available")
        edge_cases.append("token_economy_fallback_to_standard_prompting")
        return _unique(gaps), _unique(edge_cases), _unique(critical_voids)
    if socraticode.coverage_score < 0.72:
        gaps.append("socraticode_context_coverage_low")
    else:
        edge_cases.append("socraticode_compact_context_available")
    if socraticode.missing_files:
        edge_cases.append("socraticode_missing_requested_files")
    return _unique(gaps), _unique(edge_cases), _unique(critical_voids)


def _socraticode_context_compaction(task: Task, socraticode: FrameSocratiCodeResult) -> FrameSocratiCodeContextCompactionResult:
    if socraticode.status != "applied":
        return FrameSocratiCodeContextCompactionResult(
            status="disabled",
            compaction_mode="raw_prompt",
            prompt_context_source="task_description",
            raw_file_dump_allowed=True,
            token_reduction_expected="low",
            recommended_prompt_strategy="use standard file-bounded prompting until SocratiCode context is available",
            evidence=["socraticode_unavailable"],
        )

    evidence = []
    if socraticode.shared_index_ready:
        evidence.append("shared_index_ready")
    if socraticode.tools_used:
        evidence.extend(socraticode.tools_used[:4])
    if socraticode.missing_files:
        evidence.append("missing_files_present")

    if socraticode.coverage_score >= 0.88 and not socraticode.missing_files:
        return FrameSocratiCodeContextCompactionResult(
            status="active",
            compaction_mode="compact_context_first",
            prompt_context_source="socraticode_compact_context",
            raw_file_dump_allowed=False,
            token_reduction_expected="high",
            recommended_prompt_strategy="use SocratiCode compact context, impact summaries, and targeted file references before any raw file body expansion",
            evidence=_unique(evidence or ["strong_context_coverage"]),
        )

    if socraticode.coverage_score >= 0.72:
        return FrameSocratiCodeContextCompactionResult(
            status="active",
            compaction_mode="hybrid_context",
            prompt_context_source="socraticode_compact_context_plus_file_refs",
            raw_file_dump_allowed=False,
            token_reduction_expected="medium",
            recommended_prompt_strategy="prefer compact context and only expand explicitly missing files or unresolved symbols",
            evidence=_unique(evidence or ["good_context_coverage"]),
        )

    return FrameSocratiCodeContextCompactionResult(
        status="fallback",
        compaction_mode="raw_prompt",
        prompt_context_source="task_description_plus_file_refs",
        raw_file_dump_allowed=True,
        token_reduction_expected="low",
        recommended_prompt_strategy="do not shrink prompt context aggressively because SocratiCode coverage is still partial",
        evidence=_unique(evidence or ["low_context_coverage"]),
    )


def _best_practices(tags: list[str], socraticode: FrameSocratiCodeResult | None = None) -> list[str]:
    practices = [
        "Keep shard ownership file-scoped to avoid merge collisions.",
        "Persist shard status transitions so fan-out/fan-in remains observable.",
    ]
    if "database" in tags:
        practices.append("Isolate writes behind transactional repository boundaries.")
    if "validation" in tags or "security" in tags:
        practices.append("Apply strict input schemas before business logic and wrap unsafe edges with structured error handling.")
    if "testing" in tags:
        practices.append("Generate unit and integration fixtures from method signatures as lanes land.")
    if socraticode and socraticode.status == "applied" and socraticode.coverage_score >= 0.72:
        practices.append("Prefer SocratiCode compact context and impact summaries before attaching raw file bodies to preserve tokens.")
    elif socraticode and socraticode.status != "applied":
        practices.append("Keep a deterministic fallback path when SocratiCode context is unavailable so orchestration does not block on MCP readiness.")
    return practices


def _architectural_fixes(gaps: list[str], critical_voids: list[str], socraticode: FrameSocratiCodeResult | None = None) -> list[str]:
    fixes: list[str] = []
    if "missing_explicit_test_lane" in gaps:
        fixes.append("Provision a dedicated QA lane and bind it to the core logic signatures.")
    if "missing_validation_or_security_lane" in gaps:
        fixes.append("Add a validation/security lane to own schemas, sanitization, and exception policies.")
    if "database_intent_without_schema_targets" in gaps:
        fixes.append("Map database intent to migrations, repositories, or cache policy files before dispatch.")
    if "socraticode_context_not_available" in gaps:
        fixes.append("Fallback to standard file-bounded prompting until SocratiCode MCP/index is reachable.")
    if "socraticode_context_coverage_low" in gaps:
        fixes.append("Warm SocratiCode search/context artifacts before reducing provider cost or shrinking prompt context.")
    if critical_voids:
        fixes.append("Hold merge until critical voids are resolved or explicitly accepted by the orchestrator.")
    return fixes


def _worker_roles(
    *,
    description: str,
    layer_mapping: dict[str, list[str]],
    dependency_mapping: dict[str, list[str]],
    acceptance_criteria: list[str],
    tags: list[str],
) -> list[FrameWorkerRole]:
    roles: list[FrameWorkerRole] = []
    for role in ("core_logic", "database_storage", "validation_security", "qa_test_automation"):
        include = bool(layer_mapping.get(role))
        if role == "database_storage" and "database" in tags:
            include = True
        if role == "validation_security" and any(tag in tags for tag in ("validation", "security")):
            include = True
        if role == "qa_test_automation" and "testing" in tags:
            include = True
        if not include:
            continue
        file_targets = list(layer_mapping.get(role) or [])
        role_title = role.replace("_", " ")
        role_acceptance = list(acceptance_criteria)
        if role == "validation_security":
            role_acceptance = _unique(role_acceptance + ["input schemas generated", "exception handling added"])
        elif role == "qa_test_automation":
            role_acceptance = _unique(role_acceptance + ["unit or integration assertions generated"])
        focus = (
            f"Own the {role_title} lane for `{description}`. "
            f"Stay within {', '.join(file_targets) if file_targets else 'the discovered architectural boundary'} and publish structured output."
        )
        roles.append(
            FrameWorkerRole(
                role=role,
                target_capability=_ROLE_CAPABILITIES.get(role, ["code"])[0],
                file_targets=file_targets,
                objective=description,
                acceptance_criteria=role_acceptance,
                dependencies=list(dependency_mapping.get(role) or []),
                focus_prompt=focus,
            )
        )
    return roles


def _xml_package(
    *,
    task: Task,
    ingest: FrameIngestResult,
    context: FrameContextResult,
    semantic_gap: FrameSemanticGapResult,
    socraticode: FrameSocratiCodeResult,
    socraticode_context_compaction: FrameSocratiCodeContextCompactionResult,
    validation: FrameValidationResult,
) -> str:
    role_rows = "\n".join(
        f'    <worker role="{escape(role.role)}" capability="{escape(role.target_capability)}" files="{escape(",".join(role.file_targets))}">{escape(role.focus_prompt)}</worker>'
        for role in validation.worker_roles
    )
    gap_rows = "\n".join(f"    <gap>{escape(item)}</gap>" for item in semantic_gap.gap_scanner)
    critical_rows = "\n".join(f"    <critical_void>{escape(item)}</critical_void>" for item in semantic_gap.critical_voids_output)
    tag_rows = "\n".join(f"    <tag>{escape(tag)}</tag>" for tag in ingest.tags)
    socraticode_row = (
        f'  <socraticode status="{escape(socraticode.status)}" coverage_score="{socraticode.coverage_score}" '
        f'coverage_status="{escape(socraticode.coverage_status)}" prefer_low_cost="{str(socraticode.prefer_low_cost_lanes).lower()}" '
        f'preferred_provider="{escape(socraticode.preferred_provider or "")}" recommended_parallel_branches="{escape(str(socraticode.recommended_parallel_branches or ""))}" '
        f'shared_index_ready="{str(socraticode.shared_index_ready).lower()}">{escape(socraticode.compact_context_summary)}</socraticode>'
    )
    compaction_row = (
        f'  <socraticode_context_compaction status="{escape(socraticode_context_compaction.status)}" '
        f'compaction_mode="{escape(socraticode_context_compaction.compaction_mode)}" '
        f'prompt_context_source="{escape(socraticode_context_compaction.prompt_context_source)}" '
        f'raw_file_dump_allowed="{str(socraticode_context_compaction.raw_file_dump_allowed).lower()}" '
        f'token_reduction_expected="{escape(socraticode_context_compaction.token_reduction_expected)}">'
        f'{escape(socraticode_context_compaction.recommended_prompt_strategy)}</socraticode_context_compaction>'
    )
    return (
        f'<orchestrator_package task_id="{escape(task.task_id)}" status="validated">\n'
        f'  <ingest stripped_text="{escape(ingest.stripped_text[:240])}">\n'
        f'{tag_rows}\n'
        f'  </ingest>\n'
        f'  <context merged_items="{len(context.merged_array_output)}" collision_count="{len(context.collision_exclusion)}" />\n'
        f'{socraticode_row}\n'
        f'{compaction_row}\n'
        f'  <semantic_gap gap_count="{len(semantic_gap.gap_scanner)}" critical_void_count="{len(semantic_gap.critical_voids_output)}">\n'
        f'{gap_rows}\n'
        f'{critical_rows}\n'
        f'  </semantic_gap>\n'
        f'  <validation intent_score="{validation.intent_match_cross_validation.get("score", 0.0)}">\n'
        f'{role_rows}\n'
        f'  </validation>\n'
        f'</orchestrator_package>'
    )


def build_frame_orchestrator_package(task: Task, normalized_payload: dict[str, Any] | None = None) -> FrameOrchestratorPackage:
    normalized = normalized_payload if isinstance(normalized_payload, dict) else {}
    raw_text = str(normalized.get("description") or normalized.get("message") or task.input.description or "").strip()
    transport_noise_removed = sorted(key for key in normalized if key in _TRANSPORT_NOISE_KEYS)
    stripped_text = " ".join(raw_text.split())
    files = _unique(list(task.input.files))
    constraints = _unique(list(task.input.constraints))
    acceptance_criteria = _unique(list(task.input.acceptance_criteria))
    tags = _unique(_extract_tags(" ".join([str(task.type.value), stripped_text, *files, *constraints, *acceptance_criteria])))
    entities = _extract_entities(
        description=stripped_text,
        files=files,
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
    )
    ingest = FrameIngestResult(
        raw_text=raw_text,
        stripped_text=stripped_text,
        extracted_entities=entities,
        tags=tags,
        transport_noise_removed=transport_noise_removed,
    )

    layer_mapping = _base_layer_mapping(files, tags)
    context = FrameContextResult(
        base_layer_mapping=layer_mapping,
        context_sync={
            "files": files,
            "constraints": constraints,
            "acceptance_criteria": acceptance_criteria,
        },
        collision_exclusion=_collision_exclusion(layer_mapping),
        merged_array_output=_unique(files + constraints + acceptance_criteria + tags),
    )

    dependency_mapping = _dependency_mapping(tags, acceptance_criteria, files)
    socraticode = _socraticode_frame(task)
    socraticode_context_compaction = _socraticode_context_compaction(task, socraticode)
    gaps, edge_cases, critical_voids = _semantic_gaps(
        tags=tags,
        files=files,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
    )
    gaps, edge_cases, critical_voids = _apply_socraticode_gap_hints(task, socraticode, gaps, edge_cases, critical_voids)
    semantic_gap = FrameSemanticGapResult(
        text_only_audit={
            "description_length": len(stripped_text),
            "file_count": len(files),
            "constraint_count": len(constraints),
            "acceptance_count": len(acceptance_criteria),
        },
        dependency_mapping=dependency_mapping,
        gap_scanner=gaps,
        edge_case_check=edge_cases,
        critical_voids_output=critical_voids,
    )

    worker_roles = _worker_roles(
        description=task.input.description,
        layer_mapping=layer_mapping,
        dependency_mapping=dependency_mapping,
        acceptance_criteria=acceptance_criteria,
        tags=tags,
    )
    validation = FrameValidationResult(
        best_practices_generation=_best_practices(tags, socraticode),
        intent_match_cross_validation={
            "score": 1.0 if task.input.description.strip() else 0.0,
            "matched_type": task.type.value,
            "parallel_candidate": bool(task.routing_hints.get("parallelize_code")) if isinstance(task.routing_hints, dict) else False,
        },
        architectural_fixes=_architectural_fixes(gaps, critical_voids, socraticode),
        worker_roles=worker_roles,
    )
    validation.xml_orchestrator_package_output = _xml_package(
        task=task,
        ingest=ingest,
        context=context,
        semantic_gap=semantic_gap,
        socraticode=socraticode,
        socraticode_context_compaction=socraticode_context_compaction,
        validation=validation,
    )
    return FrameOrchestratorPackage(
        ingest=ingest,
        context=context,
        semantic_gap=semantic_gap,
        socraticode=socraticode,
        socraticode_context_compaction=socraticode_context_compaction,
        validation=validation,
        orchestrator_roles={
            "lead_architect_agent": "orchestrator",
            "parallel_roles": [role.role for role in worker_roles],
            "merge_owner": "result_merger",
            "context_compaction_owner": "socraticode" if socraticode.status == "applied" else "orchestrator",
        },
    )
