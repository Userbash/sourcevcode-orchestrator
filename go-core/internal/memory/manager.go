package memory

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

const (
	hardStopReason              = "CACHE_GUARD_HARD_STOP"
	modelUsageStateKey          = "model_usage"
	defaultModelTokenLimit      = 1000000
	defaultWarnBelowPercentage  = 20.0
	defaultReduceBelowPercent   = 10.0
	defaultErrorBelowPercent    = 5.0
	defaultVectorDims           = 64
	defaultChunkWordLimit       = 120
	defaultChunkOverlap         = 24
	defaultVectorTopK           = 4
	defaultVectorCandidateCap   = 256
	defaultRetrievalTokenBudget = 192
	defaultChunkTokenOverhead   = 8
	defaultRRFK                 = 60.0
)

var defaultModelTokenLimits = map[string]int{
	"gpt-4":                500000,
	"gpt-coding-large":     800000,
	"gemini-1.5-pro":       1000000,
	"mistral-large-latest": 1500000,
}

var errEmbeddingsNotConfigured = errors.New("embeddings provider is not configured")

type Manager struct {
	store      state.Store
	embeddings *embeddingClient
}

type retrievalUsage struct {
	BudgetTokens   int
	UsedTokens     int
	TruncatedCount int
}

func NewManager(store state.Store) *Manager {
	return &Manager{
		store:      store,
		embeddings: newEmbeddingClientFromEnv(),
	}
}

func (m *Manager) CacheGuardSnapshot(ctx context.Context, sessionID string, branch string) map[string]any {
	sessionID = normalizeSessionID(sessionID)
	branch = normalizeBranch(branch)
	snapshot := map[string]any{
		"session_id":           sessionID,
		"branch":               branch,
		"blocked":              false,
		"hard_stop":            false,
		"recent_invalidations": []map[string]any{},
	}
	if m == nil || m.store == nil || sessionID == "" {
		return snapshot
	}
	events, err := m.store.RecentInvalidations(ctx, sessionID, branch, 10)
	if err != nil {
		snapshot["error"] = err.Error()
		return snapshot
	}
	recent := make([]map[string]any, 0, len(events))
	for _, event := range events {
		recent = append(recent, invalidationPayload(event))
		if strings.EqualFold(strings.TrimSpace(event.Reason), hardStopReason) {
			snapshot["blocked"] = true
			snapshot["hard_stop"] = true
			snapshot["reason"] = event.Reason
			snapshot["last_invalidated_at"] = event.LoggedAt
		}
	}
	snapshot["recent_invalidations"] = recent
	return snapshot
}

func (m *Manager) BuildRuntimeContext(ctx context.Context, task domain.Task, agentID string, provider string, modelName string) map[string]any {
	sessionID := normalizeSessionID(firstNonEmpty(task.SessionID, task.ID))
	branch := normalizeBranch(task.Context.Branch)
	memoryScope := normalizedMemoryScope(task.MemoryScope)
	cachePolicy := normalizedCachePolicy(task.CachePolicy)
	memoryKeys := cleanMemoryKeys(task.MemoryKeys)

	runtime := map[string]any{
		"session_id":    sessionID,
		"branch":        branch,
		"agent_id":      strings.TrimSpace(agentID),
		"provider":      strings.TrimSpace(provider),
		"model_name":    strings.TrimSpace(modelName),
		"memory_scope":  memoryScope,
		"memory_keys":   memoryKeys,
		"cache_policy":  cachePolicy,
		"loaded_at":     time.Now().UTC(),
		"session_state": map[string]any{},
		"memory":        map[string]any{},
	}
	validation := map[string]any{
		"session_id":           sessionID,
		"branch":               branch,
		"cache_policy":         cachePolicy,
		"memory_scope":         memoryScope,
		"memory_keys":          memoryKeys,
		"cache_guard_snapshot": m.CacheGuardSnapshot(ctx, sessionID, branch),
		"recent_invalidations": []map[string]any{},
		"blocked":              false,
	}

	if m == nil || m.store == nil || sessionID == "" {
		runtime["validation_context"] = validation
		return runtime
	}

	sessionState, ok, err := m.store.GetSessionState(ctx, sessionID, branch)
	if err == nil && ok {
		runtime["session_state"] = sessionStatePayload(sessionState)
		runtime["memory"] = scopedMemory(sessionState.State, memoryScope, memoryKeys)
	} else if err != nil {
		runtime["session_state_error"] = err.Error()
	}

	events, err := m.store.RecentInvalidations(ctx, sessionID, branch, 10)
	if err == nil {
		recent := make([]map[string]any, 0, len(events))
		blocked := false
		for _, event := range events {
			recent = append(recent, invalidationPayload(event))
			if strings.EqualFold(strings.TrimSpace(event.Reason), hardStopReason) {
				blocked = true
			}
		}
		validation["recent_invalidations"] = recent
		validation["blocked"] = blocked
	} else {
		validation["error"] = err.Error()
	}

	runtime["validation_context"] = validation
	return runtime
}

func (m *Manager) LoadMemoryContext(ctx context.Context, task domain.Task, agentID string, provider string, modelName string) map[string]any {
	runtime := m.BuildRuntimeContext(ctx, task, agentID, provider, modelName)
	contextValue := cloneMap(runtime)
	validation := asMap(runtime["validation_context"])
	if len(validation) > 0 {
		contextValue["validation_context"] = validation
		for key, value := range validation {
			contextValue[key] = value
		}
	}

	sessionState := asMap(runtime["session_state"])
	stateData := asMap(sessionState["state"])
	memoryValues := asMap(runtime["memory"])
	contextValue["session_state"] = sessionState
	contextValue["memory"] = memoryValues
	contextValue["memory_identifier"] = memoryIdentifier(task, agentID)
	contextValue["trained_memory_disabled_for_risk"] = true
	contextValue["trained_memory_brief"] = ""
	contextValue["reusable_task_memory_brief"] = ""
	contextValue["reusable_task_memory_count"] = 0
	contextValue["layered_context_brief"] = ""
	contextValue["prompt_memory_brief"] = ""
	contextValue["routing_memory_brief"] = ""
	contextValue["execution_memory_brief"] = ""
	contextValue["prompt_guidance"] = []string{}
	contextValue["vector_memory_brief"] = ""
	contextValue["vector_memory_count"] = 0
	contextValue["vector_memory_hits"] = []map[string]any{}
	contextValue["augmented_prompt"] = ""
	contextValue["prompt_context_blocks"] = []string{}

	if normalizedCachePolicy(task.CachePolicy) == "write_only" {
		return contextValue
	}

	if len(memoryValues) == 0 && len(stateData) > 0 {
		memoryValues = scopedMemory(stateData, normalizedMemoryScope(task.MemoryScope), cleanMemoryKeys(task.MemoryKeys))
		contextValue["memory"] = memoryValues
	}
	for key, value := range memoryValues {
		if shouldExposeMemoryKey(key) {
			contextValue[key] = value
		}
	}
	vectorContext := m.buildVectorPromptContext(ctx, task, agentID)
	for key, value := range vectorContext {
		contextValue[key] = value
	}
	_ = m.RecordPromptInput(ctx, task, agentID)
	return contextValue
}

