package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type routerTestAgent struct {
	info                          domain.AgentInfo
	supportsAssignedModelOverride bool
}

func (a *routerTestAgent) Info() domain.AgentInfo {
	return a.info
}

func (a *routerTestAgent) SupportsAssignedModelOverride() bool {
	return a.supportsAssignedModelOverride
}

func (a *routerTestAgent) CanAccept(domain.Task) bool {
	return true
}

func (a *routerTestAgent) Execute(_ context.Context, task domain.Task) domain.AgentResult {
	return domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     a.info.ID,
		Status:      domain.TaskStatusDone,
		Provider:    a.info.Provider,
		ModelName:   a.info.ModelName,
		CompletedAt: time.Now().UTC(),
	}
}

func TestRouterHonorsAssignedProviderWhenMatchingAgentExists(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "docs-ai-kernel",
		Type:         "docs",
		Provider:     "ai_kernel",
		ModelName:    "gemma4-12b-agentic-fable5:q4_k_m",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "research-mistral",
		Type:         "docs",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedProvider: "mistral", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want matched provider route")
	}
	if acceptance.Provider != "mistral" {
		t.Fatalf("acceptance.Provider = %s, want mistral", acceptance.Provider)
	}
	if agent.Info().ID != "research-mistral" {
		t.Fatalf("agent.ID = %s, want research-mistral", agent.Info().ID)
	}
}

func TestRouterRejectsWhenAssignedProviderHasNoMatchingAgent(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "docs-ai-kernel",
		Type:         "docs",
		Provider:     "ai_kernel",
		ModelName:    "gemma4-12b-agentic-fable5:q4_k_m",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedProvider: "mistral", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, _, ok := router.Route(task, plan)
	if ok {
		t.Fatal("Route() succeeded, want rejection when assigned provider has no agent")
	}
	if acceptance.Reason != "no available agent matched assigned provider mistral" {
		t.Fatalf("acceptance.Reason = %q, want assigned-provider rejection", acceptance.Reason)
	}
}

func TestRouterRejectsWhenAssignedModelHasNoMatchingAgent(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "docs-gpt",
		Type:         "docs",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "docs-claude",
		Type:         "docs",
		Provider:     "codexsale",
		ModelName:    "claude-sonnet-4-6",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedModel: "gpt-5.4-mini", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, _, ok := router.Route(task, plan)
	if ok {
		t.Fatal("Route() succeeded, want rejection when assigned model has no agent")
	}
	if acceptance.Reason != "no available agent matched assigned model gpt-5.4-mini" {
		t.Fatalf("acceptance.Reason = %q, want assigned-model rejection", acceptance.Reason)
	}
}

func TestRouterRoutesConfirmedAssignedModelThroughProviderAgent(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "research-mistral",
		Type:         "docs",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}, supportsAssignedModelOverride: true})

	now := time.Now().UTC()
	router.runtime = NewRuntimeManager(registry, &ProviderModelRegistry{snapshots: map[string]domain.ProviderCatalogSnapshot{
		"mistral": {
			Provider:           "mistral",
			Configured:         true,
			Available:          true,
			Status:             "ready",
			ObservedAt:         now,
			RefreshIntervalSec: 300,
			Models: []domain.ProviderModelStatus{{
				Provider:           "mistral",
				ModelName:          "codestral-latest",
				Available:          true,
				Status:             "ready",
				VerificationStatus: "confirmed",
				TransportStatus:    "transport_verified",
				ObservedAt:         now,
			}},
		},
	}}, nil)

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedModel: "codestral-latest", AssignedProvider: "mistral", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want confirmed assigned-model route")
	}
	if agent.Info().ID != "research-mistral" {
		t.Fatalf("agent.ID = %s, want research-mistral", agent.Info().ID)
	}
	if acceptance.ModelName != "codestral-latest" {
		t.Fatalf("acceptance.ModelName = %s, want codestral-latest", acceptance.ModelName)
	}
}

func TestRouterAllowsExactAssignedModelWhenRuntimeHasNoProviderRegistry(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "docs-gpt",
		Type:         "docs",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}})

	router.runtime = NewRuntimeManager(registry, nil, nil)

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedModel: "gpt-5.5", AssignedProvider: "openai", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want exact assigned-model route without provider registry")
	}
	if agent.Info().ID != "docs-gpt" {
		t.Fatalf("agent.ID = %s, want docs-gpt", agent.Info().ID)
	}
	if acceptance.ModelName != "gpt-5.5" {
		t.Fatalf("acceptance.ModelName = %s, want gpt-5.5", acceptance.ModelName)
	}
}

