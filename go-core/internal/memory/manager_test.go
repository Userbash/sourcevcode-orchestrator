package memory

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestManagerBuildRuntimeContext(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	_, err = store.SaveSessionState(ctx, "session-1", "feature", map[string]any{
		"session_note": "carry",
		"budget":       3,
	}, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}

	runtime := manager.BuildRuntimeContext(ctx, domain.Task{
		ID:          "task-1",
		SessionID:   "session-1",
		MemoryScope: "session",
		MemoryKeys:  []string{"session_note"},
		CachePolicy: "read_write",
		Context: domain.TaskContext{
			Branch: "feature",
		},
	}, "agent-1", "openai", "gpt-5.5")

	sessionState, ok := runtime["session_state"].(map[string]any)
	if !ok {
		t.Fatalf("session_state missing or wrong type: %T", runtime["session_state"])
	}
	if sessionState["session_id"] != "session-1" {
		t.Fatalf("session_state.session_id = %v, want session-1", sessionState["session_id"])
	}

	memorySlice, ok := runtime["memory"].(map[string]any)
	if !ok {
		t.Fatalf("memory missing or wrong type: %T", runtime["memory"])
	}
	if memorySlice["session_note"] != "carry" {
		t.Fatalf("memory.session_note = %v, want carry", memorySlice["session_note"])
	}
	if _, exists := memorySlice["budget"]; exists {
		t.Fatalf("memory.budget should be filtered out when memory_keys are provided")
	}

	validation, ok := runtime["validation_context"].(map[string]any)
	if !ok {
		t.Fatalf("validation_context missing or wrong type: %T", runtime["validation_context"])
	}
	if blocked, _ := validation["blocked"].(bool); blocked {
		t.Fatal("validation_context.blocked = true, want false")
	}
}

func TestManagerCacheGuardSnapshotBlockedAfterHardStop(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()
	task := domain.Task{
		ID:        "task-2",
		SessionID: "session-2",
		Context: domain.TaskContext{
			Branch: "main",
		},
	}

	if err := manager.RecordHardStop(ctx, task, "agent_execute", "provider failure"); err != nil {
		t.Fatalf("RecordHardStop() error = %v", err)
	}

	snapshot := manager.CacheGuardSnapshot(ctx, "session-2", "main")
	if blocked, _ := snapshot["blocked"].(bool); !blocked {
		t.Fatal("CacheGuardSnapshot().blocked = false, want true")
	}
	if hardStop, _ := snapshot["hard_stop"].(bool); !hardStop {
		t.Fatal("CacheGuardSnapshot().hard_stop = false, want true")
	}
}

