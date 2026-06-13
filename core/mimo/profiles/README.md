Model-class profiles for MIMO routing.

Each JSON file may define:
- `task_type`
- `provider_weights`
- `model_class_weights`
- `thresholds`
- `default_context_depth`
- `budget_pressure`
- `quality_pressure`

Supported profiles now include base task types and more granular variants such as:
- `plan_high`, `plan_critical`, `plan_research`
- `code_fast`, `code_senior`, `code_fix`, `code_refactor`
- `test_regression`
- `review_senior`, `review_security`
- `docs_light`, `docs_api`, `docs_release`
- `fix`, `fix_regression`
- `research`, `research_deep`, `research_compare`

The director resolves the most specific matching profile first, then falls back to the base task type.