func (m *Manager) RecordPromptInput(ctx context.Context, task domain.Task, agentID string) error {
	if m == nil || m.store == nil {
		return nil
	}
	text := m.queryText(task)
	if strings.TrimSpace(text) == "" {
		return nil
	}
	_, err := m.IngestText(ctx, firstNonEmpty(task.SessionID, task.ID), task.Context.Branch, "chat_prompt", firstNonEmpty(task.ID, agentID), text, map[string]any{
		"agent_id":    strings.TrimSpace(agentID),
		"task_id":     task.ID,
		"task_type":   string(task.Type),
		"project":     task.Context.Project,
		"repo_path":   task.Context.RepoPath,
		"source_kind": "task_input",
	})
	return err
}

func (m *Manager) RecordTaskExchange(ctx context.Context, task domain.Task, result domain.AgentResult) error {
	if m == nil || m.store == nil {
		return nil
	}
	sessionID := firstNonEmpty(task.SessionID, task.ID)
	branch := task.Context.Branch
	combined := []string{strings.TrimSpace(task.Input.Description), strings.TrimSpace(result.Output.Summary)}
	if strings.TrimSpace(strings.Join(combined, " ")) == "" {
		return nil
	}
	_, err := m.IngestText(ctx, sessionID, branch, "task_exchange", firstNonEmpty(task.ID, result.TaskID), strings.Join(combined, "\n\n"), map[string]any{
		"task_id":     task.ID,
		"task_type":   string(task.Type),
		"agent_id":    firstNonEmpty(result.AgentID, task.RequiredCapability),
		"provider":    firstNonEmpty(result.Provider, task.AssignedProvider),
		"model_name":  firstNonEmpty(result.ModelName, task.AssignedModel),
		"project":     task.Context.Project,
		"files":       append([]string(nil), task.Input.Files...),
		"constraints": append([]string(nil), task.Input.Constraints...),
	})
	return err
}

func (m *Manager) RecordPeerExchange(ctx context.Context, envelope domain.TaskEnvelope, acceptance domain.TaskAcceptance, result *domain.AgentResult, reason string) error {
	if m == nil || m.store == nil {
		return nil
	}
	parts := []string{
		strings.TrimSpace(envelope.Payload.Objective),
		strings.TrimSpace(strings.Join(envelope.Payload.AcceptanceCriteria, "\n")),
		strings.TrimSpace(reason),
	}
	if result != nil {
		parts = append(parts, strings.TrimSpace(result.Output.Summary), strings.TrimSpace(strings.Join(result.Errors, "\n")))
	}
	text := strings.TrimSpace(strings.Join(parts, "\n\n"))
	if text == "" {
		return nil
	}
	metadata := map[string]any{
		"task_id":             envelope.TaskID,
		"parent_task_id":      envelope.ParentTaskID,
		"trace_id":            envelope.TraceID,
		"correlation_id":      envelope.CorrelationID,
		"source_agent":        envelope.SourceAgent,
		"target_agent":        envelope.TargetAgent,
		"target_capability":   envelope.TargetCapability,
		"provider":            acceptance.Provider,
		"model_name":          acceptance.ModelName,
		"retry_count":         envelope.RetryCount,
		"is_dead_letter":      envelope.IsDeadLetter,
		"source_kind":         "peer_exchange",
		"expected_output":     envelope.Payload.ExpectedOutputFormat,
		"envelope_artifacts":  append([]string(nil), envelope.Payload.Artifacts...),
		"acceptance_criteria": append([]string(nil), envelope.Payload.AcceptanceCriteria...),
	}
	if result != nil {
		metadata["result_status"] = result.Status
		metadata["result_agent_id"] = result.AgentID
		metadata["result_provider"] = result.Provider
		metadata["result_model_name"] = result.ModelName
	}
	_, err := m.IngestText(ctx, firstNonEmpty(envelope.CorrelationID, envelope.TaskID), envelope.ContextScope, "peer_exchange", firstNonEmpty(envelope.TaskID, envelope.TraceID), text, metadata)
	return err
}

func (m *Manager) IngestText(ctx context.Context, sessionID string, branch string, source string, sourceID string, text string, metadata map[string]any) ([]domain.VectorChunk, error) {
	if m == nil || m.store == nil {
		return nil, nil
	}
	sessionID = normalizeSessionID(sessionID)
	branch = normalizeBranch(branch)
	normalized := normalizeVectorText(text)
	if normalized == "" {
		return nil, nil
	}
	chunksText := chunkText(normalized, defaultChunkWordLimit, defaultChunkOverlap)
	chunks := make([]domain.VectorChunk, 0, len(chunksText))
	for idx, chunkText := range chunksText {
		terms := uniqueTerms(chunkText, 32)
		embedding := m.embedText(ctx, prepareDocumentEmbeddingInput(firstNonEmpty(sourceID, source), chunkText), defaultVectorDims)
		chunks = append(chunks, domain.VectorChunk{
			ChunkID:        vectorChunkID(sessionID, branch, source, sourceID, idx, chunkText),
			SessionID:      sessionID,
			Branch:         branch,
			Source:         strings.TrimSpace(source),
			SourceID:       strings.TrimSpace(sourceID),
			ChunkIndex:     idx,
			Text:           chunkText,
			NormalizedText: chunkText,
			Terms:          terms,
			Embedding:      embedding,
			Metadata:       cloneMap(metadata),
			CreatedAt:      time.Now().UTC(),
		})
	}
	if len(chunks) == 0 {
		return nil, nil
	}
	return chunks, m.store.UpsertVectorChunks(ctx, chunks)
}

