package kernel

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestModelSelectorFallsBackToNonGPTHealthyCloudModel(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai":    {Provider: "openai", DefaultModel: "gpt-5.5", BaseURL: "https://api.openai.example/v1", APIKey: "secret", RequireKey: true},
		"codexsale": {Provider: "codexsale", DefaultModel: "claude-sonnet-4-6", BaseURL: "https://codex.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["openai"] = domain.ProviderCatalogSnapshot{
		Provider:           "openai",
		Status:             "unavailable",
		Available:          false,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models: []domain.ProviderModelStatus{{
			Provider:   "openai",
			ModelName:  "gpt-5.5",
			Available:  false,
			Status:     "validation_failed",
			ObservedAt: now,
			Metadata:   map[string]any{"model_family": "gpt"},
		}},
	}
	registry.snapshots["codexsale"] = domain.ProviderCatalogSnapshot{
		Provider:           "codexsale",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models: []domain.ProviderModelStatus{{
			Provider:           "codexsale",
			ModelName:          "claude-sonnet-4-6",
			Available:          true,
			Status:             "ready",
			VerificationStatus: "confirmed",
			TransportStatus:    "transport_verified",
			IsDefault:          true,
			ObservedAt:         now,
			Metadata:           map[string]any{"model_family": "claude"},
		}},
	}

	selector := NewModelSelector(registry)
	selection := selector.Select(domain.Task{
		Type:     domain.TaskTypeReview,
		Priority: domain.PriorityCritical,
		Input: domain.TaskInput{
			Description: "Review a production auth migration for security regressions",
		},
	})

	if selection.Provider != "codexsale" {
		t.Fatalf("provider=%q want codexsale", selection.Provider)
	}
	if selection.ModelName != "claude-sonnet-4-6" {
		t.Fatalf("model=%q want claude-sonnet-4-6", selection.ModelName)
	}
	if selection.Reason != "high_risk_or_high_complexity" {
		t.Fatalf("reason=%q", selection.Reason)
	}
}

func TestModelSelectorUsesHealthySpecialistModelForCodeWork(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"ai_kernel": {Provider: "ai_kernel", DefaultModel: modelQwenCoder, BaseURL: "http://kernel.example/v1"},
		"local":     {Provider: "local", DefaultModel: modelLocalSmall, BaseURL: "http://local.example/v1"},
	})
	registry.snapshots["ai_kernel"] = domain.ProviderCatalogSnapshot{
		Provider:           "ai_kernel",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models: []domain.ProviderModelStatus{
			{Provider: "ai_kernel", ModelName: "deepseek-coder", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", ObservedAt: now, Metadata: map[string]any{"model_family": "deepseek"}},
			{Provider: "ai_kernel", ModelName: modelQwenCoder, Available: false, Status: "verification_pending", VerificationStatus: "verifying", TransportStatus: "retryable_failure", ObservedAt: now, Metadata: map[string]any{"model_family": "gemma"}},
		},
	}

	selector := NewModelSelector(registry)
	selection := selector.Select(domain.Task{
		Type:     domain.TaskTypeCode,
		Priority: domain.PriorityNormal,
		Input: domain.TaskInput{
			Description: "Implement API pagination",
		},
	})

	if selection.Provider != "ai_kernel" {
		t.Fatalf("provider=%q want ai_kernel", selection.Provider)
	}
	if selection.ModelName != "deepseek-coder" {
		t.Fatalf("model=%q want deepseek-coder", selection.ModelName)
	}
	traceFamilies, ok := selection.SelectionTrace["preferred_families"].([]string)
	if !ok || len(traceFamilies) == 0 || traceFamilies[0] != "gemma" {
		t.Fatalf("unexpected preferred families trace: %#v", selection.SelectionTrace["preferred_families"])
	}
}

