from __future__ import annotations

from urllib.error import URLError

import pytest

from core.core.local_llm_bridge import LocalLLMBridge


class _Response:
    def __init__(self, status_code: int = 200, payload: dict[str, object] | None = None, content: bytes = b"{}"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self) -> dict[str, object]:
        return self._payload


def test_local_llm_bridge_query_fails_over_to_secondary_endpoint(monkeypatch):
    calls: list[str] = []

    def fake_post(url: str, json: dict[str, object], timeout: float):
        calls.append(url)
        if url.startswith("http://host.containers.internal"):
            raise RuntimeError("primary endpoint down")
        return _Response(payload={"response": "fallback ok"})

    monkeypatch.setattr("core.core.local_llm_bridge.requests.post", fake_post)

    bridge = LocalLLMBridge(ollama_port=11434)
    result = bridge.query("ping", "qwen2.5:32b-instruct-q4_k_m")

    assert result == "fallback ok"
    assert calls == [
        "http://host.containers.internal:11434/api/generate",
        "http://127.0.0.1:11434/api/generate",
    ]



def test_local_llm_bridge_query_skips_empty_or_invalid_payloads_until_a_valid_response(monkeypatch):
    calls: list[str] = []

    def fake_post(url: str, json: dict[str, object], timeout: float):
        calls.append(url)
        if len(calls) == 1:
            return _Response(payload={"response": "   "})
        return _Response(payload={"response": "usable response"})

    monkeypatch.setattr("core.core.local_llm_bridge.requests.post", fake_post)

    bridge = LocalLLMBridge(ollama_port=11434)
    result = bridge.query("ping", "qwen2.5:32b-instruct-q4_k_m")

    assert result == "usable response"
    assert calls == [
        "http://host.containers.internal:11434/api/generate",
        "http://127.0.0.1:11434/api/generate",
    ]



def test_local_llm_bridge_query_raises_last_exception_when_all_endpoints_fail(monkeypatch):
    def fake_post(url: str, json: dict[str, object], timeout: float):
        if url.startswith("http://host.containers.internal"):
            raise RuntimeError("primary failed")
        raise URLError("secondary failed")

    monkeypatch.setattr("core.core.local_llm_bridge.requests.post", fake_post)

    bridge = LocalLLMBridge(ollama_port=11434)
    with pytest.raises(URLError):
        bridge.query("ping", "qwen2.5:32b-instruct-q4_k_m")



def test_local_llm_bridge_host_probe_falls_back_to_localhost(monkeypatch):
    calls: list[str] = []

    class _UrlResponse:
        def __init__(self, url: str):
            self.status = 200
            self._url = url

        def read(self) -> bytes:
            return b'{"models": []}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(url: str, timeout: float):
        calls.append(url)
        if url.startswith("http://host.containers.internal"):
            raise URLError("host alias unavailable")
        return _UrlResponse(url)

    monkeypatch.setattr("core.core.local_llm_bridge.urlopen", fake_urlopen)

    bridge = LocalLLMBridge(ollama_port=11434)
    probe = bridge._host_probe()

    assert probe["ok"] is True
    assert probe["url"] == "http://127.0.0.1:11434/api/tags"
    assert calls == [
        "http://host.containers.internal:11434/api/tags",
        "http://127.0.0.1:11434/api/tags",
    ]
