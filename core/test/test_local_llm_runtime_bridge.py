from __future__ import annotations

from urllib.error import URLError

from core.core.local_llm_bridge import LocalLLMBridge


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
