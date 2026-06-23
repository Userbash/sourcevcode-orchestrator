from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from typing import Any

from core.scripts import ping_all_models
from core.scripts.ping_all_models import classify_mistral_skip_reason, classify_openai_skip_reason, is_mistral_chat_model


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, get_map: dict[str, _FakeResponse], post_handler) -> None:
        self._get_map = get_map
        self._post_handler = post_handler

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        return self._get_map[url]

    async def post(self, url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None) -> _FakeResponse:
        return self._post_handler(url, json or {})


def test_is_mistral_chat_model_skips_known_non_chat_variants():
    assert is_mistral_chat_model('mistral-small-latest') is True
    assert is_mistral_chat_model('mistral-embed-2312') is False
    assert is_mistral_chat_model('mistral-moderation-latest') is False
    assert is_mistral_chat_model('mistral-ocr-latest') is False
    assert is_mistral_chat_model('voxtral-mini-tts-latest') is False
    assert is_mistral_chat_model('voxtral-mini-realtime-2602') is False


def test_classify_mistral_skip_reason_is_specific():
    assert classify_mistral_skip_reason('mistral-embed-2312') == 'embedding_model'
    assert classify_mistral_skip_reason('mistral-moderation-latest') == 'moderation_model'
    assert classify_mistral_skip_reason('mistral-ocr-latest') == 'ocr_model'
    assert classify_mistral_skip_reason('voxtral-mini-tts-latest') == 'tts_model'
    assert classify_mistral_skip_reason('voxtral-mini-transcribe-realtime-2602') == 'transcription_model'
    assert classify_openai_skip_reason('gpt-4o-transcribe') == 'transcription_model'
    assert classify_openai_skip_reason('gpt-image-2') == 'image_or_media_model'


def test_ping_openai_models_skips_non_text_and_pings_chat_models(monkeypatch):
    models_endpoint = "https://example.test/v1/models"
    chat_endpoint = "https://example.test/v1/chat/completions"
    monkeypatch.setattr(
        ping_all_models,
        "resolve_openai_provider_config",
        lambda: SimpleNamespace(api_key="token", models_endpoint=models_endpoint, chat_completions_endpoint=chat_endpoint),
    )
    seen_models: list[str] = []

    def post_handler(url: str, payload: dict[str, Any]) -> _FakeResponse:
        assert url == chat_endpoint
        model = payload["model"]
        seen_models.append(model)
        if model == "gpt-fail":
            return _FakeResponse(429, text="rate limit")
        return _FakeResponse(200, payload={"choices": [{"message": {"content": f"pong from {model}"}}]})

    monkeypatch.setattr(
        ping_all_models.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(get_map={models_endpoint: _FakeResponse(200, payload={"data": [{"id": "gpt-ok"}, {"id": "gpt-4o-transcribe"}, {"id": "gpt-image-2"}, {"id": "gpt-fail"}]})}, post_handler=post_handler),
    )

    report = asyncio.run(ping_all_models.ping_openai_models("reply with pong only"))

    assert seen_models == ["gpt-ok", "gpt-fail"]
    assert report["ok"] == 1
    assert report["failed"] == 1
    assert report["skipped_non_text"] == 2


