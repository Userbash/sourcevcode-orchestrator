from __future__ import annotations

import re
from typing import Any


EMPTY_PROVIDER_REQUEST_ERROR = "Refusing to send empty provider request"
EMPTY_ASSISTANT_RESPONSE_ERROR = "Provider returned no assistant content or tool calls"

_MEANINGFUL_TEXT_RE = re.compile(r"[A-Za-zА-Яа-я0-9]")
_LABEL_LINE_RE = re.compile(r"^[A-ZА-Я0-9 _/-]{2,40}:\s*(.*)$")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dumped if isinstance(dumped, dict) else {}
    data = getattr(value, "__dict__", None)
    return data if isinstance(data, dict) else {}


def _stringify_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
                continue
            row = _as_dict(item)
            text = str(row.get("text") or row.get("output_text") or row.get("content") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    row = _as_dict(value)
    if row:
        return str(row.get("text") or row.get("output_text") or row.get("content") or "").strip()
    return ""


def has_meaningful_request_payload(payload: Any) -> bool:
    text = _stringify_content(payload)
    if not text:
        return False
    normalized_lines: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = _LABEL_LINE_RE.match(candidate)
        normalized_lines.append((match.group(1) if match else candidate).strip())
    normalized = "\n".join(part for part in normalized_lines if part).strip()
    return bool(normalized and _MEANINGFUL_TEXT_RE.search(normalized))


def extract_chat_completion_text(response: Any) -> str:
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        return ""
    choice = _as_dict(choices[0])
    message = _as_dict(choice.get("message"))
    return _stringify_content(message.get("content"))


def chat_completion_has_tool_calls(response: Any) -> bool:
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        return False
    choice = _as_dict(choices[0])
    message = _as_dict(choice.get("message"))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return True
    function_call = message.get("function_call")
    return isinstance(function_call, dict) and bool(function_call)


def extract_provider_response_text(response: Any) -> str:
    return extract_chat_completion_text(response) or extract_responses_output_text(response)


def responses_has_assistant_content_or_tool_calls(response: Any) -> bool:
    if extract_responses_output_text(response):
        return True
    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    if not isinstance(output, list):
        return False
    for item in output:
        row = _as_dict(item)
        item_type = str(row.get("type") or "").strip().lower()
        if item_type in {"function_call", "tool_call"}:
            return True
    return False


def provider_response_has_assistant_content_or_tool_calls(response: Any) -> bool:
    return bool(
        extract_provider_response_text(response)
        or chat_completion_has_tool_calls(response)
        or responses_has_assistant_content_or_tool_calls(response)
    )


def extract_responses_output_text(response: Any) -> str:
    if isinstance(response, dict):
        text = response.get("output_text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    else:
        text = getattr(response, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    if not isinstance(output, list):
        return ""

    parts: list[str] = []
    for item in output:
        row = _as_dict(item)
        item_type = str(row.get("type") or "").strip().lower()
        if item_type not in {"message", "output_text"}:
            continue
        content = row.get("content")
        if isinstance(content, list):
            for value in content:
                text = _stringify_content(value)
                if text:
                    parts.append(text)
        else:
            text = _stringify_content(row.get("text"))
            if text:
                parts.append(text)
    return "\n".join(parts).strip()