func (m *Manager) IngestDocument(ctx context.Context, document domain.RAGDocument) error {
	if m == nil || m.store == nil {
		return nil
	}
	now := time.Now().UTC()
	document.DocumentID = strings.TrimSpace(document.DocumentID)
	if document.DocumentID == "" {
		document.DocumentID = vectorChunkID(
			firstNonEmpty(document.OwnerID, document.RepoID, "global"),
			firstNonEmpty(document.Branch, "default"),
			"rag_document",
			firstNonEmpty(document.SourceRef, document.Title, "document"),
			0,
			firstNonEmpty(document.ContentSummary, document.ContentText, document.Title),
		)
	}
	if document.Scope == "" {
		document.Scope = "session"
	}
	if document.CreatedAt.IsZero() {
		document.CreatedAt = now
	}
	document.UpdatedAt = now
	if document.LastAccessedAt.IsZero() {
		document.LastAccessedAt = now
	}
	if document.Metadata == nil {
		document.Metadata = map[string]any{}
	}
	document.Metadata["repo_id"] = firstNonEmpty(document.RepoID, fmt.Sprint(document.Metadata["repo_id"]))
	document.Metadata["branch"] = firstNonEmpty(document.Branch, fmt.Sprint(document.Metadata["branch"]))
	document.Metadata["commit_sha"] = firstNonEmpty(document.CommitSHA, fmt.Sprint(document.Metadata["commit_sha"]))
	document.Metadata["scope"] = firstNonEmpty(document.Scope, fmt.Sprint(document.Metadata["scope"]))
	document.Metadata["owner_id"] = firstNonEmpty(document.OwnerID, fmt.Sprint(document.Metadata["owner_id"]))
	document.Metadata["importance"] = document.Importance
	if err := m.store.UpsertRAGDocuments(ctx, []domain.RAGDocument{document}); err != nil {
		return err
	}
	searchable := strings.TrimSpace(strings.Join([]string{document.Title, document.ContentSummary, document.ContentText}, "\n\n"))
	if searchable == "" {
		return nil
	}
	_, err := m.IngestText(ctx, firstNonEmpty(document.OwnerID, document.RepoID, document.DocumentID), firstNonEmpty(document.Branch, "default"), "rag_document", document.DocumentID, searchable, document.Metadata)
	return err
}

func (m *Manager) Remember(ctx context.Context, memory domain.RAGMemoryRecord) error {
	if m == nil || m.store == nil {
		return nil
	}
	now := time.Now().UTC()
	memory.MemoryID = strings.TrimSpace(memory.MemoryID)
	if memory.MemoryID == "" {
		memory.MemoryID = vectorChunkID(
			firstNonEmpty(memory.OwnerID, memory.RepoID, "global"),
			firstNonEmpty(memory.Branch, "default"),
			"rag_memory",
			firstNonEmpty(memory.MemoryType, "memory"),
			0,
			firstNonEmpty(memory.Summary, fmt.Sprint(memory.Content)),
		)
	}
	if memory.Scope == "" {
		memory.Scope = "session"
	}
	if memory.CreatedAt.IsZero() {
		memory.CreatedAt = now
	}
	memory.UpdatedAt = now
	if memory.Metadata == nil {
		memory.Metadata = map[string]any{}
	}
	memory.Metadata["repo_id"] = firstNonEmpty(memory.RepoID, fmt.Sprint(memory.Metadata["repo_id"]))
	memory.Metadata["branch"] = firstNonEmpty(memory.Branch, fmt.Sprint(memory.Metadata["branch"]))
	memory.Metadata["commit_sha"] = firstNonEmpty(memory.CommitSHA, fmt.Sprint(memory.Metadata["commit_sha"]))
	memory.Metadata["scope"] = firstNonEmpty(memory.Scope, fmt.Sprint(memory.Metadata["scope"]))
	memory.Metadata["owner_id"] = firstNonEmpty(memory.OwnerID, fmt.Sprint(memory.Metadata["owner_id"]))
	memory.Metadata["importance"] = memory.Importance
	memory.Metadata["confidence"] = memory.Confidence
	if len(memory.Embedding) == 0 {
		payload := strings.TrimSpace(strings.Join([]string{memory.Summary, stringifyContent(memory.Content)}, "\n\n"))
		if payload != "" {
			memory.Embedding = m.embedText(ctx, prepareDocumentEmbeddingInput(memory.MemoryID, payload), defaultVectorDims)
		}
	}
	if err := m.store.UpsertRAGMemories(ctx, []domain.RAGMemoryRecord{memory}); err != nil {
		return err
	}
	searchable := strings.TrimSpace(strings.Join([]string{memory.Summary, stringifyContent(memory.Content)}, "\n\n"))
	if searchable == "" {
		return nil
	}
	_, err := m.IngestText(ctx, firstNonEmpty(memory.OwnerID, memory.RepoID, memory.MemoryID), firstNonEmpty(memory.Branch, "default"), "rag_memory", memory.MemoryID, searchable, memory.Metadata)
	return err
}

func (m *Manager) SearchVectorContext(ctx context.Context, task domain.Task, topK int) ([]domain.VectorSearchResult, error) {
	if m == nil || m.store == nil {
		return nil, nil
	}
	sessionID := normalizeSessionID(firstNonEmpty(task.SessionID, task.ID))
	branch := normalizeBranch(task.Context.Branch)
	queryText := m.queryText(task)
	if strings.TrimSpace(queryText) == "" {
		return nil, nil
	}
	query := domain.VectorSearchQuery{
		SessionID:      sessionID,
		Branch:         branch,
		Text:           queryText,
		Terms:          uniqueTerms(queryText, 40),
		Embedding:      m.embedText(ctx, prepareQueryEmbeddingInput(queryText), defaultVectorDims),
		TopK:           topK,
		CandidateLimit: defaultVectorCandidateCap,
	}
	chunks, err := m.store.ListVectorChunks(ctx, sessionID, branch, defaultVectorCandidateCap)
	if err != nil {
		return nil, err
	}
	if len(chunks) < topK {
		globalChunks, globalErr := m.store.ListVectorChunks(ctx, "", "", defaultVectorCandidateCap)
		if globalErr == nil {
			chunks = append(chunks, globalChunks...)
		}
	}
	return m.rankVectorChunks(task, query, chunks, topK), nil
}

