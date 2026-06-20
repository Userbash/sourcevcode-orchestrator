from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from typing import Any

from core.scripts import ping_all_models
from core.scripts.ping_all_models import classify_mistral_skip_reason, is_mistral_chat_model, parse_mimo_models


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


class _FakeProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")


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


def test_parse_mimo_models_reads_verbose_inventory_blocks():
    raw = """openai/gpt-5.4
{
  "id": "gpt-5.4",
  "providerID": "openai",
  "status": "ONLINE",
  "limit": {
    "context": 128000
  }
}
mimo/mimo-auto
{
  "id": "mimo-auto",
  "providerID": "mimo",
  "status": "ONLINE",
  "limit": {
    "context": 64000
  }
}
"""
    assert parse_mimo_models(raw) == ['openai/gpt-5.4', 'mimo/mimo-auto']


def test_ping_openai_models_pings_each_discovered_model(monkeypatch):
    models_endpoint = "https://example.test/v1/models"
    chat_endpoint = "https://example.test/v1/chat/completions"
    monkeypatch.setattr(
        ping_all_models,
        "resolve_openai_provider_config",
        lambda: SimpleNamespace(
            api_key="token",
            models_endpoint=models_endpoint,
            chat_completions_endpoint=chat_endpoint,
        ),
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
        lambda timeout=None: _FakeAsyncClient(
            get_map={
                models_endpoint: _FakeResponse(
                    200,
                    payload={"data": [{"id": "gpt-ok"}, {"id": "gpt-fail"}]},
                )
            },
            post_handler=post_handler,
        ),
    )

    report = asyncio.run(ping_all_models.ping_openai_models("reply with pong only"))

    assert seen_models == ["gpt-ok", "gpt-fail"]
    assert report["ok"] == 1
    assert report["failed"] == 1
    assert [row["model"] for row in report["models"]] == ["gpt-ok", "gpt-fail"]
    assert report["models"][0]["response_sample"] == "pong from gpt-ok"
    assert report["models"][1]["error"] == "rate limit"


def test_ping_mistral_models_skips_non_chat_and_pings_chat_models(monkeypatch):
    monkeypatch.setattr(
        ping_all_models,
        "MistralManager",
        lambda: SimpleNamespace(api_key="token", base_url="https://mistral.test/v1"),
    )

    called_models: list[str] = []

    def post_handler(url: str, payload: dict[str, Any]) -> _FakeResponse:
        assert url == "https://mistral.test/v1/chat/completions"
        called_models.append(payload["model"])
        return _FakeResponse(200, payload={"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(
        ping_all_models.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeAsyncClient(
            get_map={
                "https://mistral.test/v1/models": _FakeResponse(
                    200,
                    payload={"data": [{"id": "mistral-small-latest"}, {"id": "mistral-embed-2312"}]},
                )
            },
            post_handler=post_handler,
        ),
    )

    report = asyncio.run(ping_all_models.ping_mistral_models("reply with pong only"))

    assert called_models == ["mistral-small-latest"]
    assert report["ok"] == 1
    assert report["failed"] == 0
    assert report["skipped_non_chat"] == 1
    assert report["models"] == [
        {"model": "mistral-embed-2312", "ok": False, "skipped": True, "skip_reason": "embedding_model"},
        {"model": "mistral-small-latest", "ok": True, "response_sample": "pong", "status_code": 200},
    ]


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
        lambda timeout=None: _FakeAsyncClient(
            get_map={
                "http://local-llm.test/api/tags": _FakeResponse(
                    200,
                    payload={"models": [{"name": "qwen2.5:7b"}, {"name": "broken-model"}]},
                )
            },
            post_handler=post_handler,
        ),
    )

    report = asyncio.run(ping_all_models.ping_local_llm_models("reply with pong only"))

    assert called_models == ["qwen2.5:7b", "broken-model"]
    assert report["ok"] == 1
    assert report["failed"] == 1
    assert report["models"][0]["response_sample"] == "pong from qwen2.5:7b"
    assert report["models"][1]["error"] == "unavailable"


