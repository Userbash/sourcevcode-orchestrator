package memory

import (
	"context"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type MemoryClass string

const (
	MemoryClassSession   MemoryClass = "session"
	MemoryClassRoute     MemoryClass = "route"
	MemoryClassKnowledge MemoryClass = "knowledge"
)

type RerankerPolicy struct {
	Strategy         string  `json:"strategy"`
	SemanticWeight   float64 `json:"semantic_weight"`
	LexicalWeight    float64 `json:"lexical_weight"`
	ImportanceWeight float64 `json:"importance_weight"`
	RecencyWeight    float64 `json:"recency_weight"`
	ScopeWeight      float64 `json:"scope_weight"`
	TrustWeight      float64 `json:"trust_weight"`
	FreshnessWeight  float64 `json:"freshness_weight"`
	KeywordCap       float64 `json:"keyword_cap"`
	MinScore         float64 `json:"min_score"`
	Diversity        bool    `json:"diversity"`
}

type RetrievalUsage struct {
	BudgetTokens      int     `json:"budget_tokens"`
	UsedTokens        int     `json:"used_tokens"`
	TruncatedCount    int     `json:"truncated_count"`
	PackedCount       int     `json:"packed_count"`
	CandidateCount    int     `json:"candidate_count"`
	RetrievedTokens   int     `json:"retrieved_tokens"`
	CoverageRatio     float64 `json:"coverage_ratio"`
	TruncationRatio   float64 `json:"truncation_ratio"`
	ApproxLatencyHint string  `json:"approx_latency_hint"`
}

type RetrievalPolicy struct {
	Strategy          string         `json:"strategy"`
	TopK              int            `json:"top_k"`
	CandidateLimit    int            `json:"candidate_limit"`
	BudgetTokens      int            `json:"budget_tokens"`
	QueryTerms        []string       `json:"query_terms"`
	PathTerms         []string       `json:"path_terms"`
	UseGlobalFallback bool           `json:"use_global_fallback"`
	MemoryScope       string         `json:"memory_scope"`
	TaskType          string         `json:"task_type"`
	Reranker          RerankerPolicy `json:"reranker"`
}

type RetrievalKPI struct {
	Tier              string  `json:"tier"`
	CandidateCount    int     `json:"candidate_count"`
	PackedCount       int     `json:"packed_count"`
	BestKeywordHits   int     `json:"best_keyword_hits"`
	BestScore         float64 `json:"best_score"`
	BestTermOverlap   float64 `json:"best_term_overlap"`
	CoverageRatio     float64 `json:"coverage_ratio"`
	TruncationRatio   float64 `json:"truncation_ratio"`
	ApproxLatencyHint string  `json:"approx_latency_hint"`
}

type RetrievalSnapshot struct {
	Policy  RetrievalPolicy
	Usage   RetrievalUsage
	KPI     RetrievalKPI
	Results []domain.VectorSearchResult
	Packed  []domain.VectorSearchResult
}

type MemoryDomains struct {
	Session   map[string]any
	Route     map[string]any
	Knowledge map[string]any
}

type RetrieverAgent interface {
	Retrieve(context.Context, domain.Task, int) (RetrievalSnapshot, error)
}

type RouteMemoryAgent interface {
	SearchRouteMemories(context.Context, domain.Task, int) ([]domain.RouteSearchResult, error)
	RouteHistoryScore(context.Context, domain.Task, domain.AgentInfo) (float64, domain.RouteScoreBreakdown, error)
}