def test_ping_mistral_models_skips_non_chat_and_pings_chat_models(monkeypatch):
    monkeypatch.setattr(ping_all_models, "MistralManager", lambda: SimpleNamespace(api_key="token", base_url="https://mistral.test/v1"))
    called_models: list[str] = []

    def post_handler(url: str, payload: dict[str, Any]) -> _FakeResponse:
        assert url == "https://mistral.test/v1/chat/completions"
        called_models.append(payload["model"])
        return _FakeResponse(200, payload={"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(
        ping_all_models.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(get_map={"https://mistral.test/v1/models": _FakeResponse(200, payload={"data": [{"id": "mistral-small-latest"}, {"id": "mistral-embed-2312"}]})}, post_handler=post_handler),
    )

    report = asyncio.run(ping_all_models.ping_mistral_models("reply with pong only"))

    assert called_models == ["mistral-small-latest"]
    assert report["ok"] == 1
    assert report["failed"] == 0
    assert report["skipped_non_chat"] == 1


def test_ping_local_llm_models_pings_all_tagged_models(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT", "http://local-llm.test")
    called_models: list[str] = []

    def post_handler(url: str, payload: dict[str, Any]) -> _FakeResponse:
        assert url == "http://local-llm.test/api/generate"
        model = payload["model"]
        called_models.append(model)
        if model == "broken-model":
            return _FakeResponse(503, text="unavailable")
        return _FakeResponse(200, payload={"response": f"pong from {model}"})

    monkeypatch.setattr(
        ping_all_models.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(get_map={"http://local-llm.test/api/tags": _FakeResponse(200, payload={"models": [{"name": "qwen2.5:7b"}, {"name": "broken-model"}]})}, post_handler=post_handler),
    )

    report = asyncio.run(ping_all_models.ping_local_llm_models("reply with pong only"))
    assert called_models == ["qwen2.5:7b", "broken-model"]
    assert report["ok"] == 1
    assert report["failed"] == 1


def test_ping_mimo_models_uses_direct_http_native_models(monkeypatch, tmp_path):
    monkeypatch.setattr(ping_all_models, "configured_native_mimo_models", lambda: ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5"])

    def fake_invoke(model, prompt, timeout_sec=20.0, max_completion_tokens=128, temperature=0.0):
        if model == "xiaomi/mimo-v2.5-pro":
            return ({"choices": [{"message": {"content": "pong mimo pro"}}]}, None, 200)
        return ({"choices": [{"message": {"content": "pong mimo"}}]}, None, 200)

    monkeypatch.setattr(ping_all_models, "invoke_mimo_native", fake_invoke)

    report = asyncio.run(ping_all_models.ping_mimo_models("reply with pong only", tmp_path))

    assert report["ok"] == 2
    assert report["failed"] == 0
    assert [row["model"] for row in report["models"]] == ["xiaomi/mimo-v2.5", "xiaomi/mimo-v2.5-pro"]
    partial = json.loads((tmp_path / "mimo_model_ping_report.partial.json").read_text(encoding="utf-8"))
    assert partial["completed"] == 2
    assert partial["ok"] == 2


def test_ping_antigravity_marks_all_status_models_from_single_probe(monkeypatch):
    monkeypatch.setattr(ping_all_models, "AntigravityManager", lambda: SimpleNamespace(status=lambda: {"ready": True, "models": ["gemini-2.5-pro", "gemini-2.5-flash"]}))

    def fake_invoke(model, prompt, timeout_sec=20.0, max_completion_tokens=128, temperature=0.0):
        return ({"choices": [{"message": {"content": f"pong:{model}"}}]}, None, 200)

    monkeypatch.setattr(ping_all_models, "invoke_antigravity_native", fake_invoke)

    report = asyncio.run(ping_all_models.ping_antigravity("reply with pong only"))
    assert report["ok"] == 2
    assert report["failed"] == 0
    assert [row["model"] for row in report["models"]] == ["gemini-2.5-pro", "gemini-2.5-flash"]


def test_run_all_models_only_provider_mistral_marks_others_not_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(ping_all_models, "ping_mistral_models", lambda prompt, skip_non_chat=True: asyncio.sleep(0, result={"provider": "mistral", "models": [{"model": "mistral-large-latest", "ok": True}], "ok": 1, "failed": 0, "skipped": False, "skipped_non_chat": 0}))
    report, mimo_report, artifacts = asyncio.run(ping_all_models.run_all_models("reply with pong only", tmp_path, only_provider="mistral"))
    assert report["mistral"]["ok"] == 1
    assert mimo_report["skipped"] is True
    assert artifacts["failed"]["mistral"]["failed_count"] == 0


def test_main_async_writes_reports_for_all_provider_sweeps(monkeypatch, tmp_path):
    report = {"openai": {"provider": "openai", "ok": 1, "failed": 0, "models": [{"model": "gpt-ok", "ok": True}]}, "mistral": {"provider": "mistral", "ok": 1, "failed": 0, "skipped_non_chat": 0, "models": [{"model": "mistral-small", "ok": True}]}, "local_llm": {"provider": "local_llm", "ok": 1, "failed": 0, "models": [{"model": "qwen", "ok": True}]}, "antigravity": {"provider": "antigravity", "ok": 1, "failed": 0, "models": [{"model": "gemini", "ok": True}]}, "ai_kernel": {"provider": "ai_kernel", "ok": 0, "failed": 0, "models": []}}
    mimo_report = {"provider": "mimo", "ok": 1, "failed": 1, "models": [{"model": "xiaomi/mimo-v2.5-pro", "ok": True}, {"model": "xiaomi/mimo-v2.5", "ok": False}]}
    artifacts = {"failed": {"openai": {"provider": "openai", "failed_count": 0, "total": 1, "models": []}, "mistral": {"provider": "mistral", "failed_count": 0, "total": 1, "models": []}, "local_llm": {"provider": "local_llm", "failed_count": 0, "total": 1, "models": []}, "antigravity": {"provider": "antigravity", "failed_count": 0, "total": 1, "models": []}, "ai_kernel": {"provider": "ai_kernel", "failed_count": 0, "total": 0, "models": []}, "mimo": {"provider": "mimo", "failed_count": 1, "total": 2, "models": [{"model": "xiaomi/mimo-v2.5", "ok": False}]}}, "mimo_usable": {"provider": "mimo", "usable_count": 1, "total": 2, "models": [{"model": "xiaomi/mimo-v2.5-pro", "ok": True}]}}
    monkeypatch.setattr(ping_all_models, "run_all_models", lambda prompt, output_dir, skip_mistral_non_chat=True, only_provider=None: asyncio.sleep(0, result=(report, mimo_report, artifacts)))
    args = argparse.Namespace(prompt="reply with pong only", output_dir=str(tmp_path), include_mistral_non_chat=False, only_provider=None)
    exit_code = asyncio.run(ping_all_models.main_async(args))
    assert exit_code == 0
