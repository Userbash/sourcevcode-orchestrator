import pytest
from unittest.mock import patch
from core.core.model_selector import ModelSelector
from core.core.mimo_bridge import MimoModel

def test_model_selector_syncs_with_mimo():
    selector = ModelSelector()
    assert not hasattr(selector, "mimo_models") or len(selector.mimo_models) == 0
    
    mock_mimo_models = [
        MimoModel(full_id="github-copilot/claude-haiku-4.5", id="claude-haiku-4.5", provider="github-copilot", status="active", context_window=200000)
    ]
    
    with patch('core.core.mimo_bridge.MimoBridge.get_models', return_value=mock_mimo_models):
        selector.sync_with_mimo()
        
    assert hasattr(selector, "mimo_models")
    assert len(selector.mimo_models) == 1
    assert selector.mimo_models[0].full_id == "github-copilot/claude-haiku-4.5"