func TestModelSelectorUsesRouteHistoryToOverrideDefaultCodePreference(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"ai_kernel":   {Provider: "ai_kernel", DefaultModel: modelQwenCoder, BaseURL: "http://kernel.example/v1"},
		"antigravity": {Provider: "antigravity", DefaultModel: "claude-sonnet-4-6", BaseURL: "https://antigravity.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["ai_kernel"] = domain.ProviderCatalogSnapshot{
		Provider:           "ai_kernel",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "ai_kernel", ModelName: modelQwenCoder, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "gemma"}}},
	}
	registry.snapshots["antigravity"] = domain.ProviderCatalogSnapshot{
		Provider:           "antigravity",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "antigravity", ModelName: "claude-sonnet-4-6", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "claude"}}},
	}

	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("OpenStore error: %v", err)
	}
	selector := NewModelSelector(registry)
	selector.AttachMemoryManager(memory.NewManager(store))
	task := domain.Task{Type: domain.TaskTypeCode, Priority: domain.PriorityHigh, Input: domain.TaskInput{Description: "Refactor selector route scoring for code tasks"}}
	for i := 0; i < 3; i++ {
		acceptance := domain.TaskAcceptance{
			TaskID:     task.ID,
			Status:     domain.TaskStatusAccepted,
			AgentID:    "agent-antigravity",
			Complexity: domain.ComplexityHigh,
			Reason:     "historically strong code route",
			Capability: "code",
			Provider:   "antigravity",
			ModelName:  "claude-sonnet-4-6",
			AcceptedAt: time.Now().Add(-time.Duration(i) * 6 * time.Hour),
		}
		result := domain.AgentResult{
			TaskID:      task.ID,
			AgentID:     "agent-antigravity",
			Status:      domain.TaskStatusCompleted,
			Confidence:  0.95,
			Provider:    "antigravity",
			ModelName:   "claude-sonnet-4-6",
			CompletedAt: time.Now().Add(-time.Duration(i) * 6 * time.Hour),
			Output: domain.ResultOutput{
				Summary: "Completed selector route scoring improvements",
				Artifacts: map[string]any{
					"review_passed":  true,
					"tests_passed":   true,
					"estimated_cost": 0.11,
				},
			},
		}
		if err := selector.memory.RecordRouteOutcome(context.Background(), task, acceptance, result, 1200*time.Millisecond); err != nil {
			t.Fatalf("RecordRouteOutcome error: %v", err)
		}
	}

	selection := selector.Select(task)
	if selection.Provider != "antigravity" || selection.ModelName != "claude-sonnet-4-6" {
		t.Fatalf("expected history-backed antigravity route, got %s/%s", selection.Provider, selection.ModelName)
	}
	if scores, ok := selection.SelectionTrace["candidate_scores"].([]map[string]any); !ok || len(scores) == 0 {
		t.Fatalf("expected candidate scores in trace, got %#v", selection.SelectionTrace["candidate_scores"])
	}
}

func TestModelSelectorBudgetPressurePrefersLocalHealthyModel(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"ai_kernel": {Provider: "ai_kernel", DefaultModel: modelQwenCoder, BaseURL: "http://kernel.example/v1"},
		"openai":    {Provider: "openai", DefaultModel: modelOpenAIHigh, BaseURL: "https://api.openai.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["ai_kernel"] = domain.ProviderCatalogSnapshot{
		Provider:           "ai_kernel",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "ai_kernel", ModelName: modelQwenCoder, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "gemma"}}},
	}
	registry.snapshots["openai"] = domain.ProviderCatalogSnapshot{
		Provider:           "openai",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "openai", ModelName: modelOpenAIHigh, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}}},
	}

	selector := NewModelSelector(registry)
	selection := selector.Select(domain.Task{
		Type:     domain.TaskTypeCode,
		Priority: domain.PriorityNormal,
		Input:    domain.TaskInput{Description: "Apply a small local fix"},
		RoutingHints: map[string]any{
			"model_budget": map[string]any{"action": "reduce"},
		},
	})

	if selection.Provider != "ai_kernel" {
		t.Fatalf("expected ai_kernel under budget pressure, got %s/%s", selection.Provider, selection.ModelName)
	}
	inputs, ok := selection.SelectionTrace["selector_inputs"].(map[string]any)
	if !ok || inputs["budget_action"] != "reduce" {
		t.Fatalf("unexpected selector inputs: %#v", selection.SelectionTrace["selector_inputs"])
	}
}

