from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ControlProfile:
    slug: str
    name: str
    description: str
    ask_confirmation: bool = False
    auto_approve_safe_tasks: bool = True
    require_confirmation_for_destructive: bool = False
    non_interactive_default: bool = False
    blocked_risk_levels: tuple[str, ...] = ()
    confirmation_policy: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DevToolkitModeProfile:
    slug: str
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    allow_code_changes: bool = False
    allow_execution: bool = False
    dry_run: bool = False
    permissions: tuple[str, ...] = ()
    task_constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()


class _OperationProfileStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or Path(__file__).resolve().parent / "profiles" / "operation_profiles.json")
        self._payload: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self._payload is not None:
            return self._payload
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._payload = raw if isinstance(raw, dict) else {}
        return self._payload


class ControlProfileRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self._store = _OperationProfileStore(path)
        payload = self._store.load()
        profiles = payload.get("control_profiles") if isinstance(payload, dict) else []
        self._profiles: dict[str, ControlProfile] = {}
        for item in profiles if isinstance(profiles, list) else []:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip().lower()
            if not slug:
                continue
            self._profiles[slug] = ControlProfile(
                slug=slug,
                name=str(item.get("name") or slug.upper()),
                description=str(item.get("description") or ""),
                ask_confirmation=bool(item.get("ask_confirmation", False)),
                auto_approve_safe_tasks=bool(item.get("auto_approve_safe_tasks", True)),
                require_confirmation_for_destructive=bool(item.get("require_confirmation_for_destructive", False)),
                non_interactive_default=bool(item.get("non_interactive_default", False)),
                blocked_risk_levels=tuple(str(level).strip().lower() for level in item.get("blocked_risk_levels", []) if str(level).strip()),
                confirmation_policy={
                    str(key): bool(value)
                    for key, value in (item.get("confirmation_policy") or {}).items()
                },
            )

    def get(self, slug: str | None) -> ControlProfile:
        normalized = str(slug or "full_auto").strip().lower() or "full_auto"
        profile = self._profiles.get(normalized)
        if profile is not None:
            return profile
        fallback = self._profiles.get("full_auto")
        if fallback is None:
            raise KeyError(f"Missing control profile: {normalized}")
        return fallback


class DevToolkitModeRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self._store = _OperationProfileStore(path)
        payload = self._store.load()
        modes = payload.get("devtoolkit_modes") if isinstance(payload, dict) else []
        self._profiles: dict[str, DevToolkitModeProfile] = {}
        self._aliases: dict[str, str] = {}
        for item in modes if isinstance(modes, list) else []:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip().lower()
            if not slug:
                continue
            profile = DevToolkitModeProfile(
                slug=slug,
                name=str(item.get("name") or slug),
                description=str(item.get("description") or ""),
                aliases=tuple(str(alias).strip().lower() for alias in item.get("aliases", []) if str(alias).strip()),
                allow_code_changes=bool(item.get("allow_code_changes", False)),
                allow_execution=bool(item.get("allow_execution", False)),
                dry_run=bool(item.get("dry_run", False)),
                permissions=tuple(str(value).strip() for value in item.get("permissions", []) if str(value).strip()),
                task_constraints=tuple(str(value).strip() for value in item.get("task_constraints", []) if str(value).strip()),
                acceptance_criteria=tuple(str(value).strip() for value in item.get("acceptance_criteria", []) if str(value).strip()),
            )
            self._profiles[slug] = profile
            self._aliases[slug] = slug
            for alias in profile.aliases:
                self._aliases[alias] = slug

    def resolve(self, mode: str | None) -> DevToolkitModeProfile:
        normalized = str(mode or "plan").strip().lower() or "plan"
        target = self._aliases.get(normalized, "plan")
        profile = self._profiles.get(target)
        if profile is None:
            raise KeyError(f"Missing devtoolkit mode profile: {target}")
        return profile
