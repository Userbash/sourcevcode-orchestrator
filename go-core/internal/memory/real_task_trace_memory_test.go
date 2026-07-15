package memory

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestRealTaskTraceMemoryRegressionPersistsAdaptiveAndDegradationEvidence(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	task := domain.Task{
		ID:         "task-memory-real-trace",
		SessionID:  "session-memory-real-trace",
		Type:       domain.TaskTypeCode,
		Priority:   domain.PriorityHigh,
		Complexity: domain.ComplexityHigh,
		Input: domain.TaskInput{
			Description:        "Refactor orchestrator scheduling and persist full regression telemetry.",
			Files:              []string{"internal/kernel/orchestrator.go", "internal/kernel/advanced_planner.go"},
			AcceptanceCriteria: []string{"collect lifecycle evidence", "store degradation metrics"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	}
	decision := domain.AdaptiveDecision{
		Mode:           domain.AdaptiveExecutionModeThroughput,
		Reason:         "healthy pool allows wider parallel task fan-out",
		MaxParallelism: 3,
		Diagnostics: domain.AdaptiveDiagnostics{
			HealthyAgents:    2,
			DegradedAgents:   0,
			SuppressedAgents: 0,
			OfflineAgents:    0,
			AverageErrorRate: 0.05,
			ObservedAt:       time.Now().UTC(),
		},
		DecidedAt: time.Now().UTC(),
	}
	if err := manager.RecordAdaptiveDecision(ctx, task, decision); err != nil {
		t.Fatalf("RecordAdaptiveDecision() error = %v", err)
	}

	trace := domain.DegradationTrace{
		TraceID:                "trace-memory-real-trace",
		SuiteID:                "real_task_memory_regression",
		Subject:                "real task telemetry",
		SessionID:              task.SessionID,
		Branch:                 task.Context.Branch,
		Scenario:               "code",
		TaskType:               task.Type,
		WorkflowCount:          3,
		CompletedCount:         3,
		ParallelWidth:          2,
		TotalLatencyMS:         920,
		MeanQueueLatencyMS:     12,
		MeanExecutionLatencyMS: 210,
		ThroughputPerSecond:    3.1,
		Samples: []domain.DegradationSample{
			{TaskID: "task-memory-real-trace-a", Status: domain.TaskStatusCompleted, EventKinds: []string{"task.accepted", "task.running", "task.completed"}},
			{TaskID: "task-memory-real-trace-b", Status: domain.TaskStatusCompleted, EventKinds: []string{"task.accepted", "task.running", "task.completed"}},
		},
		CollectedAt: time.Now().UTC(),
	}
	if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
		t.Fatalf("RecordDegradationTrace() error = %v", err)
	}

	memories, err := store.ListRAGMemories(ctx, "session", task.SessionID, 20)
	if err != nil {
		t.Fatalf("ListRAGMemories() error = %v", err)
	}
	if len(memories) < 2 {
		t.Fatalf("len(memories) = %d, want at least 2", len(memories))
	}
	if !hasMemoryType(memories, "adaptive_decision") {
		t.Fatal("adaptive_decision memory not found")
	}
	if !hasMemoryType(memories, degradationTraceMemoryType) {
		t.Fatalf("%s memory not found", degradationTraceMemoryType)
	}

	chunks, err := store.ListVectorChunks(ctx, task.SessionID, task.Context.Branch, 40)
	if err != nil {
		t.Fatalf("ListVectorChunks() error = %v", err)
	}
	if len(chunks) == 0 {
		t.Fatal("ListVectorChunks() returned no chunks")
	}
	if !hasChunkSourceKind(chunks, "adaptive_decision") {
		t.Fatal("adaptive_decision chunks not found")
	}
	if !hasChunkSourceKind(chunks, degradationTraceMemoryType) {
		t.Fatalf("%s chunks not found", degradationTraceMemoryType)
	}

	results, err := manager.SearchVectorContext(ctx, domain.Task{
		ID:        "task-memory-query",
		SessionID: task.SessionID,
		Type:      domain.TaskTypeResearch,
		Input: domain.TaskInput{
			Description: "Find adaptive and degradation telemetry for orchestrator scheduling and execution quality.",
		},
		Context: domain.TaskContext{Branch: task.Context.Branch, Project: "go-core"},
	}, 10)
	if err != nil {
		t.Fatalf("SearchVectorContext() error = %v", err)
	}
	if len(results) == 0 {
		t.Fatal("SearchVectorContext() returned no results")
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