func TestModelSelectorRetrievalSignalsFavorStrongerReasoningModel(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {Provider: "openai", DefaultModel: modelOpenAIHigh, BaseURL: "https://api.openai.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["openai"] = domain.ProviderCatalogSnapshot{
		Provider:           "openai",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models: []domain.ProviderModelStatus{
			{Provider: "openai", ModelName: "gpt-5.4-mini", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}},
			{Provider: "openai", ModelName: modelOpenAIHigh, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}},
		},
	}

	selector := NewModelSelector(registry)
	selection := selector.Select(domain.Task{
		Type:     domain.TaskTypeResearch,
		Priority: domain.PriorityHigh,
		Input:    domain.TaskInput{Description: "Synthesize architecture guidance from vector memory and validation context"},
		RoutingHints: map[string]any{
			"memory_context": map[string]any{
				"vector_memory_count":  5,
				"trained_memory_brief": "prior architecture findings",
			},
			"validation_context": map[string]any{"rag_required": true},
		},
	})

	if selection.ModelName != modelOpenAIHigh {
		t.Fatalf("expected stronger model for retrieval-heavy research, got %s/%s", selection.Provider, selection.ModelName)
	}
}

func TestModelSelectorExpandsPreferredModelsWithRegisteredHealthyModels(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {Provider: "openai", DefaultModel: modelOpenAIHigh, BaseURL: "https://api.openai.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["openai"] = domain.ProviderCatalogSnapshot{
		Provider:           "openai",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models: []domain.ProviderModelStatus{
			{Provider: "openai", ModelName: "gpt-5.4-mini", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}},
			{Provider: "openai", ModelName: "gpt-4.1", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}},
			{Provider: "openai", ModelName: "o3", Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", ObservedAt: now, Metadata: map[string]any{"model_family": "gpt"}},
		},
	}

	selector := NewModelSelector(registry)
	selection := selector.Select(domain.Task{
		Type:     domain.TaskTypeResearch,
		Priority: domain.PriorityHigh,
		Input:    domain.TaskInput{Description: "Survey available registered reasoning models"},
	})

	preferred, ok := selection.SelectionTrace["preferred_models"].([]string)
	if !ok {
		t.Fatalf("preferred_models trace missing: %#v", selection.SelectionTrace["preferred_models"])
	}
	for _, modelName := range []string{"gpt-4.1", "o3", "gpt-5.4-mini"} {
		found := false
		for _, preferredModel := range preferred {
			if preferredModel == modelName {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("preferred_models missing registered model %q: %#v", modelName, preferred)
		}
	}
}

func TestModelSelectorAddsAIKernelSupportLaneForCloudAnalysisRoute(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"ai_kernel": {Provider: "ai_kernel", DefaultModel: modelQwenCoder, BaseURL: "http://kernel.example/v1"},
		"mistral":   {Provider: "mistral", DefaultModel: modelMistral, BaseURL: "https://mistral.example/v1", APIKey: "secret", RequireKey: true},
	})
	registry.snapshots["ai_kernel"] = domain.ProviderCatalogSnapshot{
		Provider:           "ai_kernel",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "ai_kernel", ModelName: modelQwenCoder, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "gemma"}}},
	}
	registry.snapshots["mistral"] = domain.ProviderCatalogSnapshot{
		Provider:           "mistral",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "mistral", ModelName: modelMistral, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "mistral"}}},
	}

	selection := NewModelSelector(registry).Select(domain.Task{
		Type:     domain.TaskTypeReview,
		Priority: domain.PriorityHigh,
		Input: domain.TaskInput{
			Description: "Review architecture tradeoffs and summarize repo-local regression risks",
		},
	})

	if selection.Provider != "mistral" {
		t.Fatalf("provider=%q want mistral", selection.Provider)
	}
	if len(selection.SupportLanes) < 2 {
		t.Fatalf("support lanes=%d want at least 2", len(selection.SupportLanes))
	}
	roles := map[string]bool{}
	for _, lane := range selection.SupportLanes {
		if lane.Provider != "ai_kernel" {
			t.Fatalf("support lane provider=%q want ai_kernel", lane.Provider)
		}
		roles[lane.Role] = true
	}
	for _, role := range []string{"helper", "fallback", "parallel"} {
		if !roles[role] {
			t.Fatalf("missing support lane role %q in %#v", role, selection.SupportLanes)
		}
	}
	if available, _ := selection.SelectionTrace["ai_kernel_helper_available"].(bool); !available {
		t.Fatalf("expected ai_kernel helper trace, got %#v", selection.SelectionTrace)
	}
}
