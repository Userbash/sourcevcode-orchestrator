from __future__ import annotations

import argparse
import json
import subprocess


from core.core.env_loader import load_env_file
from core.core.integrations.antigravity_manager import AntigravityManager
from core.core.integrations.mistral_manager import MistralManager
from core.core.local_llm_module import LocalLLMModule
from core.core.provider_credentials import credential_snapshot
from core.scripts.verify_openai_bridge import build_summary as build_openai_summary

load_env_file(".env")
load_env_file(".env.bridge", override=True)
load_env_file(".env.gemini.local", override=True)


def _mimo_summary() -> dict[str, object]:
    try:
        proc = subprocess.run(["mimo", "models", "--verbose"], capture_output=True, text=True, timeout=20, check=False)
    except FileNotFoundError:
        return {"configured": False, "ready": False, "error": "mimo_cli_missing", "model_count": 0, "sample_models": []}
    except Exception as exc:
        return {"configured": True, "ready": False, "error": str(exc), "model_count": 0, "sample_models": []}

    if proc.returncode != 0:
        return {"configured": True, "ready": False, "error": (proc.stderr or proc.stdout or f"exit_{proc.returncode}").strip(), "model_count": 0, "sample_models": []}

    model_ids = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{") or stripped.startswith("}") or stripped.startswith('\"') or stripped.startswith("["):
            continue
        if "/" in stripped and " " not in stripped and ":" not in stripped:
            model_ids.append(stripped)
    deduped = []
    seen = set()
    for item in model_ids:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return {"configured": True, "ready": True, "error": None, "model_count": len(deduped), "sample_models": deduped[:8]}


def _mimo_run_summary() -> dict[str, object]:
    model = "mimo/mimo-auto"
    prompt = "reply with ok"
    try:
        proc = subprocess.run(["mimo", "run", "-m", model, "--format", "json", prompt], capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        return {"run_model": model, "run_ready": False, "run_error": "mimo_cli_missing", "run_response_sample": None}
    except Exception as exc:
        return {"run_model": model, "run_ready": False, "run_error": str(exc), "run_response_sample": None}

    events = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue

    for event in events:
        if isinstance(event, dict) and event.get("type") == "error":
            message = (((event.get("error") or {}).get("data") or {}).get("message") or (event.get("error") or {}).get("name") or "mimo_run_failed")
            return {"run_model": model, "run_ready": False, "run_error": str(message), "run_response_sample": None}

    text_parts = []
    for event in events:
        if isinstance(event, dict) and event.get("type") == "text":
            part = event.get("part") or {}
            text = str(part.get("text") or "").strip()
            if text:
                text_parts.append(text)

    if text_parts:
        sample = " ".join(text_parts).strip()
        return {"run_model": model, "run_ready": True, "run_error": None, "run_response_sample": sample[:160]}

    stderr = (proc.stderr or "").strip()
    return {"run_model": model, "run_ready": False, "run_error": stderr or "no_text_events", "run_response_sample": None}


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
    github_credential = credential_snapshot(("GITHUB_API", "GITHUB_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HOST_BRIDGE_GH_TOKEN"))

    openai_summary = build_openai_summary()
    mimo_summary = _mimo_summary()
    mimo_run_summary = _mimo_run_summary() if mimo_summary.get("ready") else {"run_model": None, "run_ready": False, "run_error": "mimo_inventory_unavailable", "run_response_sample": None}
    mistral_probe = MistralManager().probe_models()
    antigravity_status = AntigravityManager().status()
    antigravity_api_probe = antigravity_status.get("api_probe") or {}
    local_llm_summary = _local_llm_summary()

    report = {
        "openai": openai_summary,
        "mimo": {**mimo_summary, **mimo_run_summary, "credential_configured": bool(github_credential.get("configured")), "credential_env": github_credential.get("env_var")},
        "local_llm": local_llm_summary,
        "mistral": {
            "configured": bool(mistral_credential.get("configured")),
            "usable_by_policy": bool(mistral_credential.get("usable")),
            "placeholder": bool(mistral_credential.get("placeholder")),
            "ready": bool(mistral_probe.get("ok")),
            "status_code": mistral_probe.get("status_code"),
            "model_count": len(mistral_probe.get("models", [])),
            "error": mistral_probe.get("error"),
        },
        "antigravity": {
            "configured": bool(antigravity_credential.get("configured")),
            "usable_by_policy": bool(antigravity_credential.get("usable")),
            "placeholder": bool(antigravity_credential.get("placeholder")),
            "ready": bool(antigravity_status.get("ready")),
            "auth_mode": antigravity_status.get("auth_mode"),
            "status_code": antigravity_api_probe.get("status_code"),
            "model_count": len(antigravity_status.get("models", [])),
            "error": antigravity_api_probe.get("error") or (antigravity_status.get("models_probe") or {}).get("error") or (antigravity_status.get("auth_probe") or {}).get("error"),
        },
    }
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.strict:
        failures = [
            bool(openai_summary.get("usable_by_policy")) and not bool(openai_summary.get("ready")),
            not bool(local_llm_summary.get("ready")),
            bool(mistral_credential.get("usable")) and not bool(mistral_probe.get("ok")),
            bool(antigravity_credential.get("usable")) and not bool(antigravity_status.get("ready")),
            bool(github_credential.get("usable")) and not bool(mimo_run_summary.get("run_ready")),
        ]
        raise SystemExit(1 if any(failures) else 0)


if __name__ == "__main__":
    main()
