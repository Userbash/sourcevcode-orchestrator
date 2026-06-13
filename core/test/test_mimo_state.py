from core.mimo.state import MimoStateContext


def test_state_penalizes_repeated_failures():
    state = MimoStateContext()
    state.set_rate("model-a", 1.0)
    state.set_balance(100.0)

    score = 1.0
    for _ in range(6):
        score = state.update_score("model-a", is_successful=False, latency=1200.0)

    assert score < 0.4
    assert state.get_allowed_model("model-a", "medium", remaining_budget=10.0) == "gpt-4o"


def test_state_allows_model_when_budget_and_score_ok():
    state = MimoStateContext()
    state.set_rate("model-a", 1.0)
    state.set_balance(100.0)
    state.update_score("model-a", is_successful=True, latency=100.0)

    assert state.validate_context_limit("model-a", 1024)
    assert state.get_allowed_model("model-a", "low", remaining_budget=10.0) == "model-a"
