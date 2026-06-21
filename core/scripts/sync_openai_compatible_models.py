from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.openai_compatible_inventory import sync_openai_compatible_artifacts
from core.core.openai_provider import resolve_openai_provider_config
from core.core.provider_credentials import sync_provider_env_aliases


def _load_repo_env() -> None:
    env_path = ROOT / ".env.bridge"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
    sync_provider_env_aliases()


def _fetch_models(models_endpoint: str, api_key: str) -> dict[str, object]:
    req = request.Request(
        models_endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync OpenAI-compatible model inventory from the configured /v1/models endpoint.")
    parser.add_argument("--models-endpoint", default="", help="Override the models endpoint. Defaults to the configured OpenAI-compatible endpoint.")
    parser.add_argument("--base-url", default="", help="Override the base URL stored in generated artifacts.")
    parser.add_argument("--print-models", action="store_true", help="Print the resolved model ids after syncing.")
    args = parser.parse_args()

    _load_repo_env()
    cfg = resolve_openai_provider_config()
    models_endpoint = str(args.models_endpoint or cfg.models_endpoint).strip()
    base_url = str(args.base_url or cfg.base_url).strip()
    api_key = str(cfg.api_key or "").strip()

    if not api_key:
        print(json.dumps({"ok": False, "error": "missing_api_key"}, ensure_ascii=True))
        return 1
    if not models_endpoint:
        print(json.dumps({"ok": False, "error": "missing_models_endpoint"}, ensure_ascii=True))
        return 1

    try:
        payload = _fetch_models(models_endpoint, api_key)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print(json.dumps({"ok": False, "error": "http_error", "status_code": exc.code, "body": body[:400]}, ensure_ascii=True))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=True))
        return 1

    data = payload.get("data") if isinstance(payload, dict) else []
    rows = data if isinstance(data, list) else []
    models: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id:
            models.append(model_id)

    result = sync_openai_compatible_artifacts(models, base_url=base_url)
    summary = {
        "ok": True,
        "provider": "openai",
        "models_endpoint": models_endpoint,
        "base_url": base_url,
        "fetched_models": len(models),
        "synced_models": result.get("total_models"),
        "openai_family_count": result.get("openai_family_count"),
        "cache_path": result.get("cache_path"),
        "full_cache_path": result.get("full_cache_path"),
        "generated_profile_root": result.get("generated_profile_root"),
    }
    if args.print_models:
        summary["models"] = models
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
