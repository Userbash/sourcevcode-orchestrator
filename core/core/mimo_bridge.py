from dataclasses import dataclass
from typing import List, Optional

from .mimo_provider import configured_native_mimo_models, normalize_mimo_model_name


@dataclass
class MimoModel:
    full_id: str
    id: str
    provider: str
    status: str
    context_window: Optional[int]
    capability_tags: list[str] | None = None
    cost_class: str | None = None
    readiness: bool | None = None
    blocked: bool = False


class MimoBridge:
    def get_models(self) -> List[MimoModel]:
        return [
            MimoModel(full_id=model, id=normalize_mimo_model_name(model), provider='xiaomi', status='active', context_window=None, capability_tags=['code', 'review', 'plan', 'test', 'docs', 'research'], cost_class='remote', readiness=True, blocked=False)
            for model in configured_native_mimo_models()
        ]

    def _parse_models_output(self, output: str) -> List[MimoModel]:
        return self.get_models()
