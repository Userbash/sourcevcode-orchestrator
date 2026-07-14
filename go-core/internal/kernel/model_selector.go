package kernel

import (
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const (
	modelQwenCoder  = "qwen2.5:32b-instruct-q4_k_m"
	modelLocalSmall = "qwen-2.5-7b-instruct"
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
	registry *ProviderModelRegistry
}

func NewModelSelector(registry *ProviderModelRegistry) *ModelSelector {
	return &ModelSelector{registry: registry}
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
		},
	}
	targetProvider := "local"
	targetModel := modelLocalSmall
	reason := "policy_default"
	if shouldEscalateToCloud(task, complexity, risk) {
		targetProvider = "openai"
		targetModel = modelOpenAIHigh
		choice.RequiresSecondaryReview = true
		reason = "high_risk_or_high_complexity"
	} else {
		switch task.Type {
		case domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeTest:
			targetProvider = "ai_kernel"
			targetModel = modelQwenCoder
			reason = "local_code_path"
		case domain.TaskTypePlan, domain.TaskTypeReview:
			targetProvider = "mistral"
			targetModel = modelMistral
			choice.RequiresSecondaryReview = task.Type == domain.TaskTypeReview
			reason = "analysis_review_path"
		case domain.TaskTypeDocs:
			targetProvider = "ai_kernel"
			targetModel = modelLocalSmall
			reason = "local_docs_path"
		case domain.TaskTypeResearch:
			targetProvider = "mistral"
			targetModel = modelMistral
			reason = "research_path"
		}
	}
	choice.Provider, choice.ModelName = s.resolveAvailableModel(targetProvider, targetModel, task, complexity, risk)
	choice.Reason = reason
	choice.SelectionTrace["target_provider"] = targetProvider
	choice.SelectionTrace["target_model"] = targetModel
	choice.SelectionTrace["resolved_provider"] = choice.Provider
	choice.SelectionTrace["resolved_model"] = choice.ModelName
	return choice
}

func (s *ModelSelector) resolveAvailableModel(targetProvider, targetModel string, task domain.Task, complexity domain.Complexity, risk RiskEvaluation) (string, string) {
	_ = task
	_ = complexity
	_ = risk
	if s == nil || s.registry == nil {
		return targetProvider, targetModel
	}
	tryProvider := func(provider, preferred string) (string, string, bool) {
		models := s.registry.HealthyModels(provider)
		if len(models) == 0 {
			return "", "", false
		}
		preferred = strings.TrimSpace(preferred)
		if preferred != "" {
			for _, model := range models {
				if strings.EqualFold(model.ModelName, preferred) {
					return provider, model.ModelName, true
				}
			}
		}
		for _, model := range models {
			if model.IsDefault {
				return provider, model.ModelName, true
			}
		}
		return provider, models[0].ModelName, true
	}
	if provider, model, ok := tryProvider(targetProvider, targetModel); ok {
		return provider, model
	}
	fallbacks := []struct {
		provider string
		model    string
	}{
		{provider: "ai_kernel", model: modelQwenCoder},
		{provider: "local", model: modelLocalSmall},
		{provider: "mistral", model: modelMistral},
		{provider: "openai", model: modelOpenAIHigh},
		{provider: "mimo", model: ""},
		{provider: "antigravity", model: ""},
	}
	seen := map[string]struct{}{strings.ToLower(strings.TrimSpace(targetProvider)): {}}
	for _, fallback := range fallbacks {
		providerKey := strings.ToLower(strings.TrimSpace(fallback.provider))
		if providerKey == "" {
			continue
		}
		if _, exists := seen[providerKey]; exists {
			continue
		}
		seen[providerKey] = struct{}{}
		if provider, model, ok := tryProvider(fallback.provider, fallback.model); ok {
			return provider, model
		}
	}
	return targetProvider, targetModel
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
