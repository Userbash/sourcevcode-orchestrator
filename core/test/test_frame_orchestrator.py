from core.core.frame_orchestrator import build_frame_orchestrator_package
from core.core.models import Task, TaskContext, TaskInput, TaskType


def test_build_frame_orchestrator_package_generates_xml_and_worker_roles():
    task = Task(
        TaskType.CODE,
        TaskInput(
            "Implement websocket ingest, validation middleware, repository migration, and tests",
            files=[
                "core/ws/router.py",
                "core/security/validator.py",
                "core/db/migrations/001_init.sql",
                "core/test/test_ws_router.py",
            ],
            constraints=["sanitize websocket noise", "preserve transport safety"],
            acceptance_criteria=["tests pass", "validation schemas generated"],
        ),
        TaskContext("demo", ".", "main"),
    )
    task.routing_hints = {
        "parallelize_code": True,
        "source": "websocket",
        "channel": "ws",
        "socraticode": {
            "status": "applied",
            "context_coverage": {
                "score": 0.91,
                "status": "strong",
                "covered_files": ["core/ws/router.py", "core/security/validator.py"],
                "missing_files": ["core/db/migrations/001_init.sql"],
                "summary": "Indexed code graph and context artifacts already cover the websocket/auth path.",
            },
            "cost_downgrade": {
                "eligible": True,
                "preferred_provider": "local",
            },
            "parallelism": {
                "recommended_parallel_branches": 2,
            },
            "routing_recommendations": {
                "prefer_low_cost_lanes": True,
                "shared_index_ready": True,
            },
            "compact_context": {
                "text": "Task: websocket ingest\nSearch: router and validator already indexed\nImpact: migration touches auth path",
                "tools_used": ["codebase_search", "codebase_context_search", "codebase_impact"],
            },
        },
    }

    package = build_frame_orchestrator_package(
        task,
        {"description": task.input.description, "source": "websocket", "channel": "ws", "type": "command"},
    )

    roles = [item.role for item in package.validation.worker_roles]
    assert "core_logic" in roles
    assert "database_storage" in roles
    assert "validation_security" in roles
    assert "qa_test_automation" in roles
    assert package.ingest.transport_noise_removed == ["channel", "source", "type"]
    assert "missing_explicit_test_lane" not in package.semantic_gap.gap_scanner
    assert package.validation.xml_orchestrator_package_output.startswith("<orchestrator_package")
    assert "lead_architect_agent" in package.orchestrator_roles

    assert package.socraticode.status == "applied"
    assert package.socraticode.coverage_score == 0.91
    assert package.socraticode.preferred_provider == "local"
    assert package.socraticode_context_compaction.status == "active"
    assert package.socraticode_context_compaction.compaction_mode == "hybrid_context"
    assert package.socraticode_context_compaction.raw_file_dump_allowed is False
    assert "socraticode_compact_context_available" in package.semantic_gap.edge_case_check
    assert "Prefer SocratiCode compact context" in " ".join(package.validation.best_practices_generation)
    assert '<socraticode status="applied"' in package.validation.xml_orchestrator_package_output
    assert '<socraticode_context_compaction status="active"' in package.validation.xml_orchestrator_package_output
