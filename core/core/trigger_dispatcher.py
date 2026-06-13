from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .kernel_protocol import KernelAPI, KernelModule
from .models import TaskType, Priority

logger = logging.getLogger("trigger_dispatcher")


@dataclass
class TriggerDispatcherModule:
    """
    Automates Orchestrator activation by detecting keywords and semantic patterns
    in the chat stream.
    """

    name: str = "trigger_dispatcher"
    _api: KernelAPI | None = None

    TRIGGERS: Dict[str, TaskType] = field(default_factory=lambda: {
        r"^(STATUS|СТАТУС|HEALTH|ЗДОРОВЬЕ)\b[:\s-]*": TaskType.RESEARCH,
        r"^(PLAN|ПЛАН|DESIGN|ДИЗАЙН|ARCH|АРХИТЕКТУРА)\b[:\s-]*": TaskType.PLAN,
        r"^(BUILD|КОД|РЕАЛИЗУЙ|WRITE|НАПИШИ|DEV|РАЗРАБОТАЙ)\b[:\s-]*": TaskType.CODE,
        r"^(FIX|ИСПРАВЬ|ПОЧИНИ|BUG|ОШИБКА|DEBUG|ОТЛАДКА)\b[:\s-]*": TaskType.FIX,
        r"^(CLEAN|ОЧИСТИ|REFACTOR|РЕФАКТОР|OPTIMIZE|ОПТИМИЗИРУЙ)\b[:\s-]*": TaskType.FIX,
        r"^(REVIEW|РЕВЬЮ|ПРОВЕРЬ|AUDIT|АУДИТ|SECURITY|БЕЗОПАСНОСТЬ|SCAN|СКАНИРУЙ)\b[:\s-]*": TaskType.REVIEW,
        r"^(TEST|ТЕСТ|ПРОТЕСТИРУЙ|CHECK|ПРОВЕРКА)\b[:\s-]*": TaskType.TEST,
        r"^(RESEARCH|ИССЛЕДУЙ|FIND|НАЙДИ|ANALYZE|АНАЛИЗИРУЙ|LEARN|ИЗУЧИ)\b[:\s-]*": TaskType.RESEARCH,
        r"^(DOCS|ДОКУМЕНТАЦИЯ|DOC|ОПИШИ|README)\b[:\s-]*": TaskType.DOCS,
        r"^(DEPLOY|ДЕПЛОЙ|START|ЗАПУСТИ|RUN)\b[:\s-]*": TaskType.CODE,
    })

    CORE_PREFIX = re.compile(
        r"^\s*(core|ядро|яжро|ядра|ядер|ai|ии)\b[\s,:;\-]*(please|пожалуйста)?[\s,:;\-]*",
        re.IGNORECASE,
    )

    CORE_ROUTING_PATTERNS: tuple[tuple[re.Pattern[str], TaskType], ...] = (
        (re.compile(r"^(декомпоз|разбей|раздели|спланир|план|plan|decompose|break down|roadmap|архитект)", re.IGNORECASE), TaskType.PLAN),
        (re.compile(r"^(статус|состояни|health|status|исслед|analy|research|найди|find)", re.IGNORECASE), TaskType.RESEARCH),
        (re.compile(r"^(исправ(?:ь|ить|им)?|почини(?:ть)?|починить|fix|bug|отлад(?:ь|ить|ка)?)\b", re.IGNORECASE), TaskType.FIX),
        (re.compile(r"^(проверь|review|audit|security|scan|ревью)", re.IGNORECASE), TaskType.REVIEW),
        (re.compile(r"^(тест|check|test|протест)", re.IGNORECASE), TaskType.TEST),
        (re.compile(r"^(док|docs|readme|опиши)", re.IGNORECASE), TaskType.DOCS),
        (re.compile(r"^(реализ|напиш|build|write|dev|разработ|запуст)", re.IGNORECASE), TaskType.CODE),
    )

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[TRIGGER] {self.name} system active. Monitoring chat for {len(self.TRIGGERS)} categories.")

    @staticmethod
    def _normalize_description(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip(" ,:-\n\t")

    def _detect_prefixed_core(self, text: str) -> tuple[bool, str]:
        match = self.CORE_PREFIX.match(text)
        if not match:
            return False, text.strip()
        remainder = self._normalize_description(text[match.end():])
        return True, remainder

    def _infer_core_task_type(self, text: str) -> tuple[TaskType, str]:
        normalized = self._normalize_description(text)
        lowered = normalized.lower()

        if any(token in lowered for token in ("декомпоз", "разбей", "раздели", "спланир", "plan", "decompose", "roadmap", "архитект")):
            return TaskType.PLAN, normalized
        if any(token in lowered for token in ("статус", "состояни", "health", "status")):
            return TaskType.RESEARCH, normalized

        for pattern, task_type in self.CORE_ROUTING_PATTERNS:
            if pattern.search(normalized):
                description = self._normalize_description(pattern.sub("", normalized, count=1)) or normalized
                return task_type, description

        return TaskType.PLAN, normalized or "Auto-triggered orchestration task"

    def _match_legacy_trigger(self, text: str) -> Optional[Dict[str, Any]]:
        clean_text = self._normalize_description(text)
        for pattern, task_type in self.TRIGGERS.items():
            if re.search(pattern, clean_text, re.IGNORECASE):
                description = self._normalize_description(re.sub(pattern, "", clean_text, count=1, flags=re.IGNORECASE))
                return {
                    "type": task_type.value,
                    "description": description or "Auto-triggered task",
                    "priority": "high" if "!!!" in text else "normal",
                    "source": "auto_trigger",
                }
        return None

    def process_chat_input(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Analyzes text for triggers and returns a formatted Task payload if found.
        Supports free-form prefixes like 'core запусти декомпозицию...' as well
        as explicit command prefixes like 'PLAN:' or 'FIX:'.
        """
        is_core, remainder = self._detect_prefixed_core(text)
        if is_core:
            task_type, description = self._infer_core_task_type(remainder)
            if self._api is not None:
                self._api.log("info", f"[TRIGGER] Detected {task_type.value} trigger in core-prefixed message.")
            return {
                "type": task_type.value,
                "description": description or "Auto-triggered orchestration task",
                "priority": "high" if "!!!" in text else "normal",
                "source": "auto_trigger",
            }

        legacy = self._match_legacy_trigger(text)
        if legacy is not None and self._api is not None:
            self._api.log("info", f"[TRIGGER] Detected {legacy['type']} trigger in message.")
        return legacy

    def on_unload(self) -> None:
        pass

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        pass

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {"status": "active", "registered_patterns": len(self.TRIGGERS)}
