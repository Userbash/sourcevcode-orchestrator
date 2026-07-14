package memory

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestManagerRecordDegradationTraceStoresMemoryAndVectorChunks(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	manager := NewManager(store)
	ctx := context.Background()

	trace := domain.DegradationTrace{
		TraceID:                "trace-degradation-1",
		SuiteID:                "real_tasks",
		Subject:                "planner degradation trace",
		SessionID:              "session-degradation-1",
		Branch:                 "main",
		Scenario:               "code fanout",
		TaskType:               domain.TaskTypeCode,
		WorkflowCount:          3,
		CompletedCount:         3,
		ParallelWidth:          2,
		TotalLatencyMS:         1250,
		MeanQueueLatencyMS:     18,
		MeanExecutionLatencyMS: 390,
		ThroughputPerSecond:    2.4,
		Samples: []domain.DegradationSample{
			{
				TaskID:             "task-a",
				Status:             domain.TaskStatusCompleted,
				QueueLatencyMS:     10,
				ExecutionLatencyMS: 300,
				TotalLatencyMS:     350,
				EventKinds:         []string{"task.queued", "task.running", "task.completed"},
			},
			{
				TaskID:             "task-b",
				ParentTaskID:       "task-root",
				AgentID:            "coder-local",
				Status:             domain.TaskStatusCompleted,
				QueueLatencyMS:     26,
				ExecutionLatencyMS: 480,
				TotalLatencyMS:     530,
				EventKinds:         []string{"task.queued", "task.running", "task.completed"},
			},
		},
		Metadata: map[string]any{
			"project": "go-core",
			"subject": "planner degradation trace",
		},
		CollectedAt: time.Now().UTC(),
	}

	if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
		t.Fatalf("RecordDegradationTrace() error = %v", err)
	}

	memories, err := store.ListRAGMemories(ctx, "session", "session-degradation-1", 10)
	if err != nil {
		t.Fatalf("ListRAGMemories() error = %v", err)
	}
	if len(memories) == 0 {
		t.Fatal("ListRAGMemories() returned no records")
	}
	memory := memories[0]
	if memory.MemoryType != degradationTraceMemoryType {
		t.Fatalf("MemoryType = %s, want %s", memory.MemoryType, degradationTraceMemoryType)
	}
	if memory.Metadata["source_kind"] != degradationTraceMemoryType {
		t.Fatalf("metadata.source_kind = %v, want %s", memory.Metadata["source_kind"], degradationTraceMemoryType)
	}
	if memory.Metadata["task_type"] != string(domain.TaskTypeCode) {
		t.Fatalf("metadata.task_type = %v, want %s", memory.Metadata["task_type"], domain.TaskTypeCode)
	}
	if len(memory.Embedding) == 0 {
		t.Fatal("memory.Embedding = nil, want generated embedding")
	}

	chunks, err := store.ListVectorChunks(ctx, "session-degradation-1", "main", 20)
	if err != nil {
		t.Fatalf("ListVectorChunks() error = %v", err)
	}
	if len(chunks) == 0 {
		t.Fatal("ListVectorChunks() returned no chunks")
	}
	foundSourceKind := false
	for _, chunk := range chunks {
		if chunk.Metadata["source_kind"] != degradationTraceMemoryType {
			continue
		}
		foundSourceKind = true
		if len(chunk.Embedding) == 0 {
			t.Fatal("chunk.Embedding = nil, want generated embedding")
		}
		if chunk.Metadata["subject"] != "planner degradation trace" {
			t.Fatalf("chunk.Metadata[subject] = %v, want planner degradation trace", chunk.Metadata["subject"])
		}
	}
	if !foundSourceKind {
		t.Fatalf("no vector chunk with source_kind=%s found", degradationTraceMemoryType)
	}
}
