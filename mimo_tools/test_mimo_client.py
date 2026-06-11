import pytest
from unittest.mock import patch
from mimo_client import get_models

def test_get_models_calls_cli_and_parses():
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
        
        models = get_models()
        
        mock_sub.assert_called_once_with(["mimo", "models", "--verbose"])
        assert len(models) == 1
        assert models[0]["full_id"] == "github-copilot/claude-haiku-4.5"
