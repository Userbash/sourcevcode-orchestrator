package kernel

import (
	"context"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

const (
	modelQwenCoder  = "gemma4-12b-agentic-fable5:q4_k_m"
	modelLocalSmall = "gemma4-12b-agentic-fable5:q4_k_m"
	modelOpenAIHigh = "gpt-5.5"
	modelMistral    = "mistral-large-latest"
)

var baseHighRiskKeywords = []string{"security", "auth", "rbac", "payment", "secret", "production", "migration", "destructive"}
var permissionContextKeywords = []string{"auth", "authorization", "role", "rbac", "admin", "security", "token", "database", "migration", "tenant"}
var lowRiskPermissionExemptions = []string{"permissions-sync-fix", "permission docs cleanup", "permission ui label", "permission comments", "permission formatting"}

type RiskEvaluation struct {
	DetectedKeywords         []string
	MatchedHighRiskRules     []string
	MatchedLowRiskExemptions []string
	HighRisk                 bool
}

type ModelSelector struct {
	registry    *ProviderModelRegistry
	memory      *memory.Manager
	routeMemory memory.RouteMemoryAgent
	retriever   memory.RetrieverAgent
}

type modelSelectionPolicy struct {
	targetProvider          string
	targetModel             string
	providers               []string
	families                []string
	models                  []string
	reason                  string
	cloudRequired           bool
	requiresSecondaryReview bool
}

func (s *ModelSelector) preferredCloudProvider() string {
	if s == nil || s.registry == nil {
		return "openai"
	}
	return agents.PreferredCloudProvider(s.registry.configs)
}

func NewModelSelector(registry *ProviderModelRegistry) *ModelSelector {
	return &ModelSelector{registry: registry}
}

func (s *ModelSelector) AttachMemoryManager(manager *memory.Manager) {
	if s == nil {
		return
	}
	s.memory = manager
	s.routeMemory = manager
	s.retriever = manager
}

func EvaluateRiskContext(text string) RiskEvaluation {
	text = strings.ToLower(strings.TrimSpace(text))
	evaluation := RiskEvaluation{}
	for _, keyword := range baseHighRiskKeywords {
		if strings.Contains(text, keyword) {
			evaluation.DetectedKeywords = append(evaluation.DetectedKeywords, keyword)
			evaluation.MatchedHighRiskRules = append(evaluation.MatchedHighRiskRules, keyword)
		}
	}
	permissionContext := false
	for _, keyword := range permissionContextKeywords {
		if strings.Contains(text, keyword) {
			permissionContext = true
			break
		}
	}
	if permissionContext {
		for _, exemption := range lowRiskPermissionExemptions {
			if strings.Contains(text, exemption) {
				evaluation.MatchedLowRiskExemptions = append(evaluation.MatchedLowRiskExemptions, exemption)
			}
		}
	}
	evaluation.HighRisk = len(evaluation.MatchedHighRiskRules) > 0 && len(evaluation.MatchedLowRiskExemptions) == 0
	return evaluation
}

func (s *ModelSelector) Classify(task domain.Task) domain.Complexity {
	if task.Complexity != "" {
		return task.Complexity
	}
	text := taskText(task)
	risk := EvaluateRiskContext(text)
	if task.Priority == domain.PriorityCritical || risk.HighRisk {
		return domain.ComplexityCritical
	}
	if task.Priority == domain.PriorityHigh || ((task.Type == domain.TaskTypePlan || task.Type == domain.TaskTypeReview) && containsAny(text, []string{"architecture", "distributed", "debugging"})) {
		return domain.ComplexityHigh
	}
	if len(risk.MatchedLowRiskExemptions) > 0 && (task.Type == domain.TaskTypeDocs || task.Type == domain.TaskTypeFix) && len(compactStrings(task.Input.Files)) <= 2 && len(text) < 120 {
		return domain.ComplexityLow
	}
	if task.Type == domain.TaskTypeCode || task.Type == domain.TaskTypeTest || task.Type == domain.TaskTypeFix || task.Type == domain.TaskTypeDocs || task.Type == domain.TaskTypeResearch || len(compactStrings(task.Input.Files)) > 2 {
		return domain.ComplexityMedium
	}
	return domain.ComplexityLow
}

func (s *ModelSelector) Select(task domain.Task) domain.ModelSelection {
	complexity := s.Classify(task)
	text := taskText(task)
	risk := EvaluateRiskContext(text)
	policy := s.selectionPolicy(task, complexity, risk)
	policy = s.expandPolicyModels(policy)
	choice := domain.ModelSelection{
		Complexity:               complexity,
		DetectedKeywords:         append([]string(nil), risk.DetectedKeywords...),
		MatchedHighRiskRules:     append([]string(nil), risk.MatchedHighRiskRules...),
		MatchedLowRiskExemptions: append([]string(nil), risk.MatchedLowRiskExemptions...),
		SelectionTrace: map[string]any{
			"task_type":           task.Type,
			"priority":            task.Priority,
			"required_capability": inferCapability(task),
			"sourcecraft_work":    isSourcecraftWork(task),
			"high_risk":           risk.HighRisk,
			"files":               len(compactStrings(task.Input.Files)),
			"acceptance_criteria": len(compactStrings(task.Input.AcceptanceCriteria)),
			"constraints":         len(compactStrings(task.Input.Constraints)),
			"live_model_criteria": []string{
				"confirmed models are alive only when Available=true and verification_status=confirmed",
				"verification_pending or retryable_failure models stay in catalog diagnostics but are excluded from healthy routing",
				"validation_failed, missing, disabled, or unavailable inventory entries are treated as dead",
			},
		},
	}
	choice.RequiresSecondaryReview = policy.requiresSecondaryReview
	choice.Provider, choice.ModelName = s.resolveAvailableModel(task, policy, choice.SelectionTrace)
	choice.Reason = policy.reason
	choice.SupportLanes = s.supportLanes(task, choice, policy)
	choice.SelectionTrace["target_provider"] = policy.targetProvider
	choice.SelectionTrace["target_model"] = policy.targetModel
	choice.SelectionTrace["provider_candidates"] = append([]string(nil), policy.providers...)
	choice.SelectionTrace["preferred_families"] = append([]string(nil), policy.families...)
	choice.SelectionTrace["preferred_models"] = append([]string(nil), policy.models...)
	choice.SelectionTrace["cloud_required"] = policy.cloudRequired
	choice.SelectionTrace["resolved_provider"] = choice.Provider
	choice.SelectionTrace["resolved_model"] = choice.ModelName
	if len(choice.SupportLanes) > 0 {
		choice.SelectionTrace["support_lanes"] = choice.SupportLanes
		choice.SelectionTrace["ai_kernel_helper_available"] = true
	}
	return choice
}

func (s *ModelSelector) selectionPolicy(task domain.Task, complexity domain.Complexity, risk RiskEvaluation) modelSelectionPolicy {
	if shouldEscalateToCloud(task, complexity, risk) {
		return modelSelectionPolicy{
			targetProvider:          s.preferredCloudProvider(),
			targetModel:             modelOpenAIHigh,
			providers:               s.providerPreference("cloud"),
			families:                []string{"gpt", "claude", "gemini", "deepseek", "mistral", "kimi", "glm", "qwen", "llama"},
			models:                  []string{"gpt-5.6-sol", modelOpenAIHigh, "gpt-5.4", "gpt-5.4-mini", "claude-sonnet-4-6", modelMistral},
			reason:                  "high_risk_or_high_complexity",
			cloudRequired:           true,
			requiresSecondaryReview: true,
		}
	}
	switch task.Type {
	case domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeTest:
		return modelSelectionPolicy{
			targetProvider:          "ai_kernel",
			targetModel:             modelQwenCoder,
			providers:               s.providerPreference("code"),
			families:                []string{"gemma", "deepseek", "llama", "glm", "gpt", "claude", "mistral", "gemini"},
			models:                  []string{modelQwenCoder, "deepseek-coder", "claude-sonnet-4-6", modelOpenAIHigh},
			reason:                  "code_specialist_path",
			requiresSecondaryReview: task.Type == domain.TaskTypeTest,
		}
	case domain.TaskTypePlan, domain.TaskTypeReview:
		return modelSelectionPolicy{
			targetProvider:          "mistral",
			targetModel:             modelMistral,
			providers:               s.providerPreference("analysis"),
			families:                []string{"claude", "gpt", "mistral", "gemini", "kimi", "qwen", "deepseek", "llama"},
			models:                  []string{"claude-sonnet-4-6", "gpt-5.6-sol", modelOpenAIHigh, modelMistral},
			reason:                  "analysis_review_path",
			requiresSecondaryReview: task.Type == domain.TaskTypeReview,
		}
	case domain.TaskTypeDocs:
		return modelSelectionPolicy{
			targetProvider: "ai_kernel",
			targetModel:    modelLocalSmall,
			providers:      s.providerPreference("docs"),
			families:       []string{"gemma", "llama", "mistral", "gpt", "claude", "gemini"},
			models:         []string{modelLocalSmall, modelQwenCoder, modelMistral},
			reason:         "docs_path",
		}
	case domain.TaskTypeResearch:
		return modelSelectionPolicy{
			targetProvider: "mistral",
			targetModel:    modelMistral,
			providers:      s.providerPreference("research"),
			families:       []string{"claude", "gpt", "mistral", "gemini", "kimi", "deepseek", "gemma"},
			models:         []string{"claude-sonnet-4-6", modelOpenAIHigh, modelMistral},
			reason:         "research_path",
		}
	default:
		return modelSelectionPolicy{
			targetProvider: "ai_kernel",
			targetModel:    modelLocalSmall,
			providers:      s.providerPreference("default"),
			families:       []string{"gemma", "llama", "gpt", "claude", "mistral"},
			models:         []string{modelLocalSmall, modelQwenCoder, modelOpenAIHigh},
			reason:         "policy_default",
		}
	}
}

func (s *ModelSelector) expandPolicyModels(policy modelSelectionPolicy) modelSelectionPolicy {
	if s == nil || s.registry == nil {
		policy.models = dedupeStrings(policy.models)
		return policy
	}
	providerOrder := dedupeStrings(append([]string{policy.targetProvider}, policy.providers...))
	for _, provider := range providerOrder {
		for _, model := range s.registry.HealthyModels(provider) {
			if !model.Available || !strings.EqualFold(strings.TrimSpace(model.VerificationStatus), "confirmed") {
				continue
			}
			if len(policy.families) > 0 {
				family := inferModelFamily(model.ModelName)
				if metadataFamily, _ := model.Metadata["model_family"].(string); strings.TrimSpace(metadataFamily) != "" {
					family = metadataFamily
				}
				matched := false
				for _, preferredFamily := range policy.families {
					if strings.EqualFold(family, preferredFamily) {
						matched = true
						break
					}
				}
				if !matched {
					continue
				}
			}
			policy.models = appendIfMissingPreserveOrder(policy.models, model.ModelName)
		}
	}
	policy.models = dedupeStrings(policy.models)
	return policy
}

func (s *ModelSelector) resolveAvailableModel(task domain.Task, policy modelSelectionPolicy, trace map[string]any) (string, string) {
	if s == nil || s.registry == nil {
		return policy.targetProvider, policy.targetModel
	}
	signals := s.buildSelectorSignals(task)
	providerOrder := append([]string{policy.targetProvider}, policy.providers...)
	providerOrder = dedupeStrings(providerOrder)
	candidates := make([]scoredHealthyModel, 0)
	for providerIndex, provider := range providerOrder {
		models := s.registry.HealthyModels(provider)
		for _, model := range models {
			base := scoreHealthyModel(model, policy)
			if base <= 0 {
				continue
			}
			history := scoreRouteHistory(model, signals.routeResults)
			budget := scoreBudgetFit(model, signals)
			retrieval := scoreRetrievalFit(model, policy, signals)
			failurePenalty := scoreFailurePenalty(model, signals)
			providerPriority := float64(len(providerOrder)-providerIndex) * 0.35
			total := float64(base) + providerPriority + history.Total + budget + retrieval - failurePenalty
			candidates = append(candidates, scoredHealthyModel{
				status: model,
				score:  total,
				components: map[string]any{
					"base":              base,
					"provider_priority": providerPriority,
					"history":           history,
					"budget":            budget,
					"retrieval":         retrieval,
					"failure_penalty":   failurePenalty,
				},
			})
		}
	}
	trace["selector_inputs"] = signals.trace()
	if len(candidates) == 0 {
		trace["candidate_scores"] = []map[string]any{}
		return policy.targetProvider, policy.targetModel
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].score == candidates[j].score {
			return candidates[i].status.ModelName < candidates[j].status.ModelName
		}
		return candidates[i].score > candidates[j].score
	})
	trace["candidate_scores"] = topCandidateScores(candidates, 6)
	return candidates[0].status.Provider, candidates[0].status.ModelName
}