func (m *Manager) embedText(ctx context.Context, text string, fallbackDims int) []float64 {
	if m != nil && m.embeddings != nil && m.embeddings.configured() {
		embedding, err := m.embeddings.embed(ctx, text)
		if err == nil && len(embedding) > 0 {
			return embedding
		}
	}
	return embedText(text, fallbackDims)
}

func (m *Manager) buildVectorPromptContext(ctx context.Context, task domain.Task, agentID string) map[string]any {
	budget := retrievalTokenBudget(task)
	results, err := m.SearchVectorContext(ctx, task, defaultVectorTopK)
	if err != nil || len(results) == 0 {
		return map[string]any{
			"vector_memory_brief":   "",
			"vector_memory_count":   0,
			"vector_memory_hits":    []map[string]any{},
			"augmented_prompt":      buildAugmentedPrompt(task, ""),
			"prompt_context_blocks": []string{"instruction", "user_prompt"},
			"retrieval_budget": retrievalUsage{
				BudgetTokens: budget,
				UsedTokens:   0,
			},
		}
	}
	packed, usage := packVectorResults(results, budget)
	if len(packed) == 0 {
		return map[string]any{
			"vector_memory_brief":   "",
			"vector_memory_count":   0,
			"vector_memory_hits":    []map[string]any{},
			"augmented_prompt":      buildAugmentedPrompt(task, ""),
			"prompt_context_blocks": []string{"instruction", "user_prompt"},
			"retrieval_query":       m.queryText(task),
			"memory_identifier":     memoryIdentifier(task, agentID),
			"retrieval_budget":      usage,
		}
	}
	lines := []string{fmt.Sprintf("--- VECTOR MEMORY (Packed %d/%d) ---", len(packed), len(results))}
	payload := make([]map[string]any, 0, len(packed))
	for _, result := range packed {
		summary := truncateText(result.Chunk.Text, 280)
		lines = append(lines, fmt.Sprintf("[score=%0.2f][source=%s][chunk=%d] %s", result.Score, result.Chunk.Source, result.Chunk.ChunkIndex, summary))
		payload = append(payload, map[string]any{
			"chunk_id":       result.Chunk.ChunkID,
			"source":         result.Chunk.Source,
			"source_id":      result.Chunk.SourceID,
			"score":          result.Score,
			"cosine":         result.Cosine,
			"term_overlap":   result.TermOverlap,
			"keyword_hits":   result.KeywordHits,
			"summary_signal": result.SummarySignal,
			"text":           summary,
		})
	}
	brief := strings.Join(lines, "\n")
	return map[string]any{
		"vector_memory_brief":   brief,
		"vector_memory_count":   len(packed),
		"vector_memory_hits":    payload,
		"augmented_prompt":      buildAugmentedPrompt(task, brief),
		"prompt_context_blocks": []string{"instruction", "retrieved_context", "user_prompt"},
		"retrieval_query":       m.queryText(task),
		"memory_identifier":     memoryIdentifier(task, agentID),
		"retrieval_budget":      usage,
	}
}

func (m *Manager) rankVectorChunks(task domain.Task, query domain.VectorSearchQuery, chunks []domain.VectorChunk, topK int) []domain.VectorSearchResult {
	seen := map[string]struct{}{}
	type scoredChunk struct {
		chunk           domain.VectorChunk
		cosine          float64
		overlap         float64
		keywordHits     int
		recency         float64
		scopeConfidence float64
		importance      float64
		semanticRank    int
		lexicalRank     int
	}
	candidates := make([]scoredChunk, 0, len(chunks))
	for _, chunk := range chunks {
		if _, ok := seen[chunk.ChunkID]; ok {
			continue
		}
		seen[chunk.ChunkID] = struct{}{}
		if len(query.Embedding) > 0 && len(chunk.Embedding) > 0 && len(query.Embedding) != len(chunk.Embedding) {
			continue
		}
		cosine := cosineSimilarity(query.Embedding, chunk.Embedding)
		overlap, keywordHits := termOverlapScore(query.Terms, chunk.Terms)
		if cosine < 0.12 && overlap < 0.08 {
			continue
		}
		importance := clamp01(float64(extractNumeric(chunk.Metadata["importance"])) / 100.0)
		if importance == 0 {
			importance = clamp01(extractFloat(chunk.Metadata["importance"]))
		}
		confidence := clamp01(float64(extractNumeric(chunk.Metadata["confidence"])) / 100.0)
		if confidence == 0 {
			confidence = clamp01(extractFloat(chunk.Metadata["confidence"]))
		}
		scopeConfidence := scopeConfidenceScore(task, chunk.Metadata, confidence)
		candidates = append(candidates, scoredChunk{
			chunk:           chunk,
			cosine:          cosine,
			overlap:         overlap,
			keywordHits:     keywordHits,
			recency:         recencyScore(chunk.CreatedAt),
			scopeConfidence: scopeConfidence,
			importance:      importance,
		})
	}
	if len(candidates) == 0 {
		return nil
	}
	semantic := append([]scoredChunk(nil), candidates...)
	sort.SliceStable(semantic, func(i, j int) bool {
		if semantic[i].cosine == semantic[j].cosine {
			return semantic[i].recency > semantic[j].recency
		}
		return semantic[i].cosine > semantic[j].cosine
	})
	semanticRank := make(map[string]int, len(semantic))
	for idx, candidate := range semantic {
		semanticRank[candidate.chunk.ChunkID] = idx + 1
	}
	lexical := append([]scoredChunk(nil), candidates...)
	sort.SliceStable(lexical, func(i, j int) bool {
		if lexical[i].keywordHits == lexical[j].keywordHits {
			if lexical[i].overlap == lexical[j].overlap {
				return lexical[i].recency > lexical[j].recency
			}
			return lexical[i].overlap > lexical[j].overlap
		}
		return lexical[i].keywordHits > lexical[j].keywordHits
	})
	lexicalRank := make(map[string]int, len(lexical))
	for idx, candidate := range lexical {
		lexicalRank[candidate.chunk.ChunkID] = idx + 1
	}
	results := make([]domain.VectorSearchResult, 0, len(candidates))
	for _, candidate := range candidates {
		candidate.semanticRank = semanticRank[candidate.chunk.ChunkID]
		candidate.lexicalRank = lexicalRank[candidate.chunk.ChunkID]
		rrfScore := (1.0 / (defaultRRFK + float64(candidate.semanticRank))) + (1.0 / (defaultRRFK + float64(candidate.lexicalRank)))
		score := candidate.cosine*0.30 + candidate.overlap*0.18 + candidate.importance*0.12 + candidate.recency*0.08 + candidate.scopeConfidence*0.08 + clamp01(rrfScore*defaultRRFK/2.0)*0.24
		score += minFloat(0.04, float64(candidate.keywordHits)/20.0)
		if project := strings.TrimSpace(task.Context.Project); project != "" && strings.EqualFold(project, strings.TrimSpace(fmt.Sprint(candidate.chunk.Metadata["project"]))) {
			score += 0.04
		}
		if strings.TrimSpace(candidate.chunk.SourceID) == strings.TrimSpace(task.ID) {
			score -= 0.08
		}
		if score < 0.18 {
			continue
		}
		results = append(results, domain.VectorSearchResult{
			Chunk:         candidate.chunk,
			Score:         round2(score),
			Cosine:        round2(candidate.cosine),
			TermOverlap:   round2(candidate.overlap),
			KeywordHits:   candidate.keywordHits,
			SummarySignal: round2(candidate.scopeConfidence),
			RecencyScore:  round2(candidate.recency),
		})
	}
	sort.SliceStable(results, func(i, j int) bool {
		if results[i].Score == results[j].Score {
			return results[i].RecencyScore > results[j].RecencyScore
		}
		return results[i].Score > results[j].Score
	})
	if topK <= 0 {
		topK = defaultVectorTopK
	}
	return selectDiverseVectorResults(results, topK)
}

