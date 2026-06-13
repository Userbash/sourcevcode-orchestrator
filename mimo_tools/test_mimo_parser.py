import pytest
from mimo_parser import parse_mimo_models_output

def test_parse_mimo_models_empty():
    assert parse_mimo_models_output("") == []

def test_parse_mimo_models_single():
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
    parsed = parse_mimo_models_output(sample_output)
    assert len(parsed) == 1
    assert parsed[0]["full_id"] == "github-copilot/claude-haiku-4.5"
    assert parsed[0]["id"] == "claude-haiku-4.5"
    assert parsed[0]["provider"] == "github-copilot"
    assert parsed[0]["status"] == "active"
    assert parsed[0]["context_window"] == 200000
