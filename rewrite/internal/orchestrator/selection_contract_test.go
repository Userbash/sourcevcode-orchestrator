package orchestrator_test

import (
	"context"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestSelectChoosesOnlyReadyCompatibleFreshCandidate(t *testing.T) {
	selector := orchestrator.NewSelector()
	candidates := []orchestrator.ModelCandidate{
		{Provider: "fast", Model: "old", Status: orchestrator.CandidateStale, Capabilities: []string{"code"}, Score: 100},
		{Provider: "cheap", Model: "chat", Status: orchestrator.CandidateReady, Capabilities: []string{"chat"}, Score: 90},
		{Provider: "safe", Model: "code-1", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 50},
	}

	got, err := selector.Select(context.Background(), orchestrator.SelectionRequest{Capability: "code"}, candidates)
	if err != nil {
		t.Fatalf("Select() error = %v", err)
	}
	if got.Provider != "safe" || got.Model != "code-1" {
		t.Fatalf("selection = %#v; want safe/code-1", got)
	}
}

func TestSelectIsDeterministicWhenScoresTie(t *testing.T) {
	selector := orchestrator.NewSelector()
	candidates := []orchestrator.ModelCandidate{
		{Provider: "zeta", Model: "z", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 10},
		{Provider: "alpha", Model: "a", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 10},
	}
	for range 100 {
		got, err := selector.Select(context.Background(), orchestrator.SelectionRequest{Capability: "code"}, candidates)
		if err != nil {
			t.Fatal(err)
		}
		if got.Provider != "alpha" || got.Model != "a" {
			t.Fatalf("non-deterministic selection: %#v", got)
		}
	}
}

func TestSelectPrefersTheHighestScore(t *testing.T) {
	selector := orchestrator.NewSelector()
	got, err := selector.Select(context.Background(), orchestrator.SelectionRequest{Capability: "code"}, []orchestrator.ModelCandidate{
		{Provider: "low", Model: "model", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 1},
		{Provider: "high", Model: "model", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 2},
	})
	if err != nil || got != (orchestrator.Selection{Provider: "high", Model: "model"}) {
		t.Fatalf("Select() = %#v, %v", got, err)
	}
}

func TestSelectUsesModelNameAsFinalTieBreaker(t *testing.T) {
	selector := orchestrator.NewSelector()
	got, err := selector.Select(context.Background(), orchestrator.SelectionRequest{Capability: "code"}, []orchestrator.ModelCandidate{
		{Provider: "provider", Model: "z", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 1},
		{Provider: "provider", Model: "a", Status: orchestrator.CandidateReady, Capabilities: []string{"code"}, Score: 1},
	})
	if err != nil || got != (orchestrator.Selection{Provider: "provider", Model: "a"}) {
		t.Fatalf("Select() = %#v, %v", got, err)
	}
}

func TestRoutePreservesTheSelectionProviderAndModel(t *testing.T) {
	router, err := orchestrator.NewRouter(orchestrator.NewInMemoryRegistry(
		orchestrator.Agent{ID: "wrong-provider", Provider: "other", Model: "other-code", Capabilities: []string{"code"}, Ready: true},
		orchestrator.Agent{ID: "right-provider", Provider: "safe", Model: "code-1", Capabilities: []string{"code"}, Ready: true},
	))
	if err != nil {
		t.Fatal(err)
	}
	got, err := router.Route(orchestrator.RouteRequest{Capability: "code", Selection: orchestrator.Selection{Provider: "safe", Model: "code-1"}})
	if err != nil {
		t.Fatal(err)
	}
	if got.AgentID != "right-provider" || got.Provider != "safe" || got.Model != "code-1" {
		t.Fatalf("route = %#v", got)
	}
}

func TestFallbackAtomicallyReplacesProviderAndModelAndWritesAuditEvent(t *testing.T) {
	workflow := orchestrator.NewWorkflow("task-1", "idem-1")
	workflow.Assign(orchestrator.Selection{Provider: "first", Model: "first-1"})
	if err := workflow.Fallback(orchestrator.Selection{Provider: "second", Model: "second-2"}, "first timed out"); err != nil {
		t.Fatal(err)
	}
	if workflow.Selection() != (orchestrator.Selection{Provider: "second", Model: "second-2"}) {
		t.Fatalf("selection = %#v", workflow.Selection())
	}
	events := workflow.Events()
	if len(events) != 2 || events[1].Kind != "fallback" || events[1].Reason != "first timed out" {
		t.Fatalf("events = %#v", events)
	}
}
