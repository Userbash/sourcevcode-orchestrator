from __future__ import annotations

from typing import Any


class InventoryScoringPolicy:
    @staticmethod
    def lane_bonus(*, provider: str, runtime_entry: dict[str, Any] | None, model_row: dict[str, Any] | None, model_name: str = "") -> float:
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider in {"", "local"}:
            return 0.0
        runtime_entry = runtime_entry if isinstance(runtime_entry, dict) else {}
        diagnostics = runtime_entry.get("diagnostics") if isinstance(runtime_entry.get("diagnostics"), dict) else {}
        model_row = model_row if isinstance(model_row, dict) else {}
        status = str(runtime_entry.get("status") or diagnostics.get("inventory_status") or "").strip().lower()
        score = 0.0
        if status == "ready":
            score += 0.2
        elif status == "degraded":
            score += 0.05
        elif status in {"offline", "auth_failed", "quota_exceeded"}:
            score -= 0.3
        if bool(diagnostics.get("model_alias_present")):
            score += 0.2
        if bool(model_row.get("resident")):
            score += 0.25
        if normalized_provider == "local_llm" and bool(diagnostics.get("model_present")):
            score += 0.15
        if normalized_provider == "ai_kernel" and diagnostics.get("inventory_status") == "degraded":
            score -= 0.15
        if normalized_provider == "antigravity" and not bool(diagnostics.get("model_alias_present", True)):
            score -= 0.1
        if normalized_provider == "local_llm" and model_name and model_name != str(diagnostics.get("default_model") or "") and not bool(model_row.get("resident")):
            score -= 0.05
        return max(-0.4, min(0.6, score))

    @staticmethod
    def lane_preferred(*, provider: str, runtime_entry: dict[str, Any] | None, model_row: dict[str, Any] | None) -> bool:
        bonus = InventoryScoringPolicy.lane_bonus(provider=provider, runtime_entry=runtime_entry, model_row=model_row)
        return bonus > 0.15
