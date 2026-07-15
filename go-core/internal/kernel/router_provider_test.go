package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type routerTestAgent struct {
	info domain.AgentInfo
}

func (a *routerTestAgent) Info() domain.AgentInfo {
	return a.info
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
		ModelName:    "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
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
		ModelName:    "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
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