def test_ping_mimo_models_runs_all_discovered_models_and_writes_partial_report(monkeypatch, tmp_path):
    monkeypatch.setattr(ping_all_models, "resolve_mimo_cli", lambda: "/usr/bin/mimo")

    inventory = """openai/gpt-5.4
{
  "id": "gpt-5.4",
  "providerID": "openai",
  "status": "ONLINE"
}
xiaomi/mimo-v2-pro
{
  "id": "mimo-v2-pro",
  "providerID": "xiaomi",
  "status": "ONLINE"
}
"""

    async def fake_create_subprocess_exec(*args, **kwargs):
        if args == ("/usr/bin/mimo", "models", "--verbose"):
            return _FakeProcess(stdout=inventory)
        if args[0:6] == ("timeout", "15s", "/usr/bin/mimo", "run", "-m", "openai/gpt-5.4"):
            return _FakeProcess(stdout='{"type":"text","part":{"text":"pong openai"}}\n')
        if args[0:6] == ("timeout", "15s", "/usr/bin/mimo", "run", "-m", "xiaomi/mimo-v2-pro"):
            return _FakeProcess(stdout='{"type":"text","part":{"text":"pong mimo"}}\n')
        raise AssertionError(f"unexpected subprocess args: {args!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    report = asyncio.run(ping_all_models.ping_mimo_models("reply with pong only", tmp_path))

    assert report["ok"] == 2
    assert report["failed"] == 0
    assert [row["model"] for row in report["models"]] == ["openai/gpt-5.4", "xiaomi/mimo-v2-pro"]
    partial = json.loads((tmp_path / "mimo_model_ping_report.partial.json").read_text(encoding="utf-8"))
    assert partial["completed"] == 2
    assert partial["ok"] == 2


def test_ping_antigravity_marks_all_status_models_from_single_probe(monkeypatch):
    monkeypatch.setattr(
        ping_all_models,
        "AntigravityManager",
        lambda: SimpleNamespace(
            status=lambda: {
                "ready": True,
                "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
            }
        ),
    )
    monkeypatch.setattr(
        ping_all_models.ExternalAIBridge,
        "resolve_antigravity_cli_command",
        staticmethod(lambda: ["/usr/bin/gemini"]),
    )
    monkeypatch.setattr(
        ping_all_models.ExternalAIBridge,
        "_antigravity_runtime_env",
        staticmethod(lambda: {"PATH": "/usr/bin"}),
    )

    seen_args: list[tuple[Any, ...]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen_args.append(args)
        return _FakeProcess(stdout="pong", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    report = asyncio.run(ping_all_models.ping_antigravity("reply with pong only"))

    assert seen_args == [
        ("/usr/bin/gemini", "-p", "reply with pong only", "--skip-trust"),
    ]
    assert report["ok"] == 2
    assert report["failed"] == 0
    assert [row["model"] for row in report["models"]] == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert all(row["response_sample"] == "pong" for row in report["models"])


def test_run_all_models_only_provider_mistral_marks_others_not_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ping_all_models,
        "ping_mistral_models",
        lambda prompt, skip_non_chat=True: asyncio.sleep(0, result={"provider": "mistral", "models": [{"model": "mistral-large-latest", "ok": True}], "ok": 1, "failed": 0, "skipped": False, "skipped_non_chat": 0}),
    )

    report, mimo_report, artifacts = asyncio.run(
        ping_all_models.run_all_models("reply with pong only", tmp_path, only_provider="mistral")
    )

    assert report["mistral"]["ok"] == 1
    assert report["openai"]["skipped"] is True
    assert report["local_llm"]["skipped"] is True
    assert report["antigravity"]["skipped"] is True
    assert mimo_report["skipped"] is True
    assert artifacts["failed"]["mistral"]["failed_count"] == 0


def test_main_async_writes_reports_for_all_provider_sweeps(monkeypatch, tmp_path):
    report = {
        "openai": {"provider": "openai", "ok": 1, "failed": 0, "models": [{"model": "gpt-ok", "ok": True}]},
        "mistral": {"provider": "mistral", "ok": 1, "failed": 0, "skipped_non_chat": 0, "models": [{"model": "mistral-small", "ok": True}]},
        "local_llm": {"provider": "local_llm", "ok": 1, "failed": 0, "models": [{"model": "qwen", "ok": True}]},
        "antigravity": {"provider": "antigravity", "ok": 1, "failed": 0, "models": [{"model": "gemini", "ok": True}]},
    }
    mimo_report = {"provider": "mimo", "ok": 1, "failed": 1, "models": [{"model": "mimo-ok", "ok": True}, {"model": "mimo-bad", "ok": False}]}
    artifacts = {
        "failed": {
            "openai": {"provider": "openai", "failed_count": 0, "total": 1, "models": []},
            "mistral": {"provider": "mistral", "failed_count": 0, "total": 1, "models": []},
            "local_llm": {"provider": "local_llm", "failed_count": 0, "total": 1, "models": []},
            "antigravity": {"provider": "antigravity", "failed_count": 0, "total": 1, "models": []},
            "mimo": {"provider": "mimo", "failed_count": 1, "total": 2, "models": [{"model": "mimo-bad", "ok": False}]},
        },
        "mimo_usable": {
            "provider": "mimo",
            "usable_count": 1,
            "total": 2,
            "models": [{"model": "mimo-ok", "ok": True}],
        },
    }
    monkeypatch.setattr(
        ping_all_models,
        "run_all_models",
        lambda prompt, output_dir, skip_mistral_non_chat=True, only_provider=None: asyncio.sleep(0, result=(report, mimo_report, artifacts)),
    )

    args = argparse.Namespace(
        prompt="reply with pong only",
        output_dir=str(tmp_path),
        include_mistral_non_chat=False,
        only_provider=None,
    )

    exit_code = asyncio.run(ping_all_models.main_async(args))

    assert exit_code == 0
    assert json.loads((tmp_path / "model_ping_report.json").read_text(encoding="utf-8")) == report
    assert json.loads((tmp_path / "mimo_model_ping_report.json").read_text(encoding="utf-8")) == mimo_report
    assert json.loads((tmp_path / "failed_models_by_provider.json").read_text(encoding="utf-8")) == artifacts["failed"]
    assert json.loads((tmp_path / "mimo_usable_models.json").read_text(encoding="utf-8")) == artifacts["mimo_usable"]