func selectDiverseVectorResults(results []domain.VectorSearchResult, topK int) []domain.VectorSearchResult {
	if topK <= 0 {
		topK = defaultVectorTopK
	}
	if len(results) <= topK {
		return results
	}
	selected := make([]domain.VectorSearchResult, 0, minInt(topK, len(results)))
	remaining := make([]domain.VectorSearchResult, 0, len(results))
	seenSources := map[string]struct{}{}
	for _, result := range results {
		sourceKey := vectorSourceKey(result.Chunk)
		if sourceKey != "" {
			if _, ok := seenSources[sourceKey]; ok {
				remaining = append(remaining, result)
				continue
			}
			seenSources[sourceKey] = struct{}{}
		}
		selected = append(selected, result)
		if len(selected) >= topK {
			return selected
		}
	}
	for _, result := range remaining {
		selected = append(selected, result)
		if len(selected) >= topK {
			break
		}
	}
	return selected
}

func vectorSourceKey(chunk domain.VectorChunk) string {
	if sourceID := strings.TrimSpace(chunk.SourceID); sourceID != "" {
		return sourceID
	}
	return strings.TrimSpace(chunk.Source)
}

func prepareDocumentEmbeddingInput(sourceRef string, content string) string {
	normalizedContent := normalizeVectorText(content)
	if normalizedContent == "" {
		return ""
	}
	header := strings.TrimSpace(sourceRef)
	if header == "" {
		return "search_document: " + normalizedContent
	}
	return "search_document: " + header + "\n" + normalizedContent
}

func prepareQueryEmbeddingInput(query string) string {
	normalizedQuery := normalizeVectorText(query)
	if normalizedQuery == "" {
		return ""
	}
	return "search_query: " + normalizedQuery
}

func retrievalTokenBudget(task domain.Task) int {
	budget := defaultRetrievalTokenBudget
	if len(task.Input.AcceptanceCriteria) > 0 {
		budget += minInt(48, len(task.Input.AcceptanceCriteria)*8)
	}
	if len(task.Input.Files) > 0 {
		budget += minInt(48, len(task.Input.Files)*6)
	}
	return budget
}

func packVectorResults(results []domain.VectorSearchResult, budgetTokens int) ([]domain.VectorSearchResult, retrievalUsage) {
	if budgetTokens <= 0 {
		budgetTokens = defaultRetrievalTokenBudget
	}
	usage := retrievalUsage{BudgetTokens: budgetTokens}
	packed := make([]domain.VectorSearchResult, 0, len(results))
	for _, result := range results {
		chunkTokens := approximateTokenCount(result.Chunk.Text) + defaultChunkTokenOverhead
		if chunkTokens > budgetTokens {
			usage.TruncatedCount++
			continue
		}
		if usage.UsedTokens+chunkTokens > budgetTokens {
			usage.TruncatedCount += len(results) - len(packed)
			break
		}
		usage.UsedTokens += chunkTokens
		packed = append(packed, result)
	}
	return packed, usage
}

func approximateTokenCount(text string) int {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return 0
	}
	return maxInt(1, len(strings.Fields(trimmed))*5/4)
}

func (m *Manager) queryText(task domain.Task) string {
	parts := []string{strings.TrimSpace(task.Input.Description)}
	if len(task.Input.Files) > 0 {
		parts = append(parts, "files: "+strings.Join(task.Input.Files, " "))
	}
	if len(task.Input.Constraints) > 0 {
		parts = append(parts, "constraints: "+strings.Join(task.Input.Constraints, " "))
	}
	if len(task.Input.AcceptanceCriteria) > 0 {
		parts = append(parts, "acceptance: "+strings.Join(task.Input.AcceptanceCriteria, " "))
	}
	return normalizeVectorText(strings.Join(parts, "\n"))
}

func (m *Manager) EstimateTaskTokens(task domain.Task) int {
	input := strings.TrimSpace(task.Input.Description)
	if input == "" {
		input = strings.TrimSpace(strings.Join(task.Input.Constraints, " "))
	}
	estimated := len(input) / 4
	if estimated < 32 {
		return 32
	}
	return estimated
}

