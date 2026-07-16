package kernel

import (
	"context"
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

type staticRetriever struct {
	snapshot memory.RetrievalSnapshot
}

func (s staticRetriever) Retrieve(context.Context, domain.Task, int) (memory.RetrievalSnapshot, error) {
	return s.snapshot, nil
}

func TestRouterRetrievalScorePrefersStrongerReasoningAgentForHeavyRetrieval(t *testing.T) {
	registry := NewRegistry()
	router := NewRouter(registry, NewModelSelector(nil))
	router.retriever = staticRetriever{snapshot: memory.RetrievalSnapshot{
		KPI: memory.RetrievalKPI{Tier: "high", CoverageRatio: 0.72, PackedCount: 4, TruncationRatio: 0.38},
	}}

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "ai_kernel",
		ModelName:    "qwen2.5-coder-32b",
		Capabilities: []string{"research", "docs"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "research-openai",
		Type:         "research",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"research", "docs"},
		Status:       domain.AgentStatusReady,
	}})

	task := domain.Task{Type: domain.TaskTypeResearch, Priority: domain.PriorityHigh, Input: domain.TaskInput{Description: "Synthesize evidence from retrieval memory"}}
	plan := domain.ExecutionPlan{PrimaryCapability: "research", Complexity: domain.ComplexityHigh}

	acceptance, agent, ok := router.Route(task, plan)
	if !ok {
		t.Fatal("expected route decision")
	}
	if acceptance.Provider != "openai" {
		t.Fatalf("expected openai provider, got %q", acceptance.Provider)
	}
	if agent.Info().ID != "research-openai" {
		t.Fatalf("expected research-openai agent, got %q", agent.Info().ID)
	}
}
