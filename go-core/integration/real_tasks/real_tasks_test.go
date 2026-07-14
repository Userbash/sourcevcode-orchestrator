package realtasks

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

func TestIntegrationRealTasksMeetPerformanceThresholdsAndPersistDegradationTelemetry(t *testing.T) {
	tests := []struct {
		name       string
		task       domain.Task
		thresholds realTaskThresholds
	}{
		{
			name: "code fanout",
			task: domain.Task{
				ID:               "integration-code-fanout",
				SessionID:        "integration-session-code-fanout",
				Type:             domain.TaskTypeCode,
				Priority:         domain.PriorityHigh,
				Complexity:       domain.ComplexityHigh,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description: "Implement a multi-file scheduler refactor across API, kernel, and delivery layers.",
					Files: []string{
						"internal/api/http.go",
						"internal/kernel/orchestrator.go",
						"internal/delivery/worker_pool.go",
					},
					AcceptanceCriteria: []string{"parallel branches stay healthy", "worker pool remains fair"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			thresholds: realTaskThresholds{
				MaxTotalLatency:         overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_LATENCY_MS", 2*time.Second),
				MinThroughputPerSecond:  overrideFloatFromEnv("GO_CORE_INTEGRATION_REALTASK_MIN_THROUGHPUT", 8),
				MinParallelWidth:        3,
				MaxMeanExecutionLatency: overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_MEAN_EXECUTION_MS", 250*time.Millisecond),
			},
		},
		{
			name: "research sequential",
			task: domain.Task{
				ID:               "integration-research-sequential",
				SessionID:        "integration-session-research-sequential",
				Type:             domain.TaskTypeResearch,
				Priority:         domain.PriorityNormal,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Investigate orchestration bottlenecks and summarize mitigation strategies.",
					Files:              []string{"internal/kernel/orchestrator.go"},
					AcceptanceCriteria: []string{"find latency hot spots", "document mitigations"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			thresholds: realTaskThresholds{
				MaxTotalLatency:         overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_LATENCY_MS", 2*time.Second),
				MinThroughputPerSecond:  overrideFloatFromEnv("GO_CORE_INTEGRATION_REALTASK_MIN_THROUGHPUT", 4),
				MinParallelWidth:        1,
				MaxMeanExecutionLatency: overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_MEAN_EXECUTION_MS", 250*time.Millisecond),
			},
		},
		{
			name: "docs sequential",
			task: domain.Task{
				ID:               "integration-docs-sequential",
				SessionID:        "integration-session-docs-sequential",
				Type:             domain.TaskTypeDocs,
				Priority:         domain.PriorityNormal,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Write and validate orchestrator delivery documentation for async execution.",
					Files:              []string{"docs/p2p_delivery.md"},
					AcceptanceCriteria: []string{"capture lifecycle states", "explain failover"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			thresholds: realTaskThresholds{
				MaxTotalLatency:         overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_LATENCY_MS", 2*time.Second),
				MinThroughputPerSecond:  overrideFloatFromEnv("GO_CORE_INTEGRATION_REALTASK_MIN_THROUGHPUT", 4),
				MinParallelWidth:        1,
				MaxMeanExecutionLatency: overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_MEAN_EXECUTION_MS", 250*time.Millisecond),
			},
		},
		{
			name: "review sequential",
			task: domain.Task{
				ID:               "integration-review-sequential",
				SessionID:        "integration-session-review-sequential",
				Type:             domain.TaskTypeReview,
				Priority:         domain.PriorityHigh,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Review orchestrator async scheduling changes and confirm regression coverage.",
					Files:              []string{"internal/kernel/advanced_planner.go", "internal/kernel/orchestrator.go"},
					AcceptanceCriteria: []string{"flag lifecycle risks", "validate fairness logic"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			thresholds: realTaskThresholds{
				MaxTotalLatency:         overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_LATENCY_MS", 2*time.Second),
				MinThroughputPerSecond:  overrideFloatFromEnv("GO_CORE_INTEGRATION_REALTASK_MIN_THROUGHPUT", 4),
				MinParallelWidth:        1,
				MaxMeanExecutionLatency: overrideDurationFromEnv("GO_CORE_INTEGRATION_REALTASK_MAX_MEAN_EXECUTION_MS", 250*time.Millisecond),
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			orchestrator, store, registry := newIntegrationOrchestrator(t)
			registry.RegisterAgent(&fakeAgent{
				info: domain.AgentInfo{
					ID:           "coder-local",
					Type:         "coding",
					Provider:     "local",
					ModelName:    "qwen2.5:32b-instruct-q4_k_m",
					Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
					Status:       domain.AgentStatusReady,
				},
				delay: 10 * time.Millisecond,
				result: domain.AgentResult{
					Status: domain.TaskStatusCompleted,
					Output: domain.ResultOutput{
						Summary:   "integration workload completed",
						Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 17}},
					},
				},
			})

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()

			run, err := orchestrator.RunExecutionPlan(ctx, tt.task)
			if err != nil {
				t.Fatalf("RunExecutionPlan() error = %v", err)
			}
			if run.Checkpoint.Status != domain.ParallelPlanStatusCompleted {
				t.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, domain.ParallelPlanStatusCompleted)
			}

			metrics := collectMetrics(run, orchestrator.RuntimeEventSnapshot("tasks"))
			assertPerformanceThresholds(t, metrics, tt.thresholds)

			manager := memory.NewManager(store)
			trace := domain.DegradationTrace{
				TraceID:                tt.task.ID + "-integration-trace",
				SuiteID:                "integration_real_tasks",
				Subject:                tt.name,
				SessionID:              tt.task.SessionID,
				Branch:                 tt.task.Context.Branch,
				Scenario:               string(tt.task.Type),
				TaskType:               tt.task.Type,
				WorkflowCount:          metrics.WorkflowCount,
				ParallelWidth:          metrics.ParallelWidth,
				TotalLatencyMS:         metrics.Duration.Milliseconds(),
				MeanQueueLatencyMS:     metrics.MeanQueueLatency.Milliseconds(),
				MeanExecutionLatencyMS: metrics.MeanExecutionLatency.Milliseconds(),
				ThroughputPerSecond:    metrics.ThroughputPerSecond,
				CompletedCount:         len(run.Workflows),
				CollectedAt:            time.Now().UTC(),
				Metadata: map[string]any{
					"root_task_id": run.Task.ID,
					"checkpoint":   string(run.Checkpoint.Status),
				},
			}
			if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
				t.Fatalf("RecordDegradationTrace() error = %v", err)
			}

			memories, err := store.ListRAGMemories(ctx, "session", tt.task.SessionID, 20)
			if err != nil {
				t.Fatalf("ListRAGMemories() error = %v", err)
			}
			if !hasMemoryType(memories, "adaptive_decision") {
				t.Fatalf("session %s missing adaptive_decision memory", tt.task.SessionID)
			}
			if !hasMemoryType(memories, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace memory", tt.task.SessionID)
			}

			chunks, err := store.ListVectorChunks(ctx, tt.task.SessionID, tt.task.Context.Branch, 50)
			if err != nil {
				t.Fatalf("ListVectorChunks() error = %v", err)
			}
			if !hasChunkSourceKind(chunks, "adaptive_decision") {
				t.Fatalf("session %s missing adaptive_decision vector chunks", tt.task.SessionID)
			}
			if !hasChunkSourceKind(chunks, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace vector chunks", tt.task.SessionID)
			}

			results, err := manager.SearchVectorContext(ctx, domain.Task{
				ID:        tt.task.ID + "-query",
				SessionID: tt.task.SessionID,
				Type:      domain.TaskTypeResearch,
				Input: domain.TaskInput{
					Description: "Find degradation telemetry and adaptive routing evidence for real task integration coverage.",
				},
				Context: domain.TaskContext{Branch: tt.task.Context.Branch, Project: tt.task.Context.Project},
			}, 5)
			if err != nil {
				t.Fatalf("SearchVectorContext() error = %v", err)
			}
			if len(results) == 0 {
				t.Fatal("SearchVectorContext() returned no integration telemetry results")
			}
		})
	}
}

func hasMemoryType(memories []domain.RAGMemoryRecord, want string) bool {
	for _, memory := range memories {
		if memory.MemoryType == want {
			return true
		}
	}
	return false
}

func hasChunkSourceKind(chunks []domain.VectorChunk, want string) bool {
	for _, chunk := range chunks {
		if chunk.Metadata["source_kind"] == want {
			return true
		}
	}
	return false
}
