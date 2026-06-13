import pytest
from unittest.mock import patch
from core.core.mimo_bridge import MimoBridge, MimoModel

def test_mimo_bridge_get_models_empty():
    bridge = MimoBridge()
    with patch('subprocess.check_output') as mock_sub:
        mock_sub.return_value = b""
        models = bridge.get_models()
        assert models == []

def test_mimo_bridge_get_models_success():
    bridge = MimoBridge()
    sample_output = """github-copilot/claude-haiku-4.5
{
  "id": "claude-haiku-4.5",
  "providerID": "github-copilot",
  "name": "Claude Haiku 4.5",
  "status": "active",
  "limit": {
    "context": 200000,
    "input": 136000,
    "output": 64000
  }
}"""
    with patch('subprocess.check_output') as mock_sub:
        mock_sub.return_value = sample_output.encode('utf-8')
        models = bridge.get_models()
        
        mock_sub.assert_called_once_with(["mimo", "models", "--verbose"])
        assert len(models) == 1
        assert isinstance(models[0], MimoModel)
        assert models[0].full_id == "github-copilot/claude-haiku-4.5"
        assert models[0].context_window == 200000


def test_mimo_bridge_parses_inventory_metadata():
    bridge = MimoBridge()
    sample_output = """local/qwen-2.5-7b-instruct
{
  "id": "qwen-2.5-7b-instruct",
  "providerID": "local",
  "status": "active",
  "ready": true,
  "blocked": false,
  "limit": {
    "context": 131072
  },
  "capabilities": ["docs", "research", "review"],
  "costClass": "low"
}"""
    with patch('subprocess.check_output') as mock_sub:
        mock_sub.return_value = sample_output.encode('utf-8')
        models = bridge.get_models()

    assert len(models) == 1
    assert models[0].provider == "local"
    assert models[0].capability_tags == ["docs", "research", "review"]
    assert models[0].cost_class == "low"
    assert models[0].readiness is True
    assert models[0].blocked is False
