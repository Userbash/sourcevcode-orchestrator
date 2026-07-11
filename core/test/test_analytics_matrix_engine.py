from core.core.analytics_matrix_engine import AnalyticsKnowledgePool, AnalyticsMatrixEngine


class _StubGenerator:
    def generate_embedding_keywords(self, text: str) -> list[str]:
        if "analytics" in text.lower():
            return ["retrieval", "knowledge-pool"]
        return []

    def generate_text(self, *, prompt: str, context: dict | None = None) -> str:
        return f"generated::{len((context or {}).get('related_records', []))}"


def test_analytics_matrix_engine_extracts_keywords_templates_and_sentence_edges():
    engine = AnalyticsMatrixEngine(generator=_StubGenerator())
    report = engine.analyze(
        "Analytics agent builds analytics pipelines. "
        "Analytics pipelines connect agent memory.\n"
        "owner: data-science\n"
        "collect | metrics | quality"
    )

    assert "analytics" in report.keywords
    assert "retrieval" in report.keywords
    assert report.generated_text == "generated::0"
    assert report.templates[0].template_type == "key_value"
    assert any(item.template_type == "table_row" for item in report.templates)
    assert report.sentence_edges
    assert report.token_links["analytics"]


def test_knowledge_pool_retrieves_related_reports_by_keyword_overlap():
    engine = AnalyticsMatrixEngine()
    pool = AnalyticsKnowledgePool()

    first = engine.analyze("Analytics retrieval graph ranking for data platform.")
    second = engine.analyze("Provider routing policy for analytics ranking.")
    pool.ingest(first, source_id="r1")
    pool.ingest(second, source_id="r2")

    results = pool.query("Need analytics ranking retrieval", top_k=2)

    assert len(results) == 2
    assert results[0].source_id == "r1"


def test_engine_prompt_pool_includes_related_records():
    engine = AnalyticsMatrixEngine()
    pool = AnalyticsKnowledgePool()
    historical = engine.analyze("Analytics agent stores retrieval templates and ranking signals.")
    pool.ingest(historical, source_id="historical")

    report = engine.analyze("Need analytics retrieval templates", knowledge_pool=pool)

    assert report.prompt_pool["related_records"]
    assert report.prompt_pool["related_records"][0]["source_id"] == "historical"
