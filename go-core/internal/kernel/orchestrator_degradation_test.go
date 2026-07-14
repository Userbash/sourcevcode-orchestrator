package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

func TestRunExecutionPlanRecordsDegradationTraceAsVectorMemory(t *testing.T) {
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
			Summary:   "completed degradation test workload",
			Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 13}},
		},
	}})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	run, err := orchestrator.RunExecutionPlan(ctx, domain.Task{
		ID:               "task-plan-degradation-trace",
		SessionID:        "session-plan-degradation-trace",
		Type:             domain.TaskTypeCode,
		Priority:         domain.PriorityHigh,
		Complexity:       domain.ComplexityHigh,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		Input: domain.TaskInput{
			Description: "Implement a multi-file scheduler change and collect degradation telemetry.",
			Files: []string{
				"internal/kernel/orchestrator.go",
				"internal/kernel/advanced_planner.go",
				"internal/delivery/worker_pool.go",
			},
			AcceptanceCriteria: []string{"fan-out remains healthy", "latency telemetry is preserved"},
		},
		Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
		RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
	})
	if err != nil {
		t.Fatalf("RunExecutionPlan() error = %v", err)
	}

	trace := degradationTraceFromRun("real_tasks", "planner real-task degradation", run, orchestrator.RuntimeEventSnapshot("tasks"))
	manager := memory.NewManager(store)
	if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
		t.Fatalf("RecordDegradationTrace() error = %v", err)
	}

	memories, err := store.ListRAGMemories(ctx, "session", trace.SessionID, 10)
	if err != nil {
		t.Fatalf("ListRAGMemories() error = %v", err)
	}
	if len(memories) == 0 {
		t.Fatal("ListRAGMemories() returned no degradation memories")
	}
	if memories[0].MemoryType != "degradation_trace" {
		t.Fatalf("MemoryType = %s, want degradation_trace", memories[0].MemoryType)
	}
	if memories[0].Metadata["trace_id"] != trace.TraceID {
		t.Fatalf("metadata.trace_id = %v, want %s", memories[0].Metadata["trace_id"], trace.TraceID)
	}

	chunks, err := store.ListVectorChunks(ctx, trace.SessionID, trace.Branch, 20)
	if err != nil {
		t.Fatalf("ListVectorChunks() error = %v", err)
	}
	if len(chunks) == 0 {
		t.Fatal("ListVectorChunks() returned no degradation chunks")
	}
	foundChunk := false
	for _, chunk := range chunks {
		if chunk.Metadata["source_kind"] != "degradation_trace" {
			continue
		}
		foundChunk = true
		if len(chunk.Embedding) == 0 {
			t.Fatal("degradation chunk embedding = nil")
		}
		if chunk.Metadata["parallel_width"] != trace.ParallelWidth {
			t.Fatalf("chunk parallel_width = %v, want %d", chunk.Metadata["parallel_width"], trace.ParallelWidth)
		}
	}
	if !foundChunk {
		t.Fatal("no degradation_trace chunks found")
	}

	results, err := manager.SearchVectorContext(ctx, domain.Task{
		ID:        "task-degradation-query",
		SessionID: trace.SessionID,
		Type:      domain.TaskTypeResearch,
		Input: domain.TaskInput{
			Description: "Find degradation telemetry for planner throughput and queue latency.",
		},
		Context: domain.TaskContext{Branch: trace.Branch, Project: "go-core"},
	}, 5)
	if err != nil {
		t.Fatalf("SearchVectorContext() error = %v", err)
	}
	if len(results) == 0 {
		t.Fatal("SearchVectorContext() returned no degradation results")
	}
}

