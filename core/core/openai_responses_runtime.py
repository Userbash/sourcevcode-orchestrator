from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class ResponsesClient(Protocol):
    class _Responses(Protocol):
        def create(self, **kwargs: Any) -> Any: ...

    responses: _Responses


class OpenAIResponsesRuntimeError(RuntimeError):
    pass


class MissingToolOutputError(OpenAIResponsesRuntimeError):
    pass


class UnsupportedToolCallError(OpenAIResponsesRuntimeError):
    pass


class ResponsesRuntimeProtocolError(OpenAIResponsesRuntimeError):
    pass


@dataclass(slots=True)
class ToolCall:
    name: str
    call_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResponsesRunResult:
    response: Any
    output_text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_outputs: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0


ToolHandler = Callable[[ToolCall], Any]


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


def _response_output_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        items = response.get("output")
        return list(items) if isinstance(items, list) else []
    output = getattr(response, "output", None)
    if isinstance(output, list):
        return [_as_dict(item) for item in output]
    return []


def _response_id(response: Any) -> str | None:
    if isinstance(response, dict):
        value = response.get("id")
    else:
        value = getattr(response, "id", None)
    text = str(value or "").strip()
    return text or None


def _response_output_text(response: Any) -> str:
    if isinstance(response, dict):
        text = response.get("output_text")
        if isinstance(text, str):
            return text
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return text

    chunks: list[str] = []
    for item in _response_output_items(response):
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"message", "output_text"}:
            content = item.get("content")
            if isinstance(content, list):
                for row in content:
                    if isinstance(row, dict) and isinstance(row.get("text"), str):
                        chunks.append(str(row.get("text") or ""))
            elif isinstance(item.get("text"), str):
                chunks.append(str(item.get("text") or ""))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _parse_tool_call(item: dict[str, Any]) -> ToolCall | None:
    item_type = str(item.get("type") or "").strip().lower()
    if item_type not in {"function_call", "tool_call"}:
        return None
    name = str(item.get("name") or item.get("tool_name") or "").strip()
    call_id = str(item.get("call_id") or item.get("id") or "").strip()
    if not name or not call_id:
        raise ResponsesRuntimeProtocolError("tool call missing name or call_id")
    raw_arguments = item.get("arguments")
    if isinstance(raw_arguments, str):
        raw_arguments = raw_arguments.strip()
        if raw_arguments:
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ResponsesRuntimeProtocolError(f"tool call {name} has invalid JSON arguments") from exc
        else:
            arguments = {}
    elif isinstance(raw_arguments, Mapping):
        arguments = dict(raw_arguments)
    elif raw_arguments is None:
        arguments = {}
    else:
        raise ResponsesRuntimeProtocolError(f"tool call {name} has unsupported arguments payload")
    return ToolCall(name=name, call_id=call_id, arguments=arguments, raw=item)


class OpenAIResponsesRuntime:
    def __init__(self, client: ResponsesClient) -> None:
        self.client = client

    def run(
        self,
        *,
        model: str,
        input: Any,
        tool_handlers: Mapping[str, ToolHandler] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_iterations: int = 8,
        **kwargs: Any,
    ) -> ResponsesRunResult:
        handlers = dict(tool_handlers or {})
        request: dict[str, Any] = {"model": model, "input": input, **kwargs}
        if tools:
            request["tools"] = tools

        all_calls: list[ToolCall] = []
        all_outputs: list[dict[str, Any]] = []
        response = self.client.responses.create(**request)
        iterations = 1

        while True:
            calls = [call for item in _response_output_items(response) if (call := _parse_tool_call(item)) is not None]
            if not calls:
                return ResponsesRunResult(
                    response=response,
                    output_text=_response_output_text(response),
                    tool_calls=all_calls,
                    tool_outputs=all_outputs,
                    iterations=iterations,
                )
            if iterations >= max_iterations:
                raise OpenAIResponsesRuntimeError(f"responses loop exceeded max_iterations={max_iterations}")

            round_outputs = self._execute_tool_calls(calls, handlers)
            if len(round_outputs) != len(calls):
                missing = [call.call_id for call in calls if call.call_id not in {row.get('call_id') for row in round_outputs}]
                raise MissingToolOutputError(
                    f"missing tool output for call ids: {', '.join(missing) or 'unknown'}"
                )

            all_calls.extend(calls)
            all_outputs.extend(round_outputs)
            request = {"model": model, "input": round_outputs, **kwargs}
            previous_id = _response_id(response)
            if previous_id:
                request["previous_response_id"] = previous_id
            if tools:
                request["tools"] = tools
            response = self.client.responses.create(**request)
            iterations += 1

    def _execute_tool_calls(
        self,
        calls: list[ToolCall],
        handlers: Mapping[str, ToolHandler],
    ) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for call in calls:
            handler = handlers.get(call.name)
            if handler is None:
                raise UnsupportedToolCallError(f"unsupported tool call: {call.name}")
            payload = handler(call)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": self._serialize_tool_output(payload),
                }
            )
        return outputs

    @staticmethod
    def _serialize_tool_output(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        try:
            return json.dumps(payload, ensure_ascii=True, default=str)
        except TypeError:
            return json.dumps(str(payload), ensure_ascii=True)
