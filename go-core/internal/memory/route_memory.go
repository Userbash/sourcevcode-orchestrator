package memory

import (
	"context"
	"fmt"
	"hash/fnv"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const (
	defaultRouteMemoryCandidateCap = 96
	defaultRouteMemoryTopK         = 12
	minimumRouteHistorySamples     = 2
)

func (m *Manager) BuildTaskSignature(ctx context.Context, task domain.Task) domain.TaskSignature {
	capability := strings.TrimSpace(task.RequiredCapability)
	if capability == "" {
		capability = strings.TrimSpace(string(task.Type))
	}
	description := strings.TrimSpace(task.Input.Description)
	constraints := append([]string(nil), task.Input.Constraints...)
	files := uniqueSortedStrings(task.Input.Files)
	modules := routeModules(files)
	extensions := routeExtensions(files)
	parts := []string{
		string(task.Type),
		capability,
		description,
		"files: " + strings.Join(files, " "),
		"modules: " + strings.Join(modules, " "),
		"extensions: " + strings.Join(extensions, " "),
		"constraints: " + strings.Join(constraints, " "),
		"acceptance: " + strings.Join(task.Input.AcceptanceCriteria, " "),
	}
	normalized := normalizeVectorText(strings.Join(parts, "\n"))
	signature := domain.TaskSignature{
		TaskType:        task.Type,
		Capability:      capability,
		Complexity:      normalizeComplexity(task.Complexity),
		Project:         strings.TrimSpace(task.Context.Project),
		RepoPath:        strings.TrimSpace(task.Context.RepoPath),
		RepoFingerprint: strings.TrimSpace(task.RepoFingerprint),
		Branch:          normalizeBranch(task.Context.Branch),
		Files:           files,
		Modules:         modules,
		Extensions:      extensions,
		Constraints:     uniqueSortedStrings(constraints),
		Description:     description,
		NormalizedText:  normalized,
		CreatedAt:       time.Now().UTC(),
	}
	if normalized != "" {
		signature.Embedding = m.embedText(ctx, normalized, defaultVectorDims)
	}
	signature.Key = routeSignatureKey(signature)
	return signature
}

func (m *Manager) RecordRouteOutcome(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, result domain.AgentResult, latency time.Duration) error {
	if m == nil || m.store == nil {
		return nil
	}
	now := time.Now().UTC()
	capability := firstNonEmpty(strings.TrimSpace(acceptance.Capability), strings.TrimSpace(task.RequiredCapability), strings.TrimSpace(string(task.Type)))
	complexity := normalizeComplexity(firstNonEmptyComplexity(acceptance.Complexity, task.Complexity))
	signature := m.BuildTaskSignature(ctx, task)
	record := domain.RouteMemoryRecord{
		RouteID:         routeMemoryID(task, acceptance, result),
		SessionID:       normalizeSessionID(task.SessionID),
		TaskID:          strings.TrimSpace(task.ID),
		ParentTaskID:    strings.TrimSpace(task.ParentTaskID),
		RootTaskID:      routeRootTaskID(task),
		TaskSignature:   signature,
		TaskType:        task.Type,
		Capability:      capability,
		Complexity:      complexity,
		Project:         strings.TrimSpace(task.Context.Project),
		RepoPath:        strings.TrimSpace(task.Context.RepoPath),
		RepoFingerprint: strings.TrimSpace(task.RepoFingerprint),
		Branch:          normalizeBranch(task.Context.Branch),
		AgentID:         firstNonEmpty(result.AgentID, acceptance.AgentID),
		Provider:        firstNonEmpty(result.Provider, acceptance.Provider),
		ModelName:       firstNonEmpty(result.ModelName, acceptance.ModelName),
		PlanMode:        routePlanMode(task),
		Success:         result.Status != domain.TaskStatusFailed,
		Confidence:      clamp01(result.Confidence),
		LatencyMS:       latency.Milliseconds(),
		ReviewPassed:    routeReviewPassed(task, result),
		TestsPassed:     routeTestsPassed(result),
		CostEstimate:    routeCostEstimate(task, result),
		ErrorCount:      len(result.Errors),
		Summary:         truncateText(firstNonEmpty(strings.TrimSpace(result.Output.Summary), strings.Join(result.Errors, "; ")), 512),
		Embedding:       append([]float64(nil), signature.Embedding...),
		Metadata: map[string]any{
			"files":               append([]string(nil), signature.Files...),
			"modules":             append([]string(nil), signature.Modules...),
			"extensions":          append([]string(nil), signature.Extensions...),
			"constraints":         append([]string(nil), signature.Constraints...),
			"acceptance_criteria": append([]string(nil), task.Input.AcceptanceCriteria...),
			"result_files":        append([]string(nil), result.Output.FilesChanged...),
			"commands_run":        append([]string(nil), result.Output.CommandsRun...),
			"task_signature_key":  signature.Key,
		},
		CreatedAt: now,
		UpdatedAt: now,
	}
	return m.store.UpsertRouteMemories(ctx, []domain.RouteMemoryRecord{record})
}

func (m *Manager) SearchRouteMemories(ctx context.Context, task domain.Task, limit int) ([]domain.RouteSearchResult, error) {
	if m == nil || m.store == nil {
		return nil, nil
	}
	signature := m.BuildTaskSignature(ctx, task)
	capability := firstNonEmpty(signature.Capability, strings.TrimSpace(task.RequiredCapability), strings.TrimSpace(string(task.Type)))
	candidates, err := m.store.ListRouteMemories(ctx, strings.TrimSpace(task.Context.Project), strings.TrimSpace(task.RepoFingerprint), capability, defaultRouteMemoryCandidateCap)
	if err != nil {
		return nil, err
	}
	results := make([]domain.RouteSearchResult, 0, len(candidates))
	for _, candidate := range candidates {
		similarity := routeSimilarity(signature, candidate)
		if similarity < 0.18 {
			continue
		}
		results = append(results, domain.RouteSearchResult{Record: candidate, Similarity: round2(similarity)})
	}
	sort.Slice(results, func(i, j int) bool {
		if results[i].Similarity == results[j].Similarity {
			return results[i].Record.UpdatedAt.After(results[j].Record.UpdatedAt)
		}
		return results[i].Similarity > results[j].Similarity
	})
	if limit <= 0 {
		limit = defaultRouteMemoryTopK
	}
	if len(results) > limit {
		results = results[:limit]
	}
	return results, nil
}

func (m *Manager) RouteHistoryScore(ctx context.Context, task domain.Task, candidate domain.AgentInfo) (float64, domain.RouteScoreBreakdown, error) {
	results, err := m.SearchRouteMemories(ctx, task, defaultRouteMemoryTopK)
	if err != nil {
		return 0, domain.RouteScoreBreakdown{}, err
	}
	agentID := strings.TrimSpace(candidate.ID)
	provider := strings.TrimSpace(candidate.Provider)
	modelName := strings.TrimSpace(candidate.ModelName)
	filtered := make([]domain.RouteSearchResult, 0, len(results))
	for _, result := range results {
		record := result.Record
		if !strings.EqualFold(strings.TrimSpace(record.AgentID), agentID) {
			continue
		}
		if provider != "" && !strings.EqualFold(strings.TrimSpace(record.Provider), provider) {
			continue
		}
		if modelName != "" && !strings.EqualFold(strings.TrimSpace(record.ModelName), modelName) {
			continue
		}
		filtered = append(filtered, result)
	}
	breakdown := routeScoreBreakdown(filtered)
	if breakdown.Samples < minimumRouteHistorySamples {
		breakdown.HistoricalScore = 0
		return 0, breakdown, nil
	}
	return breakdown.HistoricalScore, breakdown, nil
}

func routeScoreBreakdown(results []domain.RouteSearchResult) domain.RouteScoreBreakdown {
	breakdown := domain.RouteScoreBreakdown{Samples: len(results)}
	if len(results) == 0 {
		return breakdown
	}
	var (
		successWeighted float64
		confidenceSum   float64
		reviewSum       float64
		testSum         float64
		recencySum      float64
		costSum         float64
		costWeight      float64
		weightSum       float64
	)
	for _, result := range results {
		weight := 0.55 + 0.45*clamp01(result.Similarity)
		if result.Record.Success {
			successWeighted += weight
		}
		confidenceSum += clamp01(result.Record.Confidence) * weight
		if result.Record.ReviewPassed {
			reviewSum += weight
		}
		if result.Record.TestsPassed {
			testSum += weight
		}
		recencySum += recencyScore(result.Record.UpdatedAt) * weight
		if result.Record.CostEstimate > 0 {
			costSum += result.Record.CostEstimate * weight
			costWeight += weight
		}
		weightSum += weight
	}
	if weightSum <= 0 {
		return breakdown
	}
	breakdown.SuccessRate = round2(clamp01(successWeighted / weightSum))
	breakdown.ConfidenceScore = round2(clamp01(confidenceSum / weightSum))
	breakdown.ReviewPassRate = round2(clamp01(reviewSum / weightSum))
	breakdown.TestPassRate = round2(clamp01(testSum / weightSum))
	breakdown.RecencyScore = round2(clamp01(recencySum / weightSum))
	breakdown.CostEfficiency = round2(costEfficiencyScore(costSum, costWeight))
	history := 0.35*breakdown.SuccessRate + 0.20*breakdown.ConfidenceScore + 0.15*breakdown.ReviewPassRate + 0.15*breakdown.TestPassRate + 0.10*breakdown.RecencyScore + 0.05*breakdown.CostEfficiency
	breakdown.HistoricalScore = round2(clamp01(history))
	return breakdown
}

func routeSimilarity(signature domain.TaskSignature, record domain.RouteMemoryRecord) float64 {
	textSimilarity := cosineSimilarity(signature.Embedding, record.Embedding)
	if textSimilarity == 0 {
		textSimilarity = cosineSimilarity(signature.Embedding, record.TaskSignature.Embedding)
	}
	fileOverlap, _ := termOverlapScore(signature.Files, record.TaskSignature.Files)
	moduleOverlap, _ := termOverlapScore(signature.Modules, record.TaskSignature.Modules)
	extensionOverlap, _ := termOverlapScore(signature.Extensions, record.TaskSignature.Extensions)
	constraintOverlap, _ := termOverlapScore(signature.Constraints, record.TaskSignature.Constraints)
	capabilityScore := 0.0
	if strings.EqualFold(signature.Capability, record.Capability) {
		capabilityScore = 1.0
	}
	typeScore := 0.0
	if signature.TaskType == record.TaskType {
		typeScore = 1.0
	}
	complexityScore := 0.35
	if normalizeComplexity(signature.Complexity) == normalizeComplexity(record.Complexity) {
		complexityScore = 1.0
	}
	scopeScore := routeScopeScore(signature, record)
	return clamp01(textSimilarity*0.40 + fileOverlap*0.10 + moduleOverlap*0.10 + extensionOverlap*0.05 + constraintOverlap*0.10 + capabilityScore*0.10 + typeScore*0.08 + complexityScore*0.04 + scopeScore*0.03)
}

func routeScopeScore(signature domain.TaskSignature, record domain.RouteMemoryRecord) float64 {
	score := 0.0
	if signature.Project != "" && strings.EqualFold(signature.Project, record.Project) {
		score += 0.45
	}
	if signature.RepoFingerprint != "" && strings.EqualFold(signature.RepoFingerprint, record.RepoFingerprint) {
		score += 0.35
	}
	if signature.Branch != "" && signature.Branch == record.Branch {
		score += 0.20
	}
	return clamp01(score)
}

func routePlanMode(task domain.Task) string {
	if strings.EqualFold(strings.TrimSpace(fmt.Sprint(task.RoutingHints["route_mode"])), "orchestrator") {
		return "orchestrator"
	}
	if len(task.Dependencies) > 0 || task.ParentTaskID != "" {
		return "parallel"
	}
	return "direct"
}

func routeReviewPassed(task domain.Task, result domain.AgentResult) bool {
	if task.Type == domain.TaskTypeReview {
		return result.Status != domain.TaskStatusFailed
	}
	artifacts := asMap(result.Output.Artifacts)
	if value, ok := artifacts["review_passed"].(bool); ok {
		return value
	}
	status := normalizeVectorText(fmt.Sprint(artifacts["review_status"]))
	if status == "approved" || status == "pass" || status == "passed" {
		return true
	}
	return len(result.Errors) == 0 && result.Status != domain.TaskStatusFailed
}

func routeTestsPassed(result domain.AgentResult) bool {
	if len(result.Output.TestResults) == 0 {
		artifacts := asMap(result.Output.Artifacts)
		if value, ok := artifacts["tests_passed"].(bool); ok {
			return value
		}
		return result.Status != domain.TaskStatusFailed && len(result.Errors) == 0
	}
	passed := 0
	total := 0
	for _, testResult := range result.Output.TestResults {
		total++
		status := normalizeVectorText(fmt.Sprint(testResult["status"]))
		if status == "pass" || status == "passed" || status == "ok" || status == "success" {
			passed++
		}
	}
	if total == 0 {
		return false
	}
	return passed == total
}

func routeCostEstimate(task domain.Task, result domain.AgentResult) float64 {
	artifacts := asMap(result.Output.Artifacts)
	usage := asMap(artifacts["usage"])
	if value := extractFloat(usage["estimated_cost"]); value > 0 {
		return round2(value)
	}
	if value := extractFloat(artifacts["estimated_cost"]); value > 0 {
		return round2(value)
	}
	if task.EstimatedCost > 0 {
		return round2(task.EstimatedCost)
	}
	return 0
}

func costEfficiencyScore(weightedCost float64, costWeight float64) float64 {
	if weightedCost <= 0 || costWeight <= 0 {
		return 0.6
	}
	avgCost := weightedCost / costWeight
	if avgCost <= 0 {
		return 0.6
	}
	return clamp01(1.0 / (1.0 + avgCost))
}

func routeSignatureKey(signature domain.TaskSignature) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte(strings.Join([]string{
		string(signature.TaskType),
		signature.Capability,
		string(signature.Complexity),
		signature.Project,
		signature.RepoFingerprint,
		signature.NormalizedText,
		strings.Join(signature.Files, ","),
		strings.Join(signature.Modules, ","),
		strings.Join(signature.Extensions, ","),
		strings.Join(signature.Constraints, ","),
	}, "|")))
	return fmt.Sprintf("sig_%x", h.Sum64())
}

