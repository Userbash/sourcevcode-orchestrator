from core.core.model_replacement_policy import ModelReplacementPolicy


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value


class _Input:
    def __init__(self, description: str) -> None:
        self.description = description
        self.constraints = []


class _Task:
    def __init__(self, task_type: str = 'code', complexity: str = 'medium', priority: str = 'normal', description: str = 'replace failed model') -> None:
        self.type = _Value(task_type)
        self.complexity = _Value(complexity)
        self.priority = _Value(priority)
        self.input = _Input(description)
        self.assigned_model = None


PARTICIPATION = {
    'active_now': [
        {'provider': 'local', 'model_name': 'qwen2.5:32b-instruct-q4_k_m', 'source': 'registered_agent'},
        {'provider': 'mistral', 'model_name': 'codestral-latest', 'source': 'registered_agent'},
        {'provider': 'mistral', 'model_name': 'mistral-large-latest', 'source': 'registered_agent'},
        {'provider': 'openai', 'model_name': 'gpt-5.4', 'source': 'registered_agent'},
        {'provider': 'openai', 'model_name': 'gpt-5.5', 'source': 'registered_agent'},
    ],
    'available_but_not_wired_directly': [
        {'provider': 'openai', 'model_name': 'gpt-5.4-mini', 'source': 'direct_ping'},
        {'provider': 'mistral', 'model_name': 'mistral-medium-latest', 'source': 'direct_ping'},
        {'provider': 'local', 'model_name': 'qwen-2.5-7b-instruct', 'source': 'direct_ping'},
    ],
    'present_but_unusable': [
        {'provider': 'local', 'model_name': 'qwen-2.5-7b-instruct', 'source': 'direct_ping', 'reason': 'local_model_no_response'},
        {'provider': 'mistral', 'model_name': 'voxtral-mini-2602', 'source': 'direct_ping', 'reason': 'invalid_model'},
        {'provider': 'github-copilot', 'model_name': 'github-copilot/gpt-5.2', 'source': 'mimo_ping', 'reason': 'github_pat_not_supported'},
    ],
}


def test_local_model_needs_replacement_after_three_failures():
    policy = ModelReplacementPolicy()
    for _ in range(3):
        decision = policy.register_failure('local', 'qwen-2.5-7b-instruct', 'local_model_no_response')

    assert decision['replacement_due'] is True
    assert decision['hide_from_catalog'] is False
    assert decision['consecutive_failures'] == 3


def test_local_model_hides_after_five_failures():
    policy = ModelReplacementPolicy()
    for _ in range(5):
        decision = policy.register_failure('local', 'qwen-2.5-7b-instruct', 'local_model_no_response')

    assert decision['replacement_due'] is True
    assert decision['hide_from_catalog'] is True
    assert decision['hard_excluded'] is True


def test_cloud_invalid_model_is_hard_excluded_immediately():
    policy = ModelReplacementPolicy()
    decision = policy.register_failure('mistral', 'voxtral-mini-2602', 'invalid_model')

    assert decision['replacement_due'] is True
    assert decision['hide_from_catalog'] is True
    assert decision['hard_excluded'] is True


def test_policy_recommends_local_then_cloud_replacement_for_unavailable_local_model():
    policy = ModelReplacementPolicy()
    for _ in range(3):
        policy.register_failure('local', 'qwen-2.5-7b-instruct', 'local_model_no_response')

    replacement = policy.recommend_replacement(_Task(task_type='code'), 'local', 'qwen-2.5-7b-instruct', PARTICIPATION, failure_reason='local_model_no_response')

    assert replacement is not None
    assert replacement['provider'] == 'local'
    assert replacement['model_name'] == 'qwen2.5:32b-instruct-q4_k_m'


def test_policy_recommends_openai_or_mistral_for_github_copilot_pat_failure():
    policy = ModelReplacementPolicy()

    replacement = policy.recommend_replacement(_Task(task_type='review', priority='high', description='security review'), 'github-copilot', 'github-copilot/gpt-5.2', PARTICIPATION, failure_reason='github_pat_not_supported')

    assert replacement is not None
    assert replacement['provider'] in {'openai', 'mistral'}
    assert replacement['model_name'] in {'gpt-5.4', 'gpt-5.5', 'codestral-latest', 'mistral-large-latest'}


def test_policy_snapshot_contains_replacement_for_unusable_models():
    policy = ModelReplacementPolicy()
    for _ in range(3):
        policy.register_failure('local', 'qwen-2.5-7b-instruct', 'local_model_no_response')

    snapshot = policy.build_snapshot(PARTICIPATION)
    unavailable = next(item for item in snapshot['present_but_unusable'] if item['model_name'] == 'qwen-2.5-7b-instruct')

    assert unavailable['replacement']['model_name'] == 'qwen2.5:32b-instruct-q4_k_m'