type scoredHealthyModel struct {
	status     domain.ProviderModelStatus
	score      float64
	components map[string]any
}

type selectorSignals struct {
	budgetAction       string
	tokenPressure      string
	peerFailures       []string
	vectorMemoryCount  int
	routeResults       []domain.RouteSearchResult
	routeMemoryCount   int
	validationHasRAG   bool
	trainedMemory      bool
	retrievalTier      string
	retrievalCoverage  float64
	retrievalPacked    int
	retrievalTruncated float64
	retrievalBestScore float64
}

func (s selectorSignals) trace() map[string]any {
	return map[string]any{
		"budget_action":        s.budgetAction,
		"token_pressure":       s.tokenPressure,
		"peer_failures":        append([]string(nil), s.peerFailures...),
		"vector_memory_count":  s.vectorMemoryCount,
		"route_memory_count":   s.routeMemoryCount,
		"validation_has_rag":   s.validationHasRAG,
		"trained_memory":       s.trainedMemory,
		"retrieval_tier":       s.retrievalTier,
		"retrieval_coverage":   s.retrievalCoverage,
		"retrieval_packed":     s.retrievalPacked,
		"retrieval_truncation": s.retrievalTruncated,
		"retrieval_best_score": s.retrievalBestScore,
	}
}

type routeHistoryScore struct {
	Total          float64 `json:"total"`
	SampleCount    int     `json:"sample_count"`
	SuccessRate    float64 `json:"success_rate"`
	ReviewPassRate float64 `json:"review_pass_rate"`
	TestPassRate   float64 `json:"test_pass_rate"`
	Confidence     float64 `json:"confidence"`
	Recency        float64 `json:"recency"`
	CostEfficiency float64 `json:"cost_efficiency"`
}

