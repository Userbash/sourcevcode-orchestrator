# Analytics Matrix Agent Runbook

## Purpose

`analytics_matrix_engine.py` builds a reusable analytics report from raw text:
- character matrices
- token and phrase matrices
- sentence graphs
- table and template extraction
- retrieval-ready knowledge pool records
- generated narrative text through an optional model adapter

`data_analytics_matrix_agent.py` wraps this engine as a local agent and stores each report in a reusable in-memory knowledge pool.

## Processing flow

1. Normalize source text.
2. Extract tokens, keywords, phrases, and character matrices.
3. Build sentence-link graph and structured templates.
4. Retrieve similar knowledge records from the shared pool.
5. Generate final narrative text with either:
   - an external model adapter
   - deterministic fallback synthesis

## Matrix contract

The engine returns one serializable payload with:
- `keywords`
- `keyword_scores`
- `token_frequency`
- `phrase_frequency`
- `token_links`
- `sentence_nodes`
- `sentence_edges`
- `templates`
- `char_matrix`
- `generated_text`
- `prompt_pool`

## Multi-agent split

- `matrix_lead`
  - freezes interfaces and acceptance criteria
- `matrix_engine_owner`
  - owns extraction matrices
- `matrix_retrieval_owner`
  - owns knowledge-pool ingest and query
- `matrix_generation_owner`
  - owns LLM adapter and fallback generation
- `matrix_agent_owner`
  - owns agent packaging and result contract
- `matrix_test_owner`
  - owns focused deterministic tests
- `matrix_integrator`
  - verifies consistency and final handoff

## Example usage

```python
from core.core.analytics_matrix_engine import AnalyticsMatrixEngine, AnalyticsKnowledgePool

engine = AnalyticsMatrixEngine()
pool = AnalyticsKnowledgePool()

report = engine.analyze(
    "Build analytics matrices for search, retrieval, and generated summaries.",
    knowledge_pool=pool,
)
pool.ingest(report, source_id="task-1")
```

## Limits

- The knowledge pool is currently in-memory only.
- The agent is additive and not yet wired into `orchestrator.py`.
- External LLM acceleration is optional through the `TextGenerationAdapter` protocol.
