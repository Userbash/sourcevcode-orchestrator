package kernel

import (
	"context"
	"fmt"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type concurrencyProbeAgent struct {
	info      domain.AgentInfo
	delay     time.Duration
	active    atomic.Int64
	maxActive atomic.Int64
	executed  atomic.Int64
}

func (a *concurrencyProbeAgent) Info() domain.AgentInfo {
	return a.info
}

func (a *concurrencyProbeAgent) CanAccept(domain.Task) bool {
	return true
}

func (a *concurrencyProbeAgent) Execute(_ context.Context, task domain.Task) domain.AgentResult {
	a.executed.Add(1)
	current := a.active.Add(1)
	for {
		observed := a.maxActive.Load()
		if current <= observed {
			break
		}
		if a.maxActive.CompareAndSwap(observed, current) {
			break
		}
	}
	defer a.active.Add(-1)
	if a.delay > 0 {
		time.Sleep(a.delay)
	}
	return domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     a.info.ID,
		Provider:    a.info.Provider,
		ModelName:   a.info.ModelName,
		Status:      domain.TaskStatusDone,
		CompletedAt: time.Now().UTC(),
		Output: domain.ResultOutput{
			Summary: "concurrency probe completed",
			Artifacts: map[string]any{
				"usage": map[string]any{"total_tokens": 5},
			},
		},
	}
}

func TestRunExecutionPlanExecutesParallelBranchesConcurrently(t *testing.T) {
	t.Setenv("GO_CORE_MAX_PARALLELISM", "8")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_AGENT", "4")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_MODEL", "4")

	orchestrator, _, registry := newBudgetTestOrchestrator(t)
	agent := &concurrencyProbeAgent{info: domain.AgentInfo{
		ID:           "coder-local-parallel",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, delay: 80 * time.Millisecond}
	registry.RegisterAgent(agent)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	run, err := orchestrator.RunExecutionPlan(ctx, domain.Task{
		ID:               "task-verify-concurrency",
		SessionID:        "session-verify-concurrency",
		Type:             domain.TaskTypeCode,
		Complexity:       domain.ComplexityCritical,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		RoutingHints:     map[string]any{"preferred_agent_id": "coder-local-parallel"},
		Input: domain.TaskInput{
			Description: "Implement concurrent changes across API, kernel, and delivery layers.",
			Files: []string{
				"internal/api/http.go",
				"internal/kernel/orchestrator.go",
				"internal/delivery/worker_pool.go",
			},
			AcceptanceCriteria: []string{"parallel branches complete", "workflow finishes cleanly"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	})
	if err != nil {
		t.Fatalf("RunExecutionPlan() error = %v", err)
	}
	if run.Checkpoint.Status != domain.ParallelPlanStatusCompleted {
		t.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, domain.ParallelPlanStatusCompleted)
	}
	if agent.executed.Load() != int64(len(run.PlanArtifact.Tasks)) {
		t.Fatalf("executed = %d, want %d", agent.executed.Load(), len(run.PlanArtifact.Tasks))
	}
	if maxActive := agent.maxActive.Load(); maxActive < 2 {
		t.Fatalf("maxActive = %d, want at least 2 concurrent branch executions", maxActive)
	}
}

func BenchmarkRunExecutionPlanParallelFanout(b *testing.B) {
	b.ReportAllocs()
	b.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	b.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	b.Setenv("GO_CORE_MAX_PARALLELISM", "8")
	b.Setenv("GO_CORE_MAX_CONCURRENT_PER_AGENT", "4")
	b.Setenv("GO_CORE_MAX_CONCURRENT_PER_MODEL", "4")

	store, err := state.NewFileStore(filepath.Join(b.TempDir(), "state.json"))
	if err != nil {
		b.Fatalf("NewFileStore() error = %v", err)
	}
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	orchestrator := NewOrchestrator(registry, planner, router, store, realtime.NewHub("runtime", 32), realtime.NewHub("inventory", 16), nil)
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-bench",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, result: domain.AgentResult{Output: domain.ResultOutput{Summary: "bench ok", Artifacts: map[string]any{
		"usage": map[string]any{"total_tokens": 7},
	}}}})

	ctx := context.Background()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		taskID := fmt.Sprintf("bench-run-plan-%d", i)
		run, err := orchestrator.RunExecutionPlan(ctx, domain.Task{
			ID:               taskID,
			SessionID:        "bench-session",
			Type:             domain.TaskTypeCode,
			Complexity:       domain.ComplexityCritical,
			AssignedProvider: "local",
			AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
			RoutingHints:     map[string]any{"preferred_agent_id": "coder-local-bench"},
			Input: domain.TaskInput{
				Description: "Benchmark task fanout across api, kernel, and delivery branches.",
				Files: []string{
					"internal/api/http.go",
					"internal/kernel/orchestrator.go",
					"internal/delivery/worker_pool.go",
				},
				AcceptanceCriteria: []string{"branches finish", "checkpoint collected"},
			},
			Context: domain.TaskContext{Branch: "main", Project: "go-core"},
		})
		if err != nil {
			b.Fatalf("RunExecutionPlan() error = %v", err)
		}
		if run.Checkpoint.Status != domain.ParallelPlanStatusCompleted {
			b.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, domain.ParallelPlanStatusCompleted)
		}
	}
}