func (s *ModelSelector) buildSelectorSignals(task domain.Task) selectorSignals {
	signals := selectorSignals{}
	if task.RoutingHints != nil {
		if budget, ok := task.RoutingHints["model_budget"].(map[string]any); ok {
			signals.budgetAction = stringValue(budget["action"])
		}
		signals.tokenPressure = stringValue(task.RoutingHints["token_pressure"])
		signals.peerFailures = stringSliceValue(task.RoutingHints["peer_failures"])
		if validation, ok := task.RoutingHints["validation_context"].(map[string]any); ok {
			signals.validationHasRAG = boolValue(validation["rag_required"])
		}
		if memoryContext, ok := task.RoutingHints["memory_context"].(map[string]any); ok {
			signals.vectorMemoryCount = intValue(memoryContext["vector_memory_count"])
			signals.trainedMemory = stringValue(memoryContext["trained_memory_brief"]) != ""
			if kpi, ok := memoryContext["retrieval_kpi"].(map[string]any); ok {
				signals.retrievalTier = stringValue(kpi["tier"])
				signals.retrievalCoverage = float64Value(kpi["coverage_ratio"])
				signals.retrievalPacked = intValue(kpi["packed_count"])
				signals.retrievalTruncated = float64Value(kpi["truncation_ratio"])
				signals.retrievalBestScore = float64Value(kpi["best_score"])
			}
		}
	}
	if s.routeMemory != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
		defer cancel()
		if results, err := s.routeMemory.SearchRouteMemories(ctx, task, 10); err == nil {
			signals.routeResults = results
			signals.routeMemoryCount = len(results)
		}
	}
	if s.retriever != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()
		if snapshot, err := s.retriever.Retrieve(ctx, task, 4); err == nil {
			signals.vectorMemoryCount = maxInt(signals.vectorMemoryCount, snapshot.KPI.PackedCount)
			signals.retrievalTier = strongerRetrievalTier(signals.retrievalTier, snapshot.KPI.Tier)
			signals.retrievalCoverage = maxFloat(signals.retrievalCoverage, snapshot.KPI.CoverageRatio)
			signals.retrievalPacked = maxInt(signals.retrievalPacked, snapshot.KPI.PackedCount)
			signals.retrievalTruncated = maxFloat(signals.retrievalTruncated, snapshot.KPI.TruncationRatio)
			signals.retrievalBestScore = maxFloat(signals.retrievalBestScore, snapshot.KPI.BestScore)
		}
	}
	return signals
}

