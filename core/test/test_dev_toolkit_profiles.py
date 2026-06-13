from __future__ import annotations

from core.core.dev_toolkit_module import DevToolkitModule


def test_dev_toolkit_normalizes_execute_alias_to_apply():
    assert DevToolkitModule._normalize_mode("execute") == "apply"


def test_dev_toolkit_build_context_uses_apply_mode_permissions():
    module = DevToolkitModule()
    session = module.load_or_create_session("s-1", mode="apply", repo_context=True)

    context = module.build_dev_context(
        session=session,
        message="implement the fix",
        repo_context=True,
        mode="apply",
    )

    assert "devtoolkit:read" in context.permissions
    assert "devtoolkit:plan" in context.permissions
    assert "devtoolkit:repo" in context.permissions
    assert "devtoolkit:write" in context.permissions
    assert "devtoolkit:execute" in context.permissions
    assert context.request.allow_code_changes is True
    assert context.request.allow_execution is True