func TestManagerEvaluateModelBudgetAndRecordUsage(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()
	_, err = store.SaveSessionState(ctx, "session-3", "main", map[string]any{
		"model_usage": map[string]any{
			"gpt-4": 499900,
		},
	}, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}

	task := domain.Task{
		ID:        "task-3",
		SessionID: "session-3",
		Type:      domain.TaskTypeCode,
		Context: domain.TaskContext{
			Branch: "main",
		},
	}
	budget := manager.EvaluateModelBudget(ctx, task, "gpt-4", 80)
	if action := budget["action"]; action != "error" {
		t.Fatalf("EvaluateModelBudget().action = %v, want error", action)
	}

	err = manager.RecordModelUsage(ctx, task, "gpt-4", domain.AgentResult{
		Output: domain.ResultOutput{
			Artifacts: map[string]any{
				"usage": map[string]any{
					"total_tokens": 21,
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("RecordModelUsage() error = %v", err)
	}

	loaded, ok, err := store.GetSessionState(ctx, "session-3", "main")
	if err != nil {
		t.Fatalf("GetSessionState() error = %v", err)
	}
	if !ok {
		t.Fatal("GetSessionState() ok = false, want true")
	}
	usage, ok := loaded.State["model_usage"].(map[string]any)
	if !ok {
		t.Fatalf("model_usage type = %T, want map[string]any", loaded.State["model_usage"])
	}
	if got := usage["gpt-4"]; got != 499921 {
		t.Fatalf("model_usage[gpt-4] = %v, want 499921", got)
	}
}

func TestManagerLoadMemoryContextSkipsThoughtKeysAndFlattensValidation(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	_, err = store.SaveSessionState(ctx, "session-ctx", "main", map[string]any{
		"session_note":     "remember this",
		"scratch_thought":  "do not expose",
		"context_priority": "high",
	}, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}

	loaded := manager.LoadMemoryContext(ctx, domain.Task{
		ID:          "task-ctx",
		SessionID:   "session-ctx",
		MemoryScope: "session",
		MemoryKeys:  []string{"session_note", "scratch_thought"},
		CachePolicy: "read_write",
		Context: domain.TaskContext{
			Branch: "main",
		},
	}, "agent-ctx", "openai", "gpt-5.5")

	if loaded["session_note"] != "remember this" {
		t.Fatalf("session_note = %v, want remember this", loaded["session_note"])
	}
	if _, exists := loaded["scratch_thought"]; exists {
		t.Fatalf("scratch_thought should not be exposed in loaded context: %#v", loaded)
	}
	if loaded["memory_identifier"] != "session-ctx" {
		t.Fatalf("memory_identifier = %v, want session-ctx", loaded["memory_identifier"])
	}
	if disabled, _ := loaded["trained_memory_disabled_for_risk"].(bool); disabled {
		t.Fatal("trained_memory_disabled_for_risk = true, want false")
	}
	if _, ok := loaded["cache_guard_snapshot"].(map[string]any); !ok {
		t.Fatalf("cache_guard_snapshot missing or wrong type: %T", loaded["cache_guard_snapshot"])
	}
}

func TestManagerIngestTextAndSearchVectorContext(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	_, err = manager.IngestText(ctx, "session-rag", "main", "chat_prompt", "prompt-1", "Implement FAISS-style vector retrieval for login and JWT middleware with chunked context memory.", map[string]any{"project": "demo"})
	if err != nil {
		t.Fatalf("IngestText() error = %v", err)
	}
	_, err = manager.IngestText(ctx, "session-rag", "main", "task_exchange", "task-2", "We already used JWT middleware and safe phased rollout for auth refactor.", map[string]any{"project": "demo"})
	if err != nil {
		t.Fatalf("IngestText() error = %v", err)
	}

	results, err := manager.SearchVectorContext(ctx, domain.Task{
		ID:        "task-search",
		SessionID: "session-rag",
		Type:      domain.TaskTypeCode,
		Input: domain.TaskInput{
			Description: "Add JWT login vector search and chunk memory",
		},
		Context: domain.TaskContext{Project: "demo", Branch: "main"},
	}, 3)
	if err != nil {
		t.Fatalf("SearchVectorContext() error = %v", err)
	}
	if len(results) == 0 {
		t.Fatal("SearchVectorContext() returned no results")
	}
	if results[0].Score <= 0 {
		t.Fatalf("top result score = %v, want > 0", results[0].Score)
	}
}

func TestManagerLoadMemoryContextBuildsAugmentedPromptFromVectorMemory(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	_, err = manager.IngestText(ctx, "session-aug", "main", "task_exchange", "task-prev", "Use chunk overlap, vector embeddings, and hybrid cosine plus lexical search for prompt retrieval.", map[string]any{"project": "demo"})
	if err != nil {
		t.Fatalf("IngestText() error = %v", err)
	}

	loaded := manager.LoadMemoryContext(ctx, domain.Task{
		ID:          "task-aug",
		SessionID:   "session-aug",
		Type:        domain.TaskTypeCode,
		MemoryScope: "session",
		CachePolicy: "read_write",
		Input: domain.TaskInput{
			Description: "Build vector memory retrieval and augmented prompt",
			Files:       []string{"internal/memory/manager.go"},
		},
		Context: domain.TaskContext{Project: "demo", Branch: "main"},
	}, "agent-rag", "local", "qwen")

	brief, _ := loaded["vector_memory_brief"].(string)
	if brief == "" {
		t.Fatal("vector_memory_brief empty, want retrieved context")
	}
	augmented, _ := loaded["augmented_prompt"].(string)
	if !strings.Contains(augmented, "[CONTEXT]") {
		t.Fatalf("augmented_prompt = %q, want [CONTEXT] block", augmented)
	}
	if count, _ := loaded["vector_memory_count"].(int); count == 0 {
		t.Fatalf("vector_memory_count = %v, want > 0", loaded["vector_memory_count"])
	}
}

func TestPrepareEmbeddingInputsUsesAsymmetricPrefixes(t *testing.T) {
	document := prepareDocumentEmbeddingInput("internal/auth/login.go", "JWT login flow with refresh token rotation")
	if !strings.HasPrefix(document, "search_document: internal/auth/login.go\n") {
		t.Fatalf("document prefix = %q, want search_document with file path", document)
	}

	query := prepareQueryEmbeddingInput("find jwt refresh token flow")
	if query != "search_query: find jwt refresh token flow" {
		t.Fatalf("query prefix = %q, want prefixed query", query)
	}
}

func TestRankVectorChunksPrefersHybridFusedRelevance(t *testing.T) {
	manager := &Manager{}
	task := domain.Task{
		ID:        "task-hybrid",
		SessionID: "session-hybrid",
		Type:      domain.TaskTypeCode,
		Input: domain.TaskInput{
			Description: "Implement JWT login with refresh token support",
		},
		Context: domain.TaskContext{
			Branch:  "main",
			Project: "demo",
		},
	}

	queryText := manager.queryText(task)
	query := domain.VectorSearchQuery{
		SessionID: "session-hybrid",
		Branch:    "main",
		Text:      queryText,
		Terms:     uniqueTerms(queryText, 40),
		Embedding: []float64{1, 0},
	}
	now := time.Now().UTC()
	chunks := []domain.VectorChunk{
		{
			ChunkID:    "strong-hybrid",
			SessionID:  "session-hybrid",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "auth-doc",
			Text:       "JWT login refresh token flow with rollout notes",
			Terms:      uniqueTerms("JWT login refresh token flow with rollout notes", 32),
			Embedding:  []float64{0.95, 0.05},
			Metadata:   map[string]any{"project": "demo", "scope": "session", "importance": 0.9},
			CreatedAt:  now,
			ChunkIndex: 0,
		},
		{
			ChunkID:    "lexical-only",
			SessionID:  "session-hybrid",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "notes-doc",
			Text:       "JWT login refresh token checklist",
			Terms:      uniqueTerms("JWT login refresh token checklist", 32),
			Embedding:  []float64{0.2, 0.8},
			Metadata:   map[string]any{"project": "demo", "scope": "session"},
			CreatedAt:  now.Add(-1 * time.Hour),
			ChunkIndex: 1,
		},
		{
			ChunkID:    "semantic-only",
			SessionID:  "session-hybrid",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "infra-doc",
			Text:       "queue worker throughput and caching",
			Terms:      uniqueTerms("queue worker throughput and caching", 32),
			Embedding:  []float64{0.93, 0.07},
			Metadata:   map[string]any{"project": "demo", "scope": "session"},
			CreatedAt:  now.Add(-2 * time.Hour),
			ChunkIndex: 2,
		},
	}

	results := manager.rankVectorChunks(task, query, chunks, buildRetrievalPolicy(task, 3, query.Text))
	if len(results) != 3 {
		t.Fatalf("len(results) = %d, want 3", len(results))
	}
	if results[0].Chunk.ChunkID != "strong-hybrid" {
		t.Fatalf("top chunk = %q, want strong-hybrid", results[0].Chunk.ChunkID)
	}
	if results[0].Score <= results[1].Score {
		t.Fatalf("top score = %v, want > second score %v", results[0].Score, results[1].Score)
	}
}

func TestRankVectorChunksPreservesSourceDiversity(t *testing.T) {
	manager := &Manager{}
	task := domain.Task{
		ID:        "task-diversity",
		SessionID: "session-diversity",
		Type:      domain.TaskTypeCode,
		Input: domain.TaskInput{
			Description: "Implement JWT auth middleware and refresh token validation",
		},
		Context: domain.TaskContext{
			Branch:  "main",
			Project: "demo",
		},
	}

	queryText := manager.queryText(task)
	query := domain.VectorSearchQuery{
		SessionID: "session-diversity",
		Branch:    "main",
		Text:      queryText,
		Terms:     uniqueTerms(queryText, 40),
		Embedding: []float64{1, 0},
	}
	now := time.Now().UTC()
	chunks := []domain.VectorChunk{
		{
			ChunkID:    "auth-0",
			SessionID:  "session-diversity",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "auth-doc",
			Text:       "JWT auth middleware validates refresh token rotation and session cookies",
			Terms:      uniqueTerms("JWT auth middleware validates refresh token rotation and session cookies", 32),
			Embedding:  []float64{0.98, 0.02},
			Metadata:   map[string]any{"project": "demo", "scope": "session", "importance": 0.8},
			CreatedAt:  now,
			ChunkIndex: 0,
		},
		{
			ChunkID:    "auth-1",
			SessionID:  "session-diversity",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "auth-doc",
			Text:       "JWT auth middleware rollout checklist for refresh token cache invalidation",
			Terms:      uniqueTerms("JWT auth middleware rollout checklist for refresh token cache invalidation", 32),
			Embedding:  []float64{0.96, 0.04},
			Metadata:   map[string]any{"project": "demo", "scope": "session", "importance": 0.7},
			CreatedAt:  now.Add(-3 * time.Minute),
			ChunkIndex: 1,
		},
		{
			ChunkID:    "api-0",
			SessionID:  "session-diversity",
			Branch:     "main",
			Source:     "rag_document",
			SourceID:   "api-doc",
			Text:       "Refresh token API contract for JWT auth middleware and session renewal",
			Terms:      uniqueTerms("Refresh token API contract for JWT auth middleware and session renewal", 32),
			Embedding:  []float64{0.93, 0.07},
			Metadata:   map[string]any{"project": "demo", "scope": "session", "importance": 0.75},
			CreatedAt:  now.Add(-10 * time.Minute),
			ChunkIndex: 0,
		},
	}

	results := manager.rankVectorChunks(task, query, chunks, buildRetrievalPolicy(task, 2, query.Text))
	if len(results) != 2 {
		t.Fatalf("len(results) = %d, want 2", len(results))
	}
	if results[0].Chunk.SourceID != "auth-doc" {
		t.Fatalf("top source_id = %q, want auth-doc", results[0].Chunk.SourceID)
	}
	if results[1].Chunk.SourceID != "api-doc" {
		t.Fatalf("second source_id = %q, want api-doc for diversity", results[1].Chunk.SourceID)
	}
}

func TestRankVectorChunksMatchesCodeIdentifiersLexically(t *testing.T) {
	manager := &Manager{}
	task := domain.Task{
		ID:        "task-identifiers",
		SessionID: "session-identifiers",
		Type:      domain.TaskTypeCode,
		Input: domain.TaskInput{
			Description: "Fix jwt auth middleware refresh token validation",
		},
		Context: domain.TaskContext{
			Branch:  "main",
			Project: "demo",
		},
	}

	queryText := manager.queryText(task)
	query := domain.VectorSearchQuery{
		SessionID: "session-identifiers",
		Branch:    "main",
		Text:      queryText,
		Terms:     uniqueTerms(queryText, 40),
		Embedding: []float64{0.7, 0.3},
	}
	now := time.Now().UTC()
	chunks := []domain.VectorChunk{
		{
			ChunkID:    "code-path",
			SessionID:  "session-identifiers",
			Branch:     "main",
			Source:     "code_chunk",
			SourceID:   "auth.go",
			Text:       "func jwt_auth_middleware() { validate_refresh_token() }",
			Terms:      uniqueTerms("func jwt_auth_middleware() { validate_refresh_token() }", 32),
			Embedding:  []float64{0.71, 0.29},
			Metadata:   map[string]any{"project": "demo", "scope": "session"},
			CreatedAt:  now,
			ChunkIndex: 0,
		},
		{
			ChunkID:    "generic",
			SessionID:  "session-identifiers",
			Branch:     "main",
			Source:     "code_chunk",
			SourceID:   "worker.go",
			Text:       "worker pipeline and queue retries",
			Terms:      uniqueTerms("worker pipeline and queue retries", 32),
			Embedding:  []float64{0.69, 0.31},
			Metadata:   map[string]any{"project": "demo", "scope": "session"},
			CreatedAt:  now.Add(-2 * time.Minute),
			ChunkIndex: 1,
		},
	}

	results := manager.rankVectorChunks(task, query, chunks, buildRetrievalPolicy(task, 2, query.Text))
	if len(results) == 0 {
		t.Fatal("len(results) = 0, want lexical match for code identifier")
	}
	if results[0].Chunk.ChunkID != "code-path" {
		t.Fatalf("top chunk = %q, want code-path", results[0].Chunk.ChunkID)
	}
	if results[0].KeywordHits == 0 {
		t.Fatalf("keyword_hits = %d, want > 0 after identifier normalization", results[0].KeywordHits)
	}
}

func TestPackVectorResultsHonorsTokenBudget(t *testing.T) {
	results := []domain.VectorSearchResult{
		{
			Chunk: domain.VectorChunk{ChunkID: "1", Source: "rag_document", ChunkIndex: 0, Text: strings.Repeat("alpha ", 32)},
			Score: 0.91,
		},
		{
			Chunk: domain.VectorChunk{ChunkID: "2", Source: "rag_document", ChunkIndex: 1, Text: strings.Repeat("beta ", 28)},
			Score: 0.83,
		},
		{
			Chunk: domain.VectorChunk{ChunkID: "3", Source: "rag_document", ChunkIndex: 2, Text: strings.Repeat("gamma ", 18)},
			Score: 0.79,
		},
	}

	packed, usage := packVectorResults(results, 40)
	if len(packed) != 1 {
		t.Fatalf("len(packed) = %d, want 1", len(packed))
	}
	if usage.UsedTokens > usage.BudgetTokens {
		t.Fatalf("used_tokens = %d, want <= budget_tokens = %d", usage.UsedTokens, usage.BudgetTokens)
	}
	if usage.TruncatedCount != 2 {
		t.Fatalf("truncated_count = %d, want 2", usage.TruncatedCount)
	}
}

func TestRetrievalTokenBudgetScalesWithComplexity(t *testing.T) {
	base := retrievalTokenBudget(domain.Task{Type: domain.TaskTypeCode, Complexity: domain.ComplexityLow, Input: domain.TaskInput{Description: "small fix"}})
	high := retrievalTokenBudget(domain.Task{Type: domain.TaskTypeCode, Complexity: domain.ComplexityCritical, Input: domain.TaskInput{Description: strings.Repeat("complex retrieval task ", 12), Files: []string{"internal/memory/manager.go", "internal/kernel/orchestrator.go"}, AcceptanceCriteria: []string{"preserve runtime behavior", "pack retrieval provenance"}}})
	if high <= base {
		t.Fatalf("critical retrieval budget = %d, want > base budget %d", high, base)
	}
}

func TestManagerLoadMemoryContextIncludesRetrievalMetadata(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	_, err = manager.IngestText(ctx, "session-meta", "main", "rag_document", "doc-1", "Hybrid retrieval combines vector search, lexical matching, provenance tracking, and source trust.", map[string]any{"project": "demo", "scope": "session", "importance": 0.9, "confidence": 0.8})
	if err != nil {
		t.Fatalf("IngestText() error = %v", err)
	}

	loaded := manager.LoadMemoryContext(ctx, domain.Task{
		ID:          "task-meta",
		SessionID:   "session-meta",
		Type:        domain.TaskTypeResearch,
		Complexity:  domain.ComplexityHigh,
		MemoryScope: "session",
		CachePolicy: "read_write",
		Input: domain.TaskInput{
			Description: "Add provenance-aware hybrid retrieval",
			Files:       []string{"internal/memory/manager.go"},
		},
		Context: domain.TaskContext{Project: "demo", Branch: "main"},
	}, "agent-meta", "local", "qwen")

	if loaded["retrieval_strategy"] == "" {
		t.Fatalf("retrieval_strategy missing: %#v", loaded)
	}
	policy, ok := loaded["retrieval_policy"].(map[string]any)
	if !ok || len(policy) == 0 {
		t.Fatalf("retrieval_policy missing or empty: %T %#v", loaded["retrieval_policy"], loaded["retrieval_policy"])
	}
	prov, ok := loaded["retrieval_provenance"].([]map[string]any)
	if !ok || len(prov) == 0 {
		t.Fatalf("retrieval_provenance missing or empty: %T %#v", loaded["retrieval_provenance"], loaded["retrieval_provenance"])
	}
	quality, ok := loaded["retrieval_quality"].(map[string]any)
	if !ok || quality["tier"] == "" {
		t.Fatalf("retrieval_quality missing or invalid: %T %#v", loaded["retrieval_quality"], loaded["retrieval_quality"])
	}
	guidance, ok := loaded["prompt_guidance"].([]string)
	if !ok || len(guidance) == 0 {
		t.Fatalf("prompt_guidance missing or empty: %T %#v", loaded["prompt_guidance"], loaded["prompt_guidance"])
	}
}

func TestPackVectorResultsTracksCoverageAndLatencyHint(t *testing.T) {
	results := []domain.VectorSearchResult{
		{Chunk: domain.VectorChunk{ChunkID: "1", Source: "rag_document", ChunkIndex: 0, Text: strings.Repeat("alpha ", 32)}, Score: 0.91},
		{Chunk: domain.VectorChunk{ChunkID: "2", Source: "rag_document", ChunkIndex: 1, Text: strings.Repeat("beta ", 28)}, Score: 0.83},
		{Chunk: domain.VectorChunk{ChunkID: "3", Source: "rag_document", ChunkIndex: 2, Text: strings.Repeat("gamma ", 18)}, Score: 0.79},
	}

	packed, usage := packVectorResults(results, 72)
	if len(packed) == 0 {
		t.Fatal("len(packed) = 0, want at least one packed result")
	}
	if usage.PackedCount != len(packed) {
		t.Fatalf("packed_count = %d, want %d", usage.PackedCount, len(packed))
	}
	if usage.CandidateCount != len(results) {
		t.Fatalf("candidate_count = %d, want %d", usage.CandidateCount, len(results))
	}
	if usage.CoverageRatio <= 0 {
		t.Fatalf("coverage_ratio = %v, want > 0", usage.CoverageRatio)
	}
	if usage.ApproxLatencyHint == "" {
		t.Fatal("approx_latency_hint empty, want populated hint")
	}
}

func TestManagerRetrieveBuildsKPIAndMemoryDomains(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()
	if err := store.UpsertVectorChunks(ctx, []domain.VectorChunk{{
		ChunkID:   "kb-1",
		Source:    "docs/spec.md",
		Text:      "retrieval pipeline for research and routing with model selection evidence",
		Embedding: []float64{0.91, 0.12, 0.07},
		Metadata:  map[string]any{"importance": 0.8, "trust": 0.9},
		CreatedAt: time.Now().UTC(),
	}}); err != nil {
		t.Fatalf("put vector chunk: %v", err)
	}
	task := domain.Task{
		ID:        "task-retrieve",
		SessionID: "session-retrieve",
		Type:      domain.TaskTypeResearch,
		Priority:  domain.PriorityHigh,
		Input:     domain.TaskInput{Description: "Find retrieval evidence for model routing"},
	}
	snapshot, err := manager.Retrieve(ctx, task, 4)
	if err != nil {
		t.Fatalf("retrieve: %v", err)
	}
	if snapshot.Policy.Reranker.Strategy == "" {
		t.Fatal("expected reranker strategy")
	}
	if snapshot.KPI.Tier == "" {
		t.Fatalf("expected retrieval kpi tier, got %+v", snapshot.KPI)
	}
	if snapshot.Policy.TopK < 4 {
		t.Fatalf("expected retrieval policy topk to respect requested minimum, got %+v", snapshot.Policy)
	}
	loaded := manager.LoadMemoryContext(ctx, task, "agent-a", "openai", "gpt-test")
	if _, ok := loaded["session_memory"].(map[string]any); !ok {
		t.Fatalf("expected session_memory map, got %#v", loaded["session_memory"])
	}
	if _, ok := loaded["route_memory"].(map[string]any); !ok {
		t.Fatalf("expected route_memory map, got %#v", loaded["route_memory"])
	}
	knowledge, ok := loaded["knowledge_memory"].(map[string]any)
	if !ok {
		t.Fatalf("expected knowledge_memory map, got %#v", loaded["knowledge_memory"])
	}
	if _, ok := knowledge["retrieval_kpi"].(map[string]any); !ok {
		t.Fatalf("expected retrieval_kpi payload, got %#v", knowledge["retrieval_kpi"])
	}
}
