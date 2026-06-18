from __future__ import annotations

import os

from core.core.env_loader import load_env_file
from core.core.provider_credentials import credential_snapshot


NONSECRET_GOOGLE_KEY = "google_live_value_1234567890abcdefghijkl"


def test_google_style_api_key_is_not_treated_as_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", NONSECRET_GOOGLE_KEY)

    snapshot = credential_snapshot(("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))

    assert snapshot["configured"] is True
    assert snapshot["usable"] is True
    assert snapshot["placeholder"] is False
    assert snapshot["env_var"] in {"ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"}


def test_load_env_file_syncs_antigravity_aliases(tmp_path, monkeypatch) -> None:
    for env_name in ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env_name, raising=False)

    env_file = tmp_path / ".env.bridge"
    env_file.write_text(f"ANTIGRAVITY_API_KEY={NONSECRET_GOOGLE_KEY}\n", encoding="utf-8")

    load_env_file(str(env_file), override=True)

    assert os.getenv("ANTIGRAVITY_API_KEY") == NONSECRET_GOOGLE_KEY
    assert os.getenv("GEMINI_API_KEY") == NONSECRET_GOOGLE_KEY
    assert os.getenv("GOOGLE_API_KEY") == NONSECRET_GOOGLE_KEY


def test_load_env_file_syncs_github_aliases(tmp_path, monkeypatch) -> None:
    for env_name in ("GITHUB_API", "GITHUB_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HOST_BRIDGE_GH_TOKEN"):
        monkeypatch.delenv(env_name, raising=False)

    env_file = tmp_path / ".env.bridge"
    env_file.write_text("GITHUB_API=github_nonsecret_token_value_1234567890\n", encoding="utf-8")

    load_env_file(str(env_file), override=True)

    assert os.getenv("GITHUB_API") == "github_nonsecret_token_value_1234567890"
    assert os.getenv("GITHUB_API_KEY") == "github_nonsecret_token_value_1234567890"
    assert os.getenv("GITHUB_TOKEN") == "github_nonsecret_token_value_1234567890"
    assert os.getenv("GH_TOKEN") == "github_nonsecret_token_value_1234567890"
    assert os.getenv("HOST_BRIDGE_GH_TOKEN") == "github_nonsecret_token_value_1234567890"


def test_load_env_file_syncs_openai_codex_sale_aliases(tmp_path, monkeypatch) -> None:
    for env_name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_SALE_API_KEY", "CODEX_SALE_BASE_URL", "OPENAI_TCP_PROBE_HOSTS"):
        monkeypatch.delenv(env_name, raising=False)

    env_file = tmp_path / ".env.bridge"
    env_file.write_text("CODEX_SALE_API_KEY=openai_nonsecret_key_value_1234567890\nCODEX_SALE_BASE_URL=https://codex.sale\n", encoding="utf-8")

    load_env_file(str(env_file), override=True)

    assert os.getenv("OPENAI_API_KEY") == "openai_nonsecret_key_value_1234567890"
    assert os.getenv("CODEX_SALE_API_KEY") == "openai_nonsecret_key_value_1234567890"
    assert os.getenv("OPENAI_BASE_URL") == "https://codex.sale/v1"
    assert os.getenv("OPENAI_TCP_PROBE_HOSTS") == "codex.sale:443"
