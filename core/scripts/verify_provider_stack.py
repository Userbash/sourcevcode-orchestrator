from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.core.env_loader import load_env_file
from core.core.integrations.antigravity_manager import AntigravityManager
from core.core.integrations.mistral_manager import MistralManager
from core.core.local_llm_module import LocalLLMModule
from core.core.mimo_provider import configured_native_mimo_models, extract_mimo_response_text, invoke_mimo_native
from core.core.provider_credentials import credential_snapshot
from core.core.provider_inventory_service import ProviderInventoryService
from core.scripts.verify_openai_bridge import build_summary as build_openai_summary

load_env_file(".env")
load_env_file(".env.bridge", override=True)
load_env_file(".env.local.secrets", override=True)
load_env_file(".env.gemini.local", override=True)


def _mimo_inventory() -> tuple[dict[str, object], list[str], None]:
    model_ids = configured_native_mimo_models()
    return ({"configured": True, "ready": bool(model_ids), "error": None if model_ids else "native_model_catalog_empty", "model_count": len(model_ids), "sample_models": model_ids[:8]}, model_ids, None)


def _mimo_summary() -> dict[str, object]:
    summary, _, _ = _mimo_inventory()
    return summary


def _preferred_mimo_probe_models(model_ids: list[str]) -> list[str]:
    preferred = [
        "xiaomi/mimo-v2.5-pro",
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2-pro",
        "xiaomi/mimo-v2-omni",
        "xiaomi/mimo-v2-flash",
    ]
    ordered = []
    seen = set()
    for item in preferred:
        if item in model_ids and item not in seen:
            ordered.append(item)
            seen.add(item)
    for item in model_ids:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered[:12]


def _run_mimo_probe() -> dict[str, object]:
    summary, model_ids, _ = _mimo_inventory()
    if not summary.get("ready"):
        return {"run_model": None, "run_ready": False, "run_error": "mimo_inventory_unavailable", "run_response_sample": None, "attempted_models": [], "status_code": None}
    prompt = "reply with ok only and no reasoning"
    attempted_models: list[str] = []
    last_result = {"run_model": None, "run_ready": False, "run_error": "mimo_inventory_unavailable", "run_response_sample": None, "status_code": None}
    for model in _preferred_mimo_probe_models(model_ids):
        attempted_models.append(model)
        payload, error_text, status_code = invoke_mimo_native(model, prompt, timeout_sec=30.0, max_completion_tokens=128, temperature=0.0)
        text_output = extract_mimo_response_text(payload) if payload else ""
        last_result = {"run_model": model, "run_ready": bool(text_output), "run_error": error_text if not text_output else None, "run_response_sample": text_output[:160] if text_output else None, "status_code": status_code}
        if last_result.get("run_ready"):
            return {**last_result, "attempted_models": attempted_models}
    return {**last_result, "attempted_models": attempted_models}


def _mimo_run_summary() -> dict[str, object]:
    return _run_mimo_probe()


def _mimo_native_run_summary() -> dict[str, object]:
    return _run_mimo_probe()