func scoreRouteHistory(model domain.ProviderModelStatus, results []domain.RouteSearchResult) routeHistoryScore {
	family := inferModelFamily(model.ModelName)
	matched := make([]domain.RouteSearchResult, 0)
	for _, result := range results {
		record := result.Record
		providerMatch := strings.EqualFold(record.Provider, model.Provider)
		modelMatch := strings.EqualFold(record.ModelName, model.ModelName)
		familyMatch := family != "" && inferModelFamily(record.ModelName) == family
		if providerMatch && (modelMatch || familyMatch) {
			matched = append(matched, result)
		}
	}
	if len(matched) == 0 {
		return routeHistoryScore{}
	}
	var success, review, tests, confidence, recency, cost float64
	for _, result := range matched {
		record := result.Record
		if record.Success {
			success += 1
		}
		if record.ReviewPassed {
			review += 1
		}
		if record.TestsPassed {
			tests += 1
		}
		confidence += clamp(result.Similarity, 0, 1) * clamp(record.Confidence, 0, 1)
		recency += recencyScore(record.UpdatedAt)
		cost += costEfficiencyScore(record.CostEstimate)
	}
	totalCount := float64(len(matched))
	score := routeHistoryScore{
		SampleCount:    len(matched),
		SuccessRate:    success / totalCount,
		ReviewPassRate: review / totalCount,
		TestPassRate:   tests / totalCount,
		Confidence:     confidence / totalCount,
		Recency:        recency / totalCount,
		CostEfficiency: cost / totalCount,
	}
	score.Total = score.SuccessRate*2.6 + score.ReviewPassRate*1.3 + score.TestPassRate*1.2 + score.Confidence*1.0 + score.Recency*0.7 + score.CostEfficiency*0.6
	return score
}

