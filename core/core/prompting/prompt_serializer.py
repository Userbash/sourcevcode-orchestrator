from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "").strip()
    return str(getattr(tool, "name", "")).strip()


def serialize_prompt(
    *,
    system_instructions: list[str] | None = None,
    tools: list[Any] | None = None,
    static_context: list[str] | None = None,
    dynamic_context: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    prompt_version: str = "v1",
) -> dict[str, Any]:
    tool_entries = [_canonical(tool) for tool in (tools or [])]
    tool_entries = sorted(tool_entries, key=_tool_name)
    prefix_payload = {
        "prompt_version": str(prompt_version),
        "system_instructions": [str(item) for item in (system_instructions or [])],
        "tools": tool_entries,
        "static_context": [str(item) for item in (static_context or [])],
    }
    full_payload = {
        **prefix_payload,
        "dynamic_context": _canonical(dynamic_context or {}),
        "messages": _canonical(messages or []),
    }
    prefix_json = json.dumps(prefix_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    full_json = json.dumps(full_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return {
        "serialized_prompt": full_json,
        "prefix_hash": hashlib.sha256(prefix_json.encode("utf-8")).hexdigest(),
        "full_prompt_hash": hashlib.sha256(full_json.encode("utf-8")).hexdigest(),
        "tool_names": [_tool_name(tool) for tool in tool_entries],
        "prompt_version": str(prompt_version),
    }