func TestRouterRejectsUnconfirmedAssignedModelEvenWhenProviderMatches(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "research-mistral",
		Type:         "docs",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"docs", "review"},
		Status:       domain.AgentStatusReady,
	}, supportsAssignedModelOverride: true})

	router.runtime = NewRuntimeManager(registry, &ProviderModelRegistry{snapshots: map[string]domain.ProviderCatalogSnapshot{
		"mistral": {
			Provider:           "mistral",
			Configured:         true,
			Available:          false,
			Status:             "verifying",
			ObservedAt:         time.Now().UTC(),
			RefreshIntervalSec: 300,
			Models: []domain.ProviderModelStatus{{
				Provider:           "mistral",
				ModelName:          "codestral-latest",
				Available:          false,
				Status:             "verification_pending",
				VerificationStatus: "verifying",
				TransportStatus:    "transport_pending",
				Reason:             "model verification is still in progress",
				ObservedAt:         time.Now().UTC(),
			}},
		},
	}}, nil)

	task := domain.Task{Type: domain.TaskTypeDocs, AssignedModel: "codestral-latest", AssignedProvider: "mistral", Input: domain.TaskInput{Description: "Write release notes"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "docs", Complexity: domain.ComplexityMedium}

	acceptance, _, ok := router.Route(task, plan)
	if ok {
		t.Fatal("Route() succeeded, want rejection while assigned model is not confirmed")
	}
	if acceptance.Reason != "no available agent matched assigned model codestral-latest" {
		t.Fatalf("acceptance.Reason = %q, want assigned-model rejection", acceptance.Reason)
	}
}

func TestRouterPrefersLocalCodeWorkerForSmallClusterTask(t *testing.T) {
	registry := NewRegistry()
	router := NewRouter(registry, NewModelSelector(nil))

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "ai_kernel",
		ModelName:    "gemma4-12b-agentic-fable5:q4_k_m",
		Capabilities: []string{"code", "review"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "review-openai",
		Type:         "review",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"code", "review"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{
		Type:               domain.TaskTypeCode,
		RequiredCapability: "code",
		Input:              domain.TaskInput{Description: "Patch one repository file"},
		RoutingHints: map[string]any{
			"worker_class":   "code",
			"context_budget": 640,
			"task_weight":    1.2,
		},
		ExecutionContract: map[string]any{
			"worker_class":   "code",
			"context_budget": 640,
			"task_weight":    1.2,
		},
	}
	plan := domain.ExecutionPlan{PrimaryCapability: "code", Complexity: domain.ComplexityLow}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want local code worker")
	}
	if acceptance.Provider != "ai_kernel" {
		t.Fatalf("acceptance.Provider = %s, want ai_kernel", acceptance.Provider)
	}
	if agent.Info().ID != "coder-local" {
		t.Fatalf("agent.ID = %s, want coder-local", agent.Info().ID)
	}
}

func TestRouterPrefersRemoteReviewWorkerForHeavyMergeTask(t *testing.T) {
	registry := NewRegistry()
	router := NewRouter(registry, NewModelSelector(nil))

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "merge-local",
		Type:         "analysis",
		Provider:     "ai_kernel",
		ModelName:    "gemma4-12b-agentic-fable5:q4_k_m",
		Capabilities: []string{"review", "plan"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "review-openai",
		Type:         "review",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"review", "plan"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{
		Type:               domain.TaskTypeReview,
		RequiredCapability: "review",
		Priority:           domain.PriorityHigh,
		Input:              domain.TaskInput{Description: "Merge two risky branches and validate regressions"},
		RoutingHints: map[string]any{
			"worker_class":   "merge",
			"context_budget": 2400,
			"task_weight":    4.4,
		},
		ExecutionContract: map[string]any{
			"worker_class":   "merge",
			"context_budget": 2400,
			"task_weight":    4.4,
		},
	}
	plan := domain.ExecutionPlan{PrimaryCapability: "review", Complexity: domain.ComplexityHigh}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want remote review worker")
	}
	if acceptance.Provider != "openai" {
		t.Fatalf("acceptance.Provider = %s, want openai", acceptance.Provider)
	}
	if agent.Info().ID != "review-openai" {
		t.Fatalf("agent.ID = %s, want review-openai", agent.Info().ID)
	}
}

func TestRouterPrefersLessLoadedProviderForCodeWorker(t *testing.T) {
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	router := NewRouter(registry, selector)

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coder",
		Provider:     "local",
		ModelName:    "gemma",
		Capabilities: []string{"code"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-mistral",
		Type:         "coder",
		Provider:     "mistral",
		ModelName:    "codestral-latest",
		Capabilities: []string{"code"},
		Status:       domain.AgentStatusReady,
	}})

	router.runtime = NewRuntimeManager(registry, nil, nil)
	router.runtime.UpdateCapacitySnapshot("coder-local", CapacitySnapshot{InFlight: 3, AgentSlotUsage: 1.0, ModelSlotUsage: 1.0, GlobalSlotUsage: 0.8})
	router.runtime.UpdateCapacitySnapshot("coder-mistral", CapacitySnapshot{InFlight: 0, AgentSlotUsage: 0.1, ModelSlotUsage: 0.1, GlobalSlotUsage: 0.2})

	task := domain.Task{
		Type:  domain.TaskTypeCode,
		Input: domain.TaskInput{Description: "Update handler implementation"},
		RoutingHints: map[string]any{
			"worker_class":   "code",
			"context_budget": 800,
			"task_weight":    0.4,
		},
	}
	plan := domain.ExecutionPlan{PrimaryCapability: "code", Complexity: domain.ComplexityMedium}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("Route() rejected task, want routed code worker")
	}
	if agent.Info().ID != "coder-mistral" {
		t.Fatalf("agent.ID = %s, want coder-mistral", agent.Info().ID)
	}
	if acceptance.Provider != "mistral" {
		t.Fatalf("acceptance.Provider = %s, want mistral", acceptance.Provider)
	}
}