func scoreBudgetFit(model domain.ProviderModelStatus, signals selectorSignals) float64 {
	action := strings.ToLower(signals.budgetAction)
	provider := strings.ToLower(model.Provider)
	family := inferModelFamily(model.ModelName)
	if action == "reduce" || action == "tighten" {
		if provider == "ai_kernel" || provider == "local" || provider == "ollama" {
			return 1.8
		}
		if strings.Contains(strings.ToLower(model.ModelName), "mini") || strings.Contains(strings.ToLower(model.ModelName), "small") {
			return 1.0
		}
		if family == "gpt" || family == "claude" || family == "gemini" {
			return -0.8
		}
	}
	if action == "expand" || action == "increase" {
		if family == "gpt" || family == "claude" || family == "gemini" {
			return 0.8
		}
	}
	return 0
}

func scoreRetrievalFit(model domain.ProviderModelStatus, policy modelSelectionPolicy, signals selectorSignals) float64 {
	heavyRetrieval := signals.vectorMemoryCount >= 3 || signals.validationHasRAG || signals.trainedMemory || signals.retrievalTier == "high" || signals.retrievalCoverage >= 0.55
	if !heavyRetrieval {
		return 0
	}
	name := strings.ToLower(model.ModelName)
	family := inferModelFamily(model.ModelName)
	score := 0.2
	if strings.Contains(name, "mini") {
		score -= 0.5
	}
	if policy.reason == "analysis_review_path" || policy.reason == "research_path" || policy.reason == "high_risk_or_high_complexity" {
		if family == "gpt" || family == "claude" || family == "gemini" || family == "qwen" || family == "mistral" {
			score += 0.9
		}
	}
	if signals.retrievalTier == "high" {
		score += 0.4
	}
	if signals.retrievalCoverage >= 0.6 {
		score += 0.3
	}
	if signals.retrievalTruncated >= 0.35 {
		score += 0.15
	}
	if signals.retrievalBestScore > 0 && signals.retrievalBestScore < 0.45 {
		score -= 0.1
	}
	return score
}

