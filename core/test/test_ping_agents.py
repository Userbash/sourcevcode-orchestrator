import asyncio

from core.scripts import ping_agents


class _DoneStatus:
    value = "done"


class _Output:
    def as_dict(self):
        return {"summary": "ok"}


class _Result:
    status = _DoneStatus()
    output = _Output()
    confidence = 0.9
    provider = "local"
    model_name = "demo-model"
    errors = []


class _Agent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def execute(self, task):
        return _Result()


def test_probe_agent_returns_detailed_payload():
    agent_id, payload = asyncio.run(ping_agents._probe_agent(_Agent("agent-1")))

    assert agent_id == "agent-1"
    assert payload["status"] == "done"
    assert payload["provider"] == "local"
    assert payload["model_name"] == "demo-model"