func (m *Manager) EvaluateModelBudget(ctx context.Context, task domain.Task, modelName string, plannedTokens int) map[string]any {
	trimmedModel := strings.TrimSpace(modelName)
	if trimmedModel == "" {
		trimmedModel = "unassigned"
	}
	limit := defaultModelLimit(trimmedModel)
	used := 0
	if m != nil && m.store != nil {
		stateValue, ok, err := m.store.GetSessionState(ctx, normalizeSessionID(firstNonEmpty(task.SessionID, task.ID)), normalizeBranch(task.Context.Branch))
		if err == nil && ok {
			used = extractModelUsage(stateValue.State, trimmedModel)
		}
	}
	if plannedTokens < 0 {
		plannedTokens = 0
	}
	remainingBefore := limit - used
	if remainingBefore < 0 {
		remainingBefore = 0
	}
	remainingAfter := remainingBefore - plannedTokens
	if remainingAfter < 0 {
		remainingAfter = 0
	}
	warnBelow, reduceBelow, errorBelow := budgetThresholds()
	remainingPct := percentage(remainingAfter, limit)
	usedPct := percentage(used, limit)
	action := "ok"
	switch {
	case remainingPct <= errorBelow:
		action = "error"
	case remainingPct <= reduceBelow:
		action = "reduce"
	case remainingPct <= warnBelow:
		action = "warn"
	}
	return map[string]any{
		"model":                   trimmedModel,
		"limit_tokens":            limit,
		"used_tokens":             used,
		"planned_tokens":          plannedTokens,
		"remaining_tokens":        remainingAfter,
		"remaining_percentage":    round2(remainingPct),
		"used_percentage":         round2(usedPct),
		"warn_below_percentage":   round2(warnBelow),
		"reduce_below_percentage": round2(reduceBelow),
		"error_below_percentage":  round2(errorBelow),
		"action":                  action,
	}
}

func (m *Manager) RecordModelUsage(ctx context.Context, task domain.Task, modelName string, result domain.AgentResult) error {
	if m == nil || m.store == nil {
		return nil
	}
	trimmedModel := strings.TrimSpace(modelName)
	if trimmedModel == "" {
		trimmedModel = strings.TrimSpace(result.ModelName)
	}
	if trimmedModel == "" {
		return nil
	}
	usedTokens := usageTotalTokens(result.Output.Artifacts)
	if usedTokens <= 0 {
		return nil
	}
	sessionID := normalizeSessionID(firstNonEmpty(task.SessionID, task.ID))
	if sessionID == "" {
		return nil
	}
	branch := normalizeBranch(task.Context.Branch)
	current := map[string]any{}
	promptVersion := ""
	contextVersion := ""
	if existing, ok, err := m.store.GetSessionState(ctx, sessionID, branch); err == nil && ok {
		current = cloneMap(existing.State)
		promptVersion = existing.PromptVersion
		contextVersion = existing.ContextVersion
	}
	usage := cloneMap(asMap(current[modelUsageStateKey]))
	usage[trimmedModel] = extractNumeric(usage[trimmedModel]) + usedTokens
	current[modelUsageStateKey] = usage
	_, err := m.store.SaveSessionState(ctx, sessionID, branch, current, promptVersion, contextVersion, nil)
	return err
}

func (m *Manager) RecordHardStop(ctx context.Context, task domain.Task, action string, reason string) error {
	if m == nil || m.store == nil {
		return nil
	}
	sessionID := normalizeSessionID(firstNonEmpty(task.SessionID, task.ID))
	if sessionID == "" {
		return nil
	}
	_, err := m.store.RecordInvalidation(ctx, sessionID, normalizeBranch(task.Context.Branch), hardStopReason, map[string]any{
		"action":  action,
		"reason":  strings.TrimSpace(reason),
		"task_id": task.ID,
	})
	return err
}

func clamp01(value float64) float64 {
	if value < 0 {
		return 0
	}
	if value > 1 {
		return 1
	}
	return value
}

func extractFloat(value any) float64 {
	switch typed := value.(type) {
	case nil:
		return 0
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int32:
		return float64(typed)
	case int64:
		return float64(typed)
	case uint:
		return float64(typed)
	case uint32:
		return float64(typed)
	case uint64:
		return float64(typed)
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		if err == nil {
			return parsed
		}
	}
	return 0
}

func stringifyContent(content map[string]any) string {
	if len(content) == 0 {
		return ""
	}
	keys := make([]string, 0, len(content))
	for key := range content {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		value := strings.TrimSpace(fmt.Sprint(content[key]))
		if value == "" {
			continue
		}
		parts = append(parts, key+": "+value)
	}
	return strings.TrimSpace(strings.Join(parts, "\n"))
}

func scopeConfidenceScore(task domain.Task, metadata map[string]any, confidence float64) float64 {
	scopeSignal := 0.35
	taskScope := normalizedMemoryScope(task.MemoryScope)
	memoryScope := normalizedMemoryScope(fmt.Sprint(metadata["scope"]))
	if taskScope == memoryScope {
		scopeSignal += 0.35
	}
	if taskScope == "session" && memoryScope == "task" {
		scopeSignal += 0.1
	}
	if repoID := strings.TrimSpace(task.Context.Project); repoID != "" && repoID == strings.TrimSpace(fmt.Sprint(metadata["repo_id"])) {
		scopeSignal += 0.1
	}
	if branch := normalizeBranch(task.Context.Branch); branch != "default" && branch == normalizeBranch(fmt.Sprint(metadata["branch"])) {
		scopeSignal += 0.1
	}
	return clamp01((clamp01(scopeSignal) + clamp01(confidence)) / 2)
}

func normalizeSessionID(sessionID string) string {
	return strings.TrimSpace(sessionID)
}

func normalizeBranch(branch string) string {
	trimmed := strings.TrimSpace(branch)
	if trimmed == "" {
		return "default"
	}
	return trimmed
}

func normalizedMemoryScope(scope string) string {
	normalized := strings.ToLower(strings.TrimSpace(scope))
	switch normalized {
	case "task", "branch", "project", "session", "agent", "capability":
		return normalized
	default:
		return "session"
	}
}

func normalizedCachePolicy(policy string) string {
	normalized := strings.ToLower(strings.TrimSpace(policy))
	switch normalized {
	case "read_only", "write_only", "bypass", "read_write":
		return normalized
	default:
		return "read_write"
	}
}