func scoreFailurePenalty(model domain.ProviderModelStatus, signals selectorSignals) float64 {
	if len(signals.peerFailures) == 0 {
		return 0
	}
	provider := strings.ToLower(model.Provider)
	name := strings.ToLower(model.ModelName)
	for _, failure := range signals.peerFailures {
		value := strings.ToLower(failure)
		if value == provider || value == name || strings.Contains(value, provider) || strings.Contains(value, name) {
			return 2.2
		}
	}
	return 0
}

func topCandidateScores(candidates []scoredHealthyModel, limit int) []map[string]any {
	if limit > len(candidates) {
		limit = len(candidates)
	}
	out := make([]map[string]any, 0, limit)
	for i := 0; i < limit; i++ {
		candidate := candidates[i]
		out = append(out, map[string]any{
			"provider":   candidate.status.Provider,
			"model":      candidate.status.ModelName,
			"score":      candidate.score,
			"status":     candidate.status.Status,
			"is_default": candidate.status.IsDefault,
			"components": candidate.components,
		})
	}
	return out
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	default:
		return strings.TrimSpace(strconv.FormatBool(boolValue(value)))
	}
}

func stringSliceValue(value any) []string {
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if item == nil {
				continue
			}
			text := stringValue(item)
			if text != "" {
				out = append(out, text)
			}
		}
		return out
	default:
		return nil
	}
}

func intValue(value any) int {
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
	case string:
		parsed, err := strconv.Atoi(strings.TrimSpace(typed))
		if err == nil {
			return parsed
		}
	}
	return 0
}

func strongerRetrievalTier(current string, candidate string) string {
	rank := map[string]int{"": 0, "empty": 0, "low": 1, "medium": 2, "high": 3}
	if rank[candidate] > rank[current] {
		return candidate
	}
	return current
}

func float64Value(value any) float64 {
	switch typed := value.(type) {
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
	case json.Number:
		parsed, err := typed.Float64()
		if err == nil {
			return parsed
		}
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		if err == nil {
			return parsed
		}
	}
	return 0
}

func maxFloat(left, right float64) float64 {
	if left >= right {
		return left
	}
	return right
}

func boolValue(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		parsed, err := strconv.ParseBool(strings.TrimSpace(typed))
		return err == nil && parsed
	default:
		return false
	}
}

