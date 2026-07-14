package kernel

import (
	"context"
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestAdaptiveRuntimeSelectsRecoveryForDegradedLane(t *testing.T) {
	orchestrator, _, registry := newBudgetTestOrchestrator(t)
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-openai",
		Type:         "coding",
		Provider:     "openai",
		ModelName:    "gpt-5.5",
		Capabilities: []string{"code", "plan", "review"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5",
		Capabilities: []string{"code", "plan", "review"},
		Status:       domain.AgentStatusReady,
	}})

	orchestrator.runtime.RecordRuntimeFailure("coder-openai", "timeout")
	orchestrator.runtime.RecordRuntimeFailure("coder-openai", "timeout")
	orchestrator.runtime.RecordRuntimeFailure("coder-openai", "timeout")

	task := domain.Task{
		ID:         "task-adaptive-recovery",
		SessionID:  "session-adaptive-recovery",
		Type:       domain.TaskTypeCode,
		Complexity: domain.ComplexityHigh,
		Input: domain.TaskInput{
			Description: "Recover routing after repeated agent failures.",
		},
		Context: domain.TaskContext{Branch: "main"},
	}
	plan := domain.ExecutionPlan{
		TaskID:            task.ID,
		Complexity:        domain.ComplexityHigh,
		PrimaryCapability: "code",
		Steps: []domain.PlanStep{{
			ID:         "step-1",
			Title:      "recover",
			Capability: "code",
		}},
	}

	adapted, decision := orchestrator.adaptive.Apply(context.Background(), task, plan)
	if decision.Mode != domain.AdaptiveExecutionModeRecovery {
		t.Fatalf("decision.Mode = %s, want recovery", decision.Mode)
	}
	if decision.MaxParallelism != 1 {
		t.Fatalf("decision.MaxParallelism = %d, want 1 for narrow recovery plan", decision.MaxParallelism)
	}
	if adapted.RoutingHints["adaptive_mode"] != string(domain.AdaptiveExecutionModeRecovery) {
		t.Fatalf("routing adaptive_mode = %v, want %s", adapted.RoutingHints["adaptive_mode"], domain.AdaptiveExecutionModeRecovery)
	}
	if adapted.RoutingHints["route_mode"] != "orchestrator" {
		t.Fatalf("route_mode = %v, want orchestrator", adapted.RoutingHints["route_mode"])
	}
	if len(decision.SuppressedAgents) == 0 || decision.SuppressedAgents[0] != "coder-openai" {
		t.Fatalf("decision.SuppressedAgents = %#v, want coder-openai", decision.SuppressedAgents)
	}
	state, ok := orchestrator.runtime.State("coder-openai")
	if !ok {
		t.Fatal("runtime.State(coder-openai) missing")
	}
	if state.Status != domain.AgentStatusMaintenance {
		t.Fatalf("runtime status = %s, want maintenance", state.Status)
	}
}

func TestAdaptiveRuntimeSelectsThroughputAndRecordsVectorMemory(t *testing.T) {
	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-a",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5",
		Capabilities: []string{"code", "plan", "review"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-b",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5",
		Capabilities: []string{"code", "plan", "review"},
		Status:       domain.AgentStatusReady,
	}})

	ctx := context.Background()
	task := domain.Task{
		ID:         "task-adaptive-throughput",
		SessionID:  "session-adaptive-throughput",
		Type:       domain.TaskTypeCode,
		Complexity: domain.ComplexityHigh,
		Input: domain.TaskInput{
			Description: "Fan out independent implementation tasks across healthy agents.",
		},
		Context: domain.TaskContext{Branch: "main"},
	}
	plan := domain.ExecutionPlan{
		TaskID:            task.ID,
		Complexity:        domain.ComplexityHigh,
		PrimaryCapability: "code",
		Steps: []domain.PlanStep{
			{ID: "api", Title: "Build API", Capability: "code"},
			{ID: "ui", Title: "Build UI", Capability: "code"},
			{ID: "docs", Title: "Write docs", Capability: "code"},
		},
	}

	adapted, decision := orchestrator.adaptive.Apply(ctx, task, plan)
	if decision.Mode != domain.AdaptiveExecutionModeThroughput {
		t.Fatalf("decision.Mode = %s, want throughput", decision.Mode)
	}
	if decision.MaxParallelism < 3 {
		t.Fatalf("decision.MaxParallelism = %d, want at least 3", decision.MaxParallelism)
	}
	if adapted.RoutingHints["adaptive_max_parallelism"] != decision.MaxParallelism {
		t.Fatalf("routing adaptive_max_parallelism = %v, want %d", adapted.RoutingHints["adaptive_max_parallelism"], decision.MaxParallelism)
	}

	memories, err := store.ListRAGMemories(ctx, "session", task.SessionID, 10)
	if err != nil {
		t.Fatalf("ListRAGMemories() error = %v", err)
	}
	if len(memories) == 0 {
		t.Fatal("ListRAGMemories() returned no adaptive memories")
	}
	foundMemory := false
	for _, memory := range memories {
		if memory.MemoryType != "adaptive_decision" {
			continue
		}
		foundMemory = true
		if memory.Metadata["adaptive_mode"] != string(domain.AdaptiveExecutionModeThroughput) {
			t.Fatalf("memory adaptive_mode = %v, want %s", memory.Metadata["adaptive_mode"], domain.AdaptiveExecutionModeThroughput)
		}
	}
	if !foundMemory {
		t.Fatal("adaptive_decision memory record not found")
	}

	chunks, err := store.ListVectorChunks(ctx, task.SessionID, task.Context.Branch, 20)
	if err != nil {
		t.Fatalf("ListVectorChunks() error = %v", err)
	}
	if len(chunks) == 0 {
		t.Fatal("ListVectorChunks() returned no adaptive chunks")
	}
	foundChunk := false
	for _, chunk := range chunks {
		if chunk.Metadata["source_kind"] != "adaptive_decision" {
			continue
		}
		foundChunk = true
		if len(chunk.Embedding) == 0 {
			t.Fatal("adaptive chunk embedding = nil")
		}
	}
	if !foundChunk {
		t.Fatal("adaptive_decision vector chunk not found")
	}
}
