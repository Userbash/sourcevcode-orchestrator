package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

func TestRealTaskRegressionSuiteCollectsLifecycleMemoryAndTraceEvidence(t *testing.T) {
	tests := []struct {
		name string
		task domain.Task
	}{
		{
			name: "code",
			task: domain.Task{
				ID:               "task-regression-code",
				SessionID:        "session-regression-code",
				Type:             domain.TaskTypeCode,
				Priority:         domain.PriorityHigh,
				Complexity:       domain.ComplexityHigh,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Refactor orchestrator routing, worker scheduling and result callbacks across multiple files.",
					Files:              []string{"internal/kernel/orchestrator.go", "internal/kernel/advanced_planner.go", "internal/delivery/worker_pool.go"},
					AcceptanceCriteria: []string{"parallel plan remains healthy", "callbacks remain stable"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
		},
		{
			name: "research",
			task: domain.Task{
				ID:               "task-regression-research",
				SessionID:        "session-regression-research",
				Type:             domain.TaskTypeResearch,
				Priority:         domain.PriorityNormal,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Investigate runtime degradation signals and summarize bottlenecks in orchestration.",
					Files:              []string{"docs/runtime.md"},
					AcceptanceCriteria: []string{"capture bottlenecks", "preserve evidence"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
		},
		{
			name: "docs",
			task: domain.Task{
				ID:               "task-regression-docs",
				SessionID:        "session-regression-docs",
				Type:             domain.TaskTypeDocs,
				Priority:         domain.PriorityLow,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Document the async delivery lifecycle, retries and adaptive execution policy.",
					Files:              []string{"docs/p2p_delivery.md"},
					AcceptanceCriteria: []string{"document lifecycle", "explain adaptive policy"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
		},
		{
			name: "review",
			task: domain.Task{
				ID:               "task-regression-review",
				SessionID:        "session-regression-review",
				Type:             domain.TaskTypeReview,
				Priority:         domain.PriorityNormal,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Review the planner and runtime changes for correctness, fairness and regression risks.",
					Files:              []string{"internal/kernel/advanced_planner.go", "internal/kernel/orchestrator.go"},
					AcceptanceCriteria: []string{"identify fairness regressions", "validate runtime behavior"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			orchestrator, store, registry := newBudgetTestOrchestrator(t)
			registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
				ID:           "coder-local",
				Type:         "coding",
				Provider:     "local",
				ModelName:    "qwen2.5:32b-instruct-q4_k_m",
				Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
				Status:       domain.AgentStatusReady,
			}, result: domain.AgentResult{
				Status: domain.TaskStatusCompleted,
				Output: domain.ResultOutput{
					Summary: "completed " + tt.name + " regression scenario",
					Artifacts: map[string]any{
						"task_type": string(tt.task.Type),
						"trace_id":  tt.task.ID,
					},
				},
			}})

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()

			run, err := orchestrator.RunExecutionPlan(ctx, tt.task)
			if err != nil {
				t.Fatalf("RunExecutionPlan() error = %v", err)
			}
			if run.Checkpoint.Status != domain.ParallelPlanStatusCompleted {
				t.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, domain.ParallelPlanStatusCompleted)
			}
			if len(run.PlanArtifact.Tasks) == 0 {
				t.Fatal("PlanArtifact.Tasks is empty")
			}
			if len(run.Workflows) != len(run.PlanArtifact.Tasks) {
				t.Fatalf("len(run.Workflows) = %d, want %d", len(run.Workflows), len(run.PlanArtifact.Tasks))
			}
			if len(run.Checkpoint.ResultsByTaskID) != len(run.PlanArtifact.Tasks) {
				t.Fatalf("len(ResultsByTaskID) = %d, want %d", len(run.Checkpoint.ResultsByTaskID), len(run.PlanArtifact.Tasks))
			}

			for _, workflow := range run.Workflows {
				if workflow.Acceptance.Status != domain.TaskStatusCompleted {
					t.Fatalf("workflow %s status = %s, want %s", workflow.Task.ID, workflow.Acceptance.Status, domain.TaskStatusCompleted)
				}
				if workflow.Result == nil {
					t.Fatalf("workflow %s result = nil", workflow.Task.ID)
				}
				persisted, ok, err := store.GetWorkflow(ctx, workflow.Task.ID)
				if err != nil {
					t.Fatalf("GetWorkflow(%s) error = %v", workflow.Task.ID, err)
				}
				if !ok {
					t.Fatalf("workflow %s not found in store", workflow.Task.ID)
				}
				if persisted.Acceptance.Status != domain.TaskStatusCompleted {
					t.Fatalf("persisted workflow %s status = %s, want %s", workflow.Task.ID, persisted.Acceptance.Status, domain.TaskStatusCompleted)
				}
				kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), workflow.Task.ID)
				assertOrderedTaskEvents(t, kinds, []taskEventKind{taskEventAccepted, taskEventRunning, taskEventCompleted})
			}

			manager := memory.NewManager(store)
			trace := degradationTraceFromRun("real_task_regression", "real task regression suite", run, orchestrator.RuntimeEventSnapshot("tasks"))
			if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
				t.Fatalf("RecordDegradationTrace() error = %v", err)
			}

			memories, err := store.ListRAGMemories(ctx, "session", tt.task.SessionID, 50)
			if err != nil {
				t.Fatalf("ListRAGMemories() error = %v", err)
			}
			if !containsMemoryType(memories, "adaptive_decision") {
				t.Fatalf("session %s missing adaptive_decision memory", tt.task.SessionID)
			}
			if !containsMemoryType(memories, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace memory", tt.task.SessionID)
			}

			chunks, err := store.ListVectorChunks(ctx, tt.task.SessionID, tt.task.Context.Branch, 100)
			if err != nil {
				t.Fatalf("ListVectorChunks() error = %v", err)
			}
			if !containsChunkSourceKind(chunks, "adaptive_decision") {
				t.Fatalf("session %s missing adaptive_decision vector chunks", tt.task.SessionID)
			}
			if !containsChunkSourceKind(chunks, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace vector chunks", tt.task.SessionID)
			}
		})
	}
}

func containsMemoryType(memories []domain.RAGMemoryRecord, want string) bool {
	for _, memory := range memories {
		if memory.MemoryType == want {
			return true
		}
	}
	return false
}

func containsChunkSourceKind(chunks []domain.VectorChunk, want string) bool {
	for _, chunk := range chunks {
		if chunk.Metadata["source_kind"] == want {
			return true
		}
	}
	return false
}