func clamp(value, minValue, maxValue float64) float64 {
	if value < minValue {
		return minValue
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func recencyScore(ts time.Time) float64 {
	if ts.IsZero() {
		return 0.35
	}
	age := time.Since(ts)
	switch {
	case age <= 24*time.Hour:
		return 1
	case age <= 7*24*time.Hour:
		return 0.8
	case age <= 30*24*time.Hour:
		return 0.55
	default:
		return 0.3
	}
}

func costEfficiencyScore(cost float64) float64 {
	if cost <= 0 {
		return 0.8
	}
	switch {
	case cost <= 0.02:
		return 1
	case cost <= 0.08:
		return 0.75
	case cost <= 0.2:
		return 0.45
	default:
		return 0.2
	}
}

func scoreHealthyModel(model domain.ProviderModelStatus, policy modelSelectionPolicy) int {
	score := 0
	for index, preferred := range policy.models {
		if strings.EqualFold(model.ModelName, preferred) {
			score += 1000 - index*25
			break
		}
	}
	family := inferModelFamily(model.ModelName)
	if metadataFamily, _ := model.Metadata["model_family"].(string); strings.TrimSpace(metadataFamily) != "" {
		family = metadataFamily
	}
	for index, preferred := range policy.families {
		if strings.EqualFold(family, preferred) {
			score += 700 - index*20
			break
		}
	}
	switch model.Status {
	case "ready":
		score += 150
	case "verification_pending":
		score += 25
	case "validation_failed", "missing", "disabled":
		score -= 500
	default:
		score += 35
	}
	if model.IsDefault {
		score += 60
	}
	if strings.EqualFold(model.ModelName, policy.targetModel) {
		score += 80
	}
	return score
}

func (s *ModelSelector) supportLanes(task domain.Task, choice domain.ModelSelection, policy modelSelectionPolicy) []domain.SupportLane {
	if !s.shouldAttachAIKernelSupport(task, choice, policy) {
		return nil
	}
	modelName, ok := s.resolveAIKernelSupportModel(task.Type)
	if !ok {
		return nil
	}
	roles := []string{"helper", "fallback"}
	switch task.Type {
	case domain.TaskTypePlan, domain.TaskTypeReview, domain.TaskTypeResearch:
		roles = append(roles, "parallel")
	}
	lanes := make([]domain.SupportLane, 0, len(roles))
	for _, role := range roles {
		lanes = append(lanes, domain.SupportLane{
			Provider:           "ai_kernel",
			ModelName:          modelName,
			Role:               role,
			MaxComplexity:      domain.ComplexityMedium,
			Capabilities:       aiKernelSupportCapabilities(task.Type, role),
			SupportedTaskTypes: aiKernelSupportTaskTypes(task.Type),
			Reason:             aiKernelSupportReason(task.Type, role),
		})
	}
	return lanes
}

func (s *ModelSelector) shouldAttachAIKernelSupport(task domain.Task, choice domain.ModelSelection, policy modelSelectionPolicy) bool {
	if strings.EqualFold(choice.Provider, "ai_kernel") || strings.EqualFold(policy.targetProvider, "ai_kernel") {
		return false
	}
	if s == nil || s.registry == nil {
		return false
	}
	if len(s.registry.HealthyModels("ai_kernel")) == 0 {
		return false
	}
	if policy.cloudRequired {
		return true
	}
	switch task.Type {
	case domain.TaskTypePlan, domain.TaskTypeReview, domain.TaskTypeResearch:
		return true
	default:
		return false
	}
}

func (s *ModelSelector) resolveAIKernelSupportModel(taskType domain.TaskType) (string, bool) {
	if s == nil || s.registry == nil {
		return "", false
	}
	preferred := []string{modelQwenCoder}
	if taskType == domain.TaskTypeDocs {
		preferred = []string{modelLocalSmall, modelQwenCoder}
	}
	models := s.registry.HealthyModels("ai_kernel")
	for _, candidate := range preferred {
		for _, model := range models {
			if strings.EqualFold(model.ModelName, candidate) {
				return model.ModelName, true
			}
		}
	}
	for _, model := range models {
		if model.Available && strings.EqualFold(strings.TrimSpace(model.VerificationStatus), "confirmed") {
			return model.ModelName, true
		}
	}
	return "", false
}

func aiKernelSupportCapabilities(taskType domain.TaskType, role string) []string {
	capabilities := []string{"docs", "code", "fix", "test"}
	switch taskType {
	case domain.TaskTypePlan, domain.TaskTypeReview:
		capabilities = append(capabilities, "plan", "review")
	case domain.TaskTypeResearch:
		capabilities = append(capabilities, "research", "review")
	}
	if role == "parallel" {
		capabilities = append(capabilities, "analysis")
	}
	return dedupeStrings(capabilities)
}

func aiKernelSupportTaskTypes(taskType domain.TaskType) []domain.TaskType {
	values := []domain.TaskType{domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeTest, domain.TaskTypeDocs}
	switch taskType {
	case domain.TaskTypePlan, domain.TaskTypeReview, domain.TaskTypeResearch:
		values = append(values, taskType)
	}
	return dedupeTaskTypes(values)
}

func aiKernelSupportReason(taskType domain.TaskType, role string) string {
	switch role {
	case "fallback":
		return "local ai_kernel fallback is available if the primary cloud route degrades"
	case "parallel":
		return "local ai_kernel can parallelize repo-local drafting, summarization, and verification subtasks"
	default:
		return "local ai_kernel can assist the primary route with repo-local drafting, verification, and synthesis"
	}
}

func dedupeTaskTypes(values []domain.TaskType) []domain.TaskType {
	if len(values) == 0 {
		return nil
	}
	seen := make(map[domain.TaskType]struct{}, len(values))
	result := make([]domain.TaskType, 0, len(values))
	for _, value := range values {
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func (s *ModelSelector) providerPreference(path string) []string {
	known := []string{"ai_kernel", "local", "mistral", "codexsale", "openai", "mimo", "antigravity"}
	orderByPath := map[string][]string{
		"cloud":    {s.preferredCloudProvider(), "codexsale", "openai", "mistral", "mimo", "antigravity", "ai_kernel", "local"},
		"code":     {"ai_kernel", "local", "antigravity", "codexsale", "openai", "mistral", "mimo"},
		"analysis": {"mistral", "codexsale", "openai", "mimo", "antigravity", "ai_kernel", "local"},
		"docs":     {"ai_kernel", "local", "mistral", "mimo", "antigravity", "codexsale", "openai"},
		"research": {"mistral", "mimo", "codexsale", "openai", "antigravity", "ai_kernel", "local"},
		"default":  {"local", "ai_kernel", "mistral", "codexsale", "openai", "mimo", "antigravity"},
	}
	ordered := append([]string(nil), orderByPath[path]...)
	if s != nil && s.registry != nil {
		for provider := range s.registry.Configs() {
			if strings.EqualFold(provider, "local") || strings.EqualFold(provider, "ai_kernel") {
				continue
			}
			ordered = appendIfMissingPreserveOrder(ordered, provider)
		}
	}
	for _, provider := range known {
		ordered = appendIfMissingPreserveOrder(ordered, provider)
	}
	return ordered
}

func appendIfMissingPreserveOrder(items []string, value string) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return items
	}
	for _, item := range items {
		if strings.EqualFold(item, value) {
			return items
		}
	}
	return append(items, value)
}

func dedupeStrings(items []string) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		out = appendIfMissingPreserveOrder(out, item)
	}
	return out
}

func shouldEscalateToCloud(task domain.Task, complexity domain.Complexity, risk RiskEvaluation) bool {
	return complexity == domain.ComplexityCritical || complexity == domain.ComplexityHigh || task.Priority == domain.PriorityCritical || risk.HighRisk
}

func containsAny(text string, keywords []string) bool {
	for _, keyword := range keywords {
		if strings.Contains(text, keyword) {
			return true
		}
	}
	return false
}