func degradationTraceFromRun(suiteID string, subject string, run domain.ExecutionPlanRun, events []domain.StreamEvent) domain.DegradationTrace {
	trace := domain.DegradationTrace{
		TraceID:       run.Task.ID + "-degradation-trace",
		SuiteID:       suiteID,
		Subject:       subject,
		SessionID:     run.Task.SessionID,
		Branch:        run.Task.Context.Branch,
		Scenario:      string(run.Task.Type),
		TaskType:      run.Task.Type,
		WorkflowCount: len(run.Workflows),
		ParallelWidth: parallelWidthFromRun(run),
		CollectedAt:   time.Now().UTC(),
		Metadata: map[string]any{
			"root_task_id": run.Task.ID,
			"checkpoint":   string(run.Checkpoint.Status),
		},
	}
	if !run.StartedAt.IsZero() && !run.CompletedAt.IsZero() {
		trace.TotalLatencyMS = run.CompletedAt.Sub(run.StartedAt).Milliseconds()
		durationSeconds := run.CompletedAt.Sub(run.StartedAt).Seconds()
		if durationSeconds > 0 {
			trace.ThroughputPerSecond = float64(len(run.Workflows)) / durationSeconds
		}
	}

	timelines := taskEventTimelines(events)
	var queueSum int64
	var executionSum int64
	for _, workflow := range run.Workflows {
		sample := domain.DegradationSample{
			TaskID:       workflow.Task.ID,
			ParentTaskID: workflow.Task.ParentTaskID,
			AgentID:      workflow.Acceptance.AgentID,
			Status:       normalizeTerminalStatus(workflow.Acceptance.Status),
			EventKinds:   eventKindsAsStrings(timelines[workflow.Task.ID].Kinds),
		}
		switch sample.Status {
		case domain.TaskStatusCompleted:
			trace.CompletedCount++
		case domain.TaskStatusFailed:
			trace.FailedCount++
		case domain.TaskStatusDeadLettered:
			trace.DeadLetteredCount++
		}

		timeline := timelines[workflow.Task.ID]
		if !timeline.Queued.IsZero() && !timeline.Running.IsZero() {
			sample.QueueLatencyMS = timeline.Running.Sub(timeline.Queued).Milliseconds()
			queueSum += sample.QueueLatencyMS
		}
		if !timeline.Running.IsZero() && !timeline.Completed.IsZero() {
			sample.ExecutionLatencyMS = timeline.Completed.Sub(timeline.Running).Milliseconds()
			executionSum += sample.ExecutionLatencyMS
		}
		switch {
		case !timeline.Queued.IsZero() && !timeline.Completed.IsZero():
			sample.TotalLatencyMS = timeline.Completed.Sub(timeline.Queued).Milliseconds()
		case !timeline.Accepted.IsZero() && !timeline.Completed.IsZero():
			sample.TotalLatencyMS = timeline.Completed.Sub(timeline.Accepted).Milliseconds()
		}
		trace.Samples = append(trace.Samples, sample)
	}
	if len(trace.Samples) > 0 {
		trace.MeanQueueLatencyMS = queueSum / int64(len(trace.Samples))
		trace.MeanExecutionLatencyMS = executionSum / int64(len(trace.Samples))
	}
	return trace
}

func parallelWidthFromRun(run domain.ExecutionPlanRun) int {
	maxWidth := 1
	for _, group := range run.PlanArtifact.ParallelGroups {
		if len(group) > maxWidth {
			maxWidth = len(group)
		}
	}
	return maxWidth
}

type taskEventTimeline struct {
	Accepted  time.Time
	Queued    time.Time
	Running   time.Time
	Completed time.Time
	Kinds     []taskEventKind
}

func taskEventTimelines(events []domain.StreamEvent) map[string]taskEventTimeline {
	timelines := make(map[string]taskEventTimeline, len(events))
	for _, event := range events {
		if event.EntityID == "" {
			continue
		}
		timeline := timelines[event.EntityID]
		kind := taskEventKind(event.Kind)
		timeline.Kinds = append(timeline.Kinds, kind)
		switch kind {
		case taskEventAccepted:
			if timeline.Accepted.IsZero() {
				timeline.Accepted = event.Timestamp
			}
		case taskEventQueued, taskEventDequeued:
			if timeline.Queued.IsZero() {
				timeline.Queued = event.Timestamp
			}
		case taskEventRunning:
			if timeline.Running.IsZero() {
				timeline.Running = event.Timestamp
			}
		case taskEventCompleted, taskEventResultReceived:
			if timeline.Completed.IsZero() {
				timeline.Completed = event.Timestamp
			}
		}
		timelines[event.EntityID] = timeline
	}
	return timelines
}

func eventKindsAsStrings(kinds []taskEventKind) []string {
	if len(kinds) == 0 {
		return nil
	}
	out := make([]string, 0, len(kinds))
	for _, kind := range kinds {
		out = append(out, string(kind))
	}
	return out
}

func normalizeTerminalStatus(status domain.TaskStatus) domain.TaskStatus {
	switch status {
	case domain.TaskStatusDone:
		return domain.TaskStatusCompleted
	default:
		return status
	}
}
