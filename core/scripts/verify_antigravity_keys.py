from __future__ import annotations

import json

from core.core.env_loader import load_env_file
from core.core.integrations.antigravity_manager import AntigravityManager
from core.core.provider_credentials import credential_snapshot

load_env_file(".env")
load_env_file(".env.bridge", override=True)
load_env_file(".env.gemini.local", override=True)


def main() -> None:
    credential = credential_snapshot(("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
    manager = AntigravityManager()
    status = manager.status()
    api_probe = status.get("api_probe") or {}
    summary = {
        "provider": "antigravity",
        "configured": bool(credential.get("configured")),
        "usable_by_policy": bool(credential.get("usable")),
        "placeholder": bool(credential.get("placeholder")),
        "ready": bool(status.get("ready")),
        "auth_mode": status.get("auth_mode"),
        "inventory_ok": status.get("inventory_ok"),
        "inventory_source": status.get("inventory_source"),
        "inventory_probe_kind": status.get("inventory_probe_kind"),
        "model_count": len(status.get("models", [])),
        "api_status_code": api_probe.get("status_code"),
        "error": api_probe.get("error") or (status.get("generation_probe") or {}).get("stderr") or (status.get("auth_probe") or {}).get("error") or (status.get("models_probe") or {}).get("error"),
    }
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    main()