func routeMemoryID(task domain.Task, acceptance domain.TaskAcceptance, result domain.AgentResult) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte(strings.Join([]string{
		firstNonEmpty(task.ID, result.TaskID),
		firstNonEmpty(result.AgentID, acceptance.AgentID),
		firstNonEmpty(result.Provider, acceptance.Provider),
		firstNonEmpty(result.ModelName, acceptance.ModelName),
		fmt.Sprint(result.CompletedAt.UnixNano()),
	}, "|")))
	return fmt.Sprintf("route_%x", h.Sum64())
}

func routeRootTaskID(task domain.Task) string {
	if root := strings.TrimSpace(fmt.Sprint(task.RoutingHints["root_task_id"])); root != "" {
		return root
	}
	return firstNonEmpty(task.ParentTaskID, task.ID)
}

func routeModules(files []string) []string {
	modules := make([]string, 0, len(files))
	for _, file := range files {
		trimmed := strings.Trim(strings.TrimSpace(file), "/")
		if trimmed == "" {
			continue
		}
		parts := strings.Split(trimmed, "/")
		if len(parts) > 1 {
			modules = append(modules, strings.ToLower(parts[0]))
		}
	}
	return uniqueSortedStrings(modules)
}

func routeExtensions(files []string) []string {
	extensions := make([]string, 0, len(files))
	for _, file := range files {
		ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(strings.TrimSpace(file)), "."))
		if ext != "" {
			extensions = append(extensions, ext)
		}
	}
	return uniqueSortedStrings(extensions)
}

func uniqueSortedStrings(values []string) []string {
	if len(values) == 0 {
		return nil
	}
	seen := map[string]struct{}{}
	out := make([]string, 0, len(values))
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed == "" {
			continue
		}
		normalized := strings.ToLower(trimmed)
		if _, ok := seen[normalized]; ok {
			continue
		}
		seen[normalized] = struct{}{}
		out = append(out, normalized)
	}
	sort.Strings(out)
	return out
}

func normalizeComplexity(complexity domain.Complexity) domain.Complexity {
	switch complexity {
	case domain.ComplexityLow, domain.ComplexityMedium, domain.ComplexityHigh, domain.ComplexityCritical:
		return complexity
	default:
		return domain.ComplexityMedium
	}
}

func firstNonEmptyComplexity(values ...domain.Complexity) domain.Complexity {
	for _, value := range values {
		if strings.TrimSpace(string(value)) != "" {
			return value
		}
	}
	return domain.ComplexityMedium
}
