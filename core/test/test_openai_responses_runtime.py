from __future__ import annotations

import json

from core.core.openai_responses_runtime import (
    MissingToolOutputError,
    OpenAIResponsesRuntime,
    UnsupportedToolCallError,
)


class _FakeResponsesClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.responses = self

    def create(self, **kwargs):
        self.requests.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("no fake responses left")
        return self._responses.pop(0)


def test_runtime_loops_tool_outputs_back_with_previous_response_id():
    client = _FakeResponsesClient(
        [
            {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_tool_1",
                        "name": "tool_search",
                        "arguments": json.dumps({"query": "auth flow"}),
                    }
                ],
            },
            {
                "id": "resp_2",
                "output_text": "final answer",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "final answer"}],
                    }
                ],
            },
        ]
    )
    runtime = OpenAIResponsesRuntime(client)

    result = runtime.run(
        model="gpt-5.5",
        input="find auth flow",
        tool_handlers={"tool_search": lambda call: {"hits": [call.arguments["query"]]}},
    )

    assert result.output_text == "final answer"
    assert result.iterations == 2
    assert result.tool_calls[0].name == "tool_search"
    assert result.tool_outputs == [
        {
            "type": "function_call_output",
            "call_id": "call_tool_1",
            "output": "{\"hits\": [\"auth flow\"]}",
        }
    ]
    assert client.requests[1]["previous_response_id"] == "resp_1"
    assert client.requests[1]["input"] == result.tool_outputs


def test_runtime_raises_for_unsupported_tool_call():
    client = _FakeResponsesClient(
        [
            {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_missing",
                        "name": "tool_search",
                        "arguments": "{}",
                    }
                ],
            }
        ]
    )
    runtime = OpenAIResponsesRuntime(client)

    try:
        runtime.run(model="gpt-5.5", input="x", tool_handlers={})
    except UnsupportedToolCallError as exc:
        assert "tool_search" in str(exc)
    else:
        raise AssertionError("expected UnsupportedToolCallError")


def test_runtime_raises_precise_error_for_missing_tool_output():
    client = _FakeResponsesClient(
        [
            {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_tool_1",
                        "name": "tool_search",
                        "arguments": "{}",
                    }
                ],
            }
        ]
    )
    runtime = OpenAIResponsesRuntime(client)

    def _bad_handler(_call):
        return None

    original = runtime._execute_tool_calls

    def _broken(calls, handlers):
        rows = original(calls, handlers)
        return []

    runtime._execute_tool_calls = _broken  # type: ignore[method-assign]
    try:
        runtime.run(model="gpt-5.5", input="x", tool_handlers={"tool_search": _bad_handler})
    except MissingToolOutputError as exc:
        assert "call_tool_1" in str(exc)
    else:
        raise AssertionError("expected MissingToolOutputError")


def test_runtime_parses_dict_arguments_and_message_text():
    client = _FakeResponsesClient(
        [
            {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_tool_1",
                        "name": "tool_search",
                        "arguments": {"query": "router"},
                    }
                ],
            },
            {
                "id": "resp_2",
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "router summary"}],
                    }
                ],
            },
        ]
    )
    runtime = OpenAIResponsesRuntime(client)

    result = runtime.run(
        model="gpt-5.5",
        input="find router",
        tool_handlers={"tool_search": lambda call: {"ok": call.arguments["query"]}},
    )

    assert result.output_text == "router summary"