func cleanMemoryKeys(keys []string) []string {
	if len(keys) == 0 {
		return nil
	}
	out := make([]string, 0, len(keys))
	seen := map[string]struct{}{}
	for _, key := range keys {
		trimmed := strings.TrimSpace(key)
		if trimmed == "" {
			continue
		}
		if _, ok := seen[trimmed]; ok {
			continue
		}
		seen[trimmed] = struct{}{}
		out = append(out, trimmed)
	}
	return out
}

func memoryIdentifier(task domain.Task, agentID string) string {
	switch normalizedMemoryScope(task.MemoryScope) {
	case "session":
		return firstNonEmpty(task.SessionID, "default")
	case "agent":
		if strings.TrimSpace(agentID) != "" {
			return strings.TrimSpace(agentID)
		}
	case "capability":
		if strings.TrimSpace(task.RequiredCapability) != "" {
			return strings.TrimSpace(task.RequiredCapability)
		}
		if strings.TrimSpace(string(task.Type)) != "" {
			return strings.TrimSpace(string(task.Type))
		}
	case "task":
		if strings.TrimSpace(task.ID) != "" {
			return strings.TrimSpace(task.ID)
		}
	}
	return firstNonEmpty(task.SessionID, task.ID, "default")
}

func shouldExposeMemoryKey(key string) bool {
	normalized := strings.ToLower(strings.TrimSpace(key))
	if normalized == "" {
		return false
	}
	return !strings.Contains(normalized, "thought")
}

func scopedMemory(values map[string]any, scope string, keys []string) map[string]any {
	if len(values) == 0 {
		return map[string]any{}
	}
	filtered := map[string]any{}
	if len(keys) > 0 {
		for _, key := range keys {
			if value, ok := values[key]; ok {
				filtered[key] = value
			}
		}
		return filtered
	}
	for key, value := range values {
		if scope == "task" && strings.HasPrefix(key, "session_") {
			continue
		}
		filtered[key] = value
	}
	return filtered
}

func sessionStatePayload(state domain.SessionState) map[string]any {
	return map[string]any{
		"session_id":      state.SessionID,
		"branch":          state.Branch,
		"version":         state.Version,
		"prompt_version":  state.PromptVersion,
		"context_version": state.ContextVersion,
		"state":           cloneMap(state.State),
		"updated_at":      state.UpdatedAt,
		"storage_mode":    state.StorageMode,
	}
}

func invalidationPayload(event domain.InvalidationEvent) map[string]any {
	return map[string]any{
		"session_id":   event.SessionID,
		"branch":       event.Branch,
		"reason":       event.Reason,
		"payload":      cloneMap(event.Payload),
		"logged_at":    event.LoggedAt,
		"storage_mode": event.StorageMode,
	}
}

func cloneMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func budgetThresholds() (float64, float64, float64) {
	warnBelow := envFloat("AI_BRIDGE_TOKEN_WARN_BELOW_PERCENT", defaultWarnBelowPercentage)
	reduceBelow := envFloat("AI_BRIDGE_TOKEN_REDUCE_BELOW_PERCENT", defaultReduceBelowPercent)
	errorBelow := envFloat("AI_BRIDGE_TOKEN_ERROR_BELOW_PERCENT", defaultErrorBelowPercent)
	if warnBelow < reduceBelow {
		warnBelow = reduceBelow
	}
	if reduceBelow < errorBelow {
		reduceBelow = errorBelow
	}
	return warnBelow, reduceBelow, errorBelow
}

func envFloat(key string, fallback float64) float64 {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func defaultModelLimit(modelName string) int {
	for key, limit := range defaultModelTokenLimits {
		if strings.EqualFold(key, modelName) {
			return limit
		}
	}
	return defaultModelTokenLimit
}

func extractModelUsage(stateValue map[string]any, modelName string) int {
	usage := asMap(stateValue[modelUsageStateKey])
	if len(usage) == 0 {
		return 0
	}
	for key, value := range usage {
		if strings.EqualFold(key, modelName) {
			return extractNumeric(value)
		}
	}
	return 0
}

func usageTotalTokens(artifacts map[string]any) int {
	usage := asMap(artifacts["usage"])
	if len(usage) == 0 {
		return 0
	}
	return extractNumeric(usage["total_tokens"])
}

func asMap(value any) map[string]any {
	typed, ok := value.(map[string]any)
	if !ok || typed == nil {
		return map[string]any{}
	}
	return cloneMap(typed)
}

func extractNumeric(value any) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int32:
		return int(typed)
	case int64:
		return int(typed)
	case float32:
		return int(typed)
	case float64:
		return int(typed)
	case jsonNumber:
		parsed, err := strconv.Atoi(string(typed))
		if err == nil {
			return parsed
		}
	case string:
		parsed, err := strconv.Atoi(strings.TrimSpace(typed))
		if err == nil {
			return parsed
		}
	}
	return 0
}

type jsonNumber string

func percentage(numerator int, denominator int) float64 {
	if denominator <= 0 {
		return 0
	}
	return (float64(numerator) / float64(denominator)) * 100
}

func round2(value float64) float64 {
	return math.Round(value*100) / 100
}

func normalizeVectorText(input string) string {
	input = strings.ToLower(strings.TrimSpace(input))
	if input == "" {
		return ""
	}
	replacer := strings.NewReplacer("\n", " ", "\t", " ", ",", " ", ";", " ", ":", " ", "(", " ", ")", " ", "[", " ", "]", " ", "{", " ", "}", " ", "_", " ", "-", " ", "/", " ", "\\", " ", ".", " ")
	normalized := replacer.Replace(input)
	return strings.Join(strings.Fields(normalized), " ")
}

func chunkText(text string, wordLimit int, overlap int) []string {
	words := strings.Fields(text)
	if len(words) == 0 {
		return nil
	}
	if wordLimit <= 0 {
		wordLimit = defaultChunkWordLimit
	}
	if overlap < 0 || overlap >= wordLimit {
		overlap = defaultChunkOverlap
	}
	stride := wordLimit - overlap
	if stride <= 0 {
		stride = wordLimit
	}
	chunks := make([]string, 0, maxInt(1, len(words)/stride+1))
	for start := 0; start < len(words); start += stride {
		end := start + wordLimit
		if end > len(words) {
			end = len(words)
		}
		chunk := strings.Join(words[start:end], " ")
		if strings.TrimSpace(chunk) != "" {
			chunks = append(chunks, chunk)
		}
		if end == len(words) {
			break
		}
	}
	return chunks
}

