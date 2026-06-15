from __future__ import annotations

from .prompt_contracts import NormalizedPrompt
from ..input_text_normalizer import normalize_text
from ..input_text_quantizer import quantize_input_text


def normalize(raw_user_input: str) -> NormalizedPrompt:
    cleaned_text = normalize_text(raw_user_input, max_chars=6000)
    profile = quantize_input_text(cleaned_text=cleaned_text)
    return NormalizedPrompt(
        original_text=raw_user_input,
        cleaned_text=cleaned_text,
        user_intent=str(profile.get("intent_bucket") or "general"),
        task_type=str(profile.get("intent_bucket") or "code"),
        constraints=[],
        required_agents=[],
        required_tools=[],
        output_format="text",
        risk_level=str(profile.get("risk_bucket") or "low"),
        memory_references=[f"profile:{profile.get('execution_shape', 'single_lane')}"] if cleaned_text else [],
        context_requirements=[
            f"quality:{profile.get('input_quality_bucket', 'clean')}",
            f"trust:{profile.get('decision_trust', 'rough_hint')}",
        ],
    )
