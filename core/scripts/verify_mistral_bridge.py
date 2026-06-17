from __future__ import annotations

import json

from core.core.env_loader import load_env_file
from core.core.integrations.mistral_manager import MistralManager
from core.core.provider_credentials import credential_snapshot

load_env_file(".env")
load_env_file(".env.bridge", override=True)


def main() -> None:
    credential = credential_snapshot(("MISTRAL_API_KEY",))
    manager = MistralManager()
    probe = manager.probe_models()
    summary = {
        "provider": "mistral",
        "configured": bool(credential.get("configured")),
        "usable_by_policy": bool(credential.get("usable")),
        "placeholder": bool(credential.get("placeholder")),
        "auth_mode": "api_key",
        "ready": bool(probe.get("ok")),
        "status_code": probe.get("status_code"),
        "model_count": len(probe.get("models", [])),
        "error": probe.get("error"),
    }
    print(json.dumps(summary, ensure_ascii=True))
    raise SystemExit(0 if summary["ready"] else 1)


if __name__ == "__main__":
    main()