func uniqueTerms(text string, limit int) []string {
	words := strings.Fields(normalizeVectorText(text))
	seen := map[string]struct{}{}
	terms := make([]string, 0, minInt(limit, len(words)))
	for _, word := range words {
		if len(word) < 3 {
			continue
		}
		if _, ok := seen[word]; ok {
			continue
		}
		seen[word] = struct{}{}
		terms = append(terms, word)
		if limit > 0 && len(terms) >= limit {
			break
		}
	}
	return terms
}

func vectorChunkID(sessionID string, branch string, source string, sourceID string, chunkIndex int, text string) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte(strings.Join([]string{sessionID, branch, source, sourceID, strconv.Itoa(chunkIndex), normalizeVectorText(text)}, "|")))
	return fmt.Sprintf("vec_%x", h.Sum64())
}

func embedText(text string, dims int) []float64 {
	if dims <= 0 {
		dims = defaultVectorDims
	}
	vector := make([]float64, dims)
	terms := strings.Fields(normalizeVectorText(text))
	if len(terms) == 0 {
		return vector
	}
	counts := map[string]int{}
	for _, term := range terms {
		counts[term]++
	}
	for term, count := range counts {
		h := fnv.New64a()
		_, _ = h.Write([]byte(term))
		sum := h.Sum64()
		idx := int(sum % uint64(dims))
		sign := 1.0
		if (sum>>8)&1 == 1 {
			sign = -1.0
		}
		weight := (1.0 + math.Min(2.0, float64(len(term))/12.0)) * (1.0 + math.Log1p(float64(count)))
		vector[idx] += sign * weight
	}
	norm := 0.0
	for _, value := range vector {
		norm += value * value
	}
	norm = math.Sqrt(norm)
	if norm <= 1e-9 {
		return vector
	}
	for idx := range vector {
		vector[idx] = math.Round((vector[idx]/norm)*1_000_000) / 1_000_000
	}
	return vector
}

func cosineSimilarity(left []float64, right []float64) float64 {
	if len(left) == 0 || len(left) != len(right) {
		return 0
	}
	dot := 0.0
	leftNorm := 0.0
	rightNorm := 0.0
	for idx := range left {
		dot += left[idx] * right[idx]
		leftNorm += left[idx] * left[idx]
		rightNorm += right[idx] * right[idx]
	}
	if leftNorm <= 1e-9 || rightNorm <= 1e-9 {
		return 0
	}
	return dot / (math.Sqrt(leftNorm) * math.Sqrt(rightNorm))
}

func termOverlapScore(queryTerms []string, chunkTerms []string) (float64, int) {
	if len(queryTerms) == 0 || len(chunkTerms) == 0 {
		return 0, 0
	}
	querySet := map[string]struct{}{}
	for _, term := range queryTerms {
		querySet[term] = struct{}{}
	}
	chunkSet := map[string]struct{}{}
	for _, term := range chunkTerms {
		chunkSet[term] = struct{}{}
	}
	hits := 0
	for term := range querySet {
		if _, ok := chunkSet[term]; ok {
			hits++
		}
	}
	union := len(querySet)
	for term := range chunkSet {
		if _, ok := querySet[term]; !ok {
			union++
		}
	}
	if union == 0 {
		return 0, hits
	}
	return float64(hits) / float64(union), hits
}

func summarySignal(text string) float64 {
	terms := strings.Fields(normalizeVectorText(text))
	if len(terms) == 0 {
		return 0
	}
	unique := map[string]struct{}{}
	longTerms := 0
	for _, term := range terms {
		unique[term] = struct{}{}
		if len(term) >= 5 {
			longTerms++
		}
	}
	lengthScore := math.Min(1.0, float64(len(terms))/18.0)
	uniqueRatio := float64(len(unique)) / float64(len(terms))
	informativeRatio := float64(longTerms) / float64(len(terms))
	return math.Max(0.0, math.Min(1.0, 0.35*lengthScore+0.35*uniqueRatio+0.30*informativeRatio))
}

func recencyScore(createdAt time.Time) float64 {
	if createdAt.IsZero() {
		return 0.4
	}
	hours := time.Since(createdAt).Hours()
	if hours < 0 {
		hours = 0
	}
	return 1.0 / (1.0 + hours/72.0)
}

func buildAugmentedPrompt(task domain.Task, vectorBrief string) string {
	instruction := "Use retrieved context only when it is relevant, keep factual details from memory, and preserve the user's intent."
	userPrompt := strings.TrimSpace(task.Input.Description)
	if userPrompt == "" {
		userPrompt = strings.TrimSpace(strings.Join(task.Input.Constraints, "\n"))
	}
	parts := []string{"[INSTRUCTION]", instruction}
	if strings.TrimSpace(vectorBrief) != "" {
		parts = append(parts, "", "[CONTEXT]", vectorBrief)
	}
	parts = append(parts, "", "[USER PROMPT]", userPrompt)
	if len(task.Input.Files) > 0 {
		parts = append(parts, "files: "+strings.Join(task.Input.Files, ", "))
	}
	if len(task.Input.Constraints) > 0 {
		parts = append(parts, "constraints: "+strings.Join(task.Input.Constraints, " | "))
	}
	if len(task.Input.AcceptanceCriteria) > 0 {
		parts = append(parts, "acceptance: "+strings.Join(task.Input.AcceptanceCriteria, " | "))
	}
	return strings.Join(parts, "\n")
}

func truncateText(text string, limit int) string {
	trimmed := strings.TrimSpace(text)
	if limit <= 0 || len(trimmed) <= limit {
		return trimmed
	}
	if limit <= 3 {
		return trimmed[:limit]
	}
	return trimmed[:limit-3] + "..."
}

func minFloat(left float64, right float64) float64 {
	if left < right {
		return left
	}
	return right
}

func minInt(left int, right int) int {
	if left < right {
		return left
	}
	return right
}

func maxInt(left int, right int) int {
	if left > right {
		return left
	}
	return right
}
