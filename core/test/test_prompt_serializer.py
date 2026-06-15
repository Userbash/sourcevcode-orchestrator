from __future__ import annotations

from core.core.prompting.prompt_serializer import serialize_prompt


def test_prompt_serializer_sorts_tools_and_keeps_prefix_hash_stable():
    prompt_a = serialize_prompt(
        system_instructions=["global safety", "coding rules"],
        tools=[
            {"name": "web_search", "description": "search"},
            {"name": "apply_patch", "description": "edit"},
        ],
        static_context=["repo policy"],
        dynamic_context={"time": "16:48:17", "user_id": "123"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v1",
    )
    prompt_b = serialize_prompt(
        system_instructions=["global safety", "coding rules"],
        tools=[
            {"name": "apply_patch", "description": "edit"},
            {"name": "web_search", "description": "search"},
        ],
        static_context=["repo policy"],
        dynamic_context={"user_id": "123", "time": "16:48:17"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v1",
    )

    assert prompt_a["serialized_prompt"] == prompt_b["serialized_prompt"]
    assert prompt_a["prefix_hash"] == prompt_b["prefix_hash"]
    assert prompt_a["full_prompt_hash"] == prompt_b["full_prompt_hash"]
    assert prompt_a["tool_names"] == ["apply_patch", "web_search"]


def test_prompt_serializer_dynamic_context_changes_full_hash_not_prefix_hash():
    baseline = serialize_prompt(
        system_instructions=["global safety"],
        tools=[{"name": "apply_patch"}],
        static_context=["repo policy"],
        dynamic_context={"time": "16:48:17"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v1",
    )
    changed = serialize_prompt(
        system_instructions=["global safety"],
        tools=[{"name": "apply_patch"}],
        static_context=["repo policy"],
        dynamic_context={"time": "16:48:18"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v1",
    )

    assert baseline["prefix_hash"] == changed["prefix_hash"]
    assert baseline["full_prompt_hash"] != changed["full_prompt_hash"]


def test_prompt_serializer_prompt_version_changes_prefix_hash():
    baseline = serialize_prompt(
        system_instructions=["global safety"],
        tools=[{"name": "apply_patch"}],
        static_context=["repo policy"],
        dynamic_context={"time": "16:48:17"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v1",
    )
    changed = serialize_prompt(
        system_instructions=["global safety"],
        tools=[{"name": "apply_patch"}],
        static_context=["repo policy"],
        dynamic_context={"time": "16:48:17"},
        messages=[{"role": "user", "content": "hello"}],
        prompt_version="v2",
    )

    assert baseline["prefix_hash"] != changed["prefix_hash"]
