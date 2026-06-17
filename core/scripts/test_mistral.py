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
    status = manager.status()
    print(json.dumps({
        "configured": bool(credential.get("configured")),
        "usable_by_policy": bool(credential.get("usable")),
        "placeholder": bool(credential.get("placeholder")),
        "ready": bool(status.get("ready")),
        "model_count": len(status.get("models", [])),
        "status_code": (status.get("api_probe") or {}).get("status_code"),
        "error": (status.get("api_probe") or {}).get("error"),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