def _ai_kernel_summary() -> dict[str, object]:
    if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"configured": False, "ready": False, "error": "ai_kernel_disabled_by_env", "model_count": 0, "sample_models": [], "status_code": None, "base_url": None, "model_alias": None}
    base_url = (os.getenv("AI_KERNEL_BASE_URL") or "http://127.0.0.1:8012/v1").rstrip('/')
    model_alias = (os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip()
    try:
        response = requests.get(f"{base_url}/models", headers={"Authorization": f"Bearer {os.getenv('AI_KERNEL_API_KEY', 'local')}"}, timeout=5.0)
        payload = response.json() if response.content else {}
        models = [str(item.get("id") or "").strip() for item in (payload.get("data") or []) if str(item.get("id") or "").strip()] if isinstance(payload, dict) else []
        return {"configured": True, "ready": response.status_code == 200 and bool(models), "error": None if response.status_code == 200 else response.text[:240], "model_count": len(models), "sample_models": models[:8], "status_code": response.status_code, "base_url": base_url, "model_alias": model_alias}
    except Exception as exc:
        return {"configured": True, "ready": False, "error": str(exc), "model_count": 0, "sample_models": [], "status_code": None, "base_url": base_url, "model_alias": model_alias}

def _local_llm_summary() -> dict[str, object]:
    probe = LocalLLMModule().check_health()
    models = [str(item).strip() for item in probe.get("available_models", []) if str(item).strip()]
    return {
        "configured": True,
        "ready": bool(probe.get("ok")),
        "error": probe.get("error"),
        "model_count": len(models),
        "sample_models": models[:8],
        "model_present": bool(probe.get("model_present")),
        "status_code": probe.get("status_code"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Quick provider health summary for openai, mimo, mistral, antigravity, and local LLM.")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when any configured provider or local LLM is not ready")
    args = parser.parse_args()

    mistral_credential = credential_snapshot(("MISTRAL_API_KEY",))
    antigravity_credential = credential_snapshot(("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
    mimo_credential = credential_snapshot(("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY"))
    github_credential = credential_snapshot(("GITHUB_API", "GITHUB_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HOST_BRIDGE_GH_TOKEN"))

    openai_summary = build_openai_summary()
    mimo_summary = _mimo_summary()
    mimo_run_summary = _mimo_run_summary() if mimo_summary.get("ready") else {"run_model": None, "run_ready": False, "run_error": "mimo_inventory_unavailable", "run_response_sample": None, "attempted_models": []}
    mimo_native_run_summary = _mimo_native_run_summary() if mimo_summary.get("ready") else {"run_model": None, "run_ready": False, "run_error": "mimo_inventory_unavailable", "run_response_sample": None, "attempted_models": []}
    inventory = ProviderInventoryService()
    snapshot = inventory.read_snapshot()
    providers_snapshot = snapshot.get("providers", {}) if isinstance(snapshot, dict) else {}
    mistral_probe = MistralManager().probe_models()
    antigravity_status = AntigravityManager().status()
    antigravity_api_probe = antigravity_status.get("api_probe") or {}
    local_llm_summary = _local_llm_summary()
    ai_kernel_summary = _ai_kernel_summary()

    report = {
        "inventory_snapshot": {"updated_at": snapshot.get("updated_at"), "providers": sorted(providers_snapshot)} if isinstance(snapshot, dict) else {"updated_at": None, "providers": []},
        "openai": openai_summary,
        "mimo": {
            **mimo_summary,
            **mimo_run_summary,
            "native_run_model": mimo_native_run_summary.get("run_model"),
            "native_run_ready": mimo_native_run_summary.get("run_ready"),
            "native_run_error": mimo_native_run_summary.get("run_error"),
            "native_run_response_sample": mimo_native_run_summary.get("run_response_sample"),
            "native_attempted_models": mimo_native_run_summary.get("attempted_models"),
            "credential_configured": bool(mimo_credential.get("configured")),
            "credential_usable": bool(mimo_credential.get("usable")),
            "credential_env": mimo_credential.get("env_var"),
            "github_token_configured": bool(github_credential.get("configured")),
            "github_token_env": github_credential.get("env_var"),
            "inventory_ready": bool(mimo_summary.get("ready")),
            "ready": bool(mimo_native_run_summary.get("run_ready")) if mimo_native_run_summary.get("attempted_models") else bool(mimo_run_summary.get("run_ready")),
            "error": (
                mimo_native_run_summary.get("run_error")
                if mimo_native_run_summary.get("attempted_models") and not mimo_native_run_summary.get("run_ready")
                else (mimo_run_summary.get("run_error") if not mimo_run_summary.get("run_ready") else mimo_summary.get("error"))
            ),
        },
        "local_llm": local_llm_summary,
        "ai_kernel": ai_kernel_summary,
        "mistral": {
            "configured": bool(mistral_credential.get("configured")),
            "usable_by_policy": bool(mistral_credential.get("usable")),
            "placeholder": bool(mistral_credential.get("placeholder")),
            "ready": bool(mistral_probe.get("ok")),
            "status_code": mistral_probe.get("status_code"),
            "model_count": len(mistral_probe.get("models", [])),
            "error": mistral_probe.get("error"),
            "inventory_source": mistral_probe.get("inventory_source") or ((providers_snapshot.get("mistral") or {}).get("source") if isinstance(providers_snapshot, dict) else None),
            "snapshot_model_count": len(((providers_snapshot.get("mistral") or {}).get("models") or [])) if isinstance(providers_snapshot, dict) else 0,
        },
        "antigravity": {
            "configured": bool(antigravity_credential.get("configured")),
            "usable_by_policy": bool(antigravity_credential.get("usable")),
            "placeholder": bool(antigravity_credential.get("placeholder")),
            "ready": bool(antigravity_status.get("ready")),
            "auth_mode": antigravity_status.get("auth_mode"),
            "status_code": antigravity_api_probe.get("status_code"),
            "inventory_ok": antigravity_status.get("inventory_ok"),
            "inventory_source": antigravity_status.get("inventory_source"),
            "inventory_probe_kind": antigravity_status.get("inventory_probe_kind"),
            "failure_kind": antigravity_status.get("failure_kind"),
            "model_count": len(antigravity_status.get("models", [])),
            "error": antigravity_api_probe.get("error") or (antigravity_status.get("generation_probe") or {}).get("stderr") or (antigravity_status.get("models_probe") or {}).get("error") or (antigravity_status.get("auth_probe") or {}).get("error"),
            "snapshot_model_count": len(((providers_snapshot.get("antigravity") or {}).get("models") or [])) if isinstance(providers_snapshot, dict) else 0,
        },
    }
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.strict:
        failures = [
            bool(openai_summary.get("usable_by_policy")) and not bool(openai_summary.get("ready")),
            not bool(local_llm_summary.get("ready")),
            not bool(ai_kernel_summary.get("ready")) if ai_kernel_summary.get("configured") else False,
            bool(mistral_credential.get("usable")) and not bool(mistral_probe.get("ok")),
            bool(antigravity_credential.get("usable")) and not bool(antigravity_status.get("ready")),
            bool(mimo_credential.get("usable")) and not bool((mimo_native_run_summary.get("run_ready") if mimo_native_run_summary.get("attempted_models") else mimo_run_summary.get("run_ready"))),
        ]
        raise SystemExit(1 if any(failures) else 0)


if __name__ == "__main__":
    main()
