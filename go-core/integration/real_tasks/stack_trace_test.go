package realtasks

import (
	"context"
	"encoding/json"
	"fmt"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

func TestIntegrationRealTaskTraceCapturesTimingErrorsAndDeliverySignals(t *testing.T) {
	tests := []struct {
		name             string
		task             domain.Task
		agentDelay       time.Duration
		agentResult      domain.AgentResult
		wantFailedTasks  int
		wantCheckpoint   domain.ParallelPlanStatus
		wantFailureEvent bool
	}{
		{
			name: "success",
			task: domain.Task{
				ID:               "integration-stack-trace-success",
				SessionID:        "integration-session-stack-trace-success",
				Type:             domain.TaskTypeCode,
				Priority:         domain.PriorityHigh,
				Complexity:       domain.ComplexityHigh,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Trace a multi-step orchestrator workload and persist lifecycle timings through the full stack.",
					Files:              []string{"internal/kernel/orchestrator.go", "internal/delivery/worker_pool.go", "internal/api/http.go"},
					AcceptanceCriteria: []string{"capture queue latency", "capture execution latency", "persist degradation trace"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			agentDelay: 20 * time.Millisecond,
			agentResult: domain.AgentResult{
				Status: domain.TaskStatusCompleted,
				Output: domain.ResultOutput{
					Summary:   "stack trace integration success",
					Artifacts: map[string]any{"trace_mode": "success"},
				},
			},
			wantCheckpoint: domain.ParallelPlanStatusCompleted,
		},
		{
			name: "failure",
			task: domain.Task{
				ID:               "integration-stack-trace-failure",
				SessionID:        "integration-session-stack-trace-failure",
				Type:             domain.TaskTypeReview,
				Priority:         domain.PriorityHigh,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Trace a failing orchestrator workload and preserve timing plus error evidence end to end.",
					Files:              []string{"internal/kernel/advanced_planner.go", "internal/kernel/orchestrator.go"},
					AcceptanceCriteria: []string{"record task.failed events", "preserve agent error payload", "persist failure trace"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			agentDelay: 15 * time.Millisecond,
			agentResult: domain.AgentResult{
				Status: domain.TaskStatusFailed,
				Errors: []string{"synthetic agent failure"},
				Output: domain.ResultOutput{
					Summary:   "stack trace integration failure",
					Artifacts: map[string]any{"trace_mode": "failure"},
				},
			},
			wantFailedTasks:  1,
			wantCheckpoint:   domain.ParallelPlanStatusCompleted,
			wantFailureEvent: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("GO_CORE_SUBMIT_MODE", "async")
			agent := &fakeAgent{
				info: domain.AgentInfo{
					ID:           "coder-local",
					Type:         "coding",
					Provider:     "local",
					ModelName:    "qwen2.5:32b-instruct-q4_k_m",
					Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
					Status:       domain.AgentStatusReady,
				},
				delay:  tt.agentDelay,
				result: tt.agentResult,
			}
			orchestrator, store, _ := newIntegrationOrchestrator(t, agent)

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()

			manager := memory.NewManager(store)
			seedDocument := domain.RAGDocument{
				DocumentID:  tt.task.ID + "-rag-seed",
				Scope:       "session",
				OwnerType:   "session",
				OwnerID:     tt.task.SessionID,
				Branch:      tt.task.Context.Branch,
				SourceType:  "integration_trace_seed",
				SourceRef:   tt.task.ID,
				Title:       "Orchestrator trace retrieval baseline",
				ContentText: tt.task.Input.Description + " queue latency execution latency degradation trace worker pools memory context vector retrieval telemetry",
				Metadata: map[string]any{
					"source_kind": "integration_trace_seed",
					"keywords":    []string{"orchestrator", "latency", "worker-pool", "degradation", "retrieval"},
				},
				CreatedAt: time.Now().UTC(),
			}
			if err := manager.IngestDocument(ctx, seedDocument); err != nil {
				t.Fatalf("IngestDocument() error = %v", err)
			}
			preMemories, err := store.ListRAGMemories(ctx, "session", tt.task.SessionID, 50)
			if err != nil {
				t.Fatalf("ListRAGMemories(pre) error = %v", err)
			}
			preChunks, err := store.ListVectorChunks(ctx, tt.task.SessionID, tt.task.Context.Branch, 100)
			if err != nil {
				t.Fatalf("ListVectorChunks(pre) error = %v", err)
			}
			memBefore := readMemStatsSnapshot()

			run, err := orchestrator.RunExecutionPlan(ctx, tt.task)
			if err != nil {
				t.Fatalf("RunExecutionPlan() error = %v", err)
			}
			memAfter := readMemStatsSnapshot()
			if run.Checkpoint.Status != tt.wantCheckpoint {
				t.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, tt.wantCheckpoint)
			}
			if len(run.Workflows) == 0 {
				t.Fatal("run.Workflows is empty")
			}

			events := orchestrator.RuntimeEventSnapshot("tasks")
			report := buildExecutionTraceReport(tt.name, run, events, orchestrator.DeliveryHealthSnapshot())
			trace := degradationTraceFromExecutionReport(tt.task, report)

			if err := manager.RecordDegradationTrace(ctx, trace); err != nil {
				t.Fatalf("RecordDegradationTrace() error = %v", err)
			}

			if report.TotalLatencyMS <= 0 {
				t.Fatalf("report.TotalLatencyMS = %d, want > 0", report.TotalLatencyMS)
			}
			if len(report.Tasks) != len(run.Workflows) {
				t.Fatalf("len(report.Tasks) = %d, want %d", len(report.Tasks), len(run.Workflows))
			}
			if report.Delivery == nil {
				t.Fatal("report.Delivery = nil")
			}
			if _, ok := report.Delivery["tracked"]; !ok {
				t.Fatalf("delivery snapshot missing tracked field: %#v", report.Delivery)
			}
			if _, ok := report.Delivery["accepted"]; !ok {
				t.Fatalf("delivery snapshot missing accepted field: %#v", report.Delivery)
			}

			nonZeroExecution := 0
			reportedFailures := 0
			failureSamplesWithErrors := 0
			for _, sample := range report.Tasks {
				if sample.ExecutionLatencyMS > 0 {
					nonZeroExecution++
				}
				if sample.Status == string(domain.TaskStatusFailed) {
					reportedFailures++
					if len(sample.Errors) > 0 {
						failureSamplesWithErrors++
					}
				}
			}
			if nonZeroExecution == 0 {
				t.Fatal("no task sample recorded non-zero execution latency")
			}
			if reportedFailures < tt.wantFailedTasks {
				t.Fatalf("reportedFailures = %d, want at least %d", reportedFailures, tt.wantFailedTasks)
			}
			if tt.wantFailedTasks > 0 && failureSamplesWithErrors == 0 {
				t.Fatal("failed scenario did not preserve sample errors")
			}

			if tt.wantFailureEvent && !hasEventKind(events, string(taskEventFailed)) {
				t.Fatal("task.failed event not found in runtime trace")
			}
			if !tt.wantFailureEvent && hasEventKind(events, string(taskEventFailed)) {
				t.Fatal("unexpected task.failed event in success runtime trace")
			}

			postMemories, err := store.ListRAGMemories(ctx, "session", tt.task.SessionID, 50)
			if err != nil {
				t.Fatalf("ListRAGMemories(post) error = %v", err)
			}
			postChunks, err := store.ListVectorChunks(ctx, tt.task.SessionID, tt.task.Context.Branch, 100)
			if err != nil {
				t.Fatalf("ListVectorChunks(post) error = %v", err)
			}
			retrievalResults, err := manager.SearchVectorContext(ctx, domain.Task{
				ID:        tt.task.ID + "-retrieval-query",
				SessionID: tt.task.SessionID,
				Type:      domain.TaskTypeResearch,
				Input: domain.TaskInput{
					Description: "Find orchestrator latency degradation trace memory context and worker pool telemetry.",
				},
				Context: domain.TaskContext{Branch: tt.task.Context.Branch, Project: tt.task.Context.Project},
			}, 10)
			if err != nil {
				t.Fatalf("SearchVectorContext() error = %v", err)
			}
			if len(retrievalResults) == 0 {
				t.Fatal("SearchVectorContext() returned no RAG results")
			}

			executedTasks := snapshotExecutedTasks(agent)
			executedWithMemoryContext := 0
			executedWithVectorHits := 0
			for _, executed := range executedTasks {
				memoryContext, ok := executed.RoutingHints["memory_context"].(map[string]any)
				if !ok {
					continue
				}
				executedWithMemoryContext++
				if hasVectorMemorySignal(memoryContext) {
					executedWithVectorHits++
				}
			}

			if !hasMemoryType(postMemories, "adaptive_decision") {
				t.Fatalf("session %s missing adaptive_decision memory", tt.task.SessionID)
			}
			if !hasMemoryType(postMemories, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace memory", tt.task.SessionID)
			}
			if !hasChunkSourceKind(postChunks, "degradation_trace") {
				t.Fatalf("session %s missing degradation_trace vector chunks", tt.task.SessionID)
			}
			if len(postMemories) < len(preMemories) {
				t.Fatalf("post memory count = %d, want >= %d", len(postMemories), len(preMemories))
			}
			if len(postChunks) < len(preChunks) {
				t.Fatalf("post vector chunk count = %d, want >= %d", len(postChunks), len(preChunks))
			}
			if executedWithMemoryContext == 0 {
				t.Fatal("no executed task captured memory_context in routing hints")
			}
			if executedWithVectorHits == 0 {
				t.Fatal("no executed task captured vector_memory_count > 0 in memory_context")
			}

			report.Memory = buildMemoryTraceReport(memBefore, memAfter)
			report.RAG = buildRAGTraceReport(preMemories, postMemories, preChunks, postChunks, retrievalResults, executedTasks)

			encoded, err := json.MarshalIndent(report, "", "  ")
			if err != nil {
				t.Fatalf("MarshalIndent(report) error = %v", err)
			}
			t.Logf("execution trace report:\n%s", encoded)
		})
	}
}

type executionTraceReport struct {
	Scenario               string              `json:"scenario"`
	RootTaskID             string              `json:"root_task_id"`
	SessionID              string              `json:"session_id"`
	CheckpointStatus       string              `json:"checkpoint_status"`
	WorkflowCount          int                 `json:"workflow_count"`
	CompletedCount         int                 `json:"completed_count"`
	FailedCount            int                 `json:"failed_count"`
	ParallelWidth          int                 `json:"parallel_width"`
	TotalLatencyMS         int64               `json:"total_latency_ms"`
	MeanQueueLatencyMS     int64               `json:"mean_queue_latency_ms"`
	MeanExecutionLatencyMS int64               `json:"mean_execution_latency_ms"`
	ThroughputPerSecond    float64             `json:"throughput_per_second"`
	Delivery               map[string]any      `json:"delivery"`
	Tasks                  []taskTraceSample   `json:"tasks"`
	EventKinds             map[string][]string `json:"event_kinds"`
	Memory                 *memoryTraceReport  `json:"memory,omitempty"`
	RAG                    *ragTraceReport     `json:"rag,omitempty"`
}

type taskTraceSample struct {
	TaskID             string   `json:"task_id"`
	ParentTaskID       string   `json:"parent_task_id,omitempty"`
	AgentID            string   `json:"agent_id,omitempty"`
	Status             string   `json:"status"`
	QueueLatencyMS     int64    `json:"queue_latency_ms,omitempty"`
	ExecutionLatencyMS int64    `json:"execution_latency_ms,omitempty"`
	TotalLatencyMS     int64    `json:"total_latency_ms,omitempty"`
	Errors             []string `json:"errors,omitempty"`
	EventKinds         []string `json:"event_kinds,omitempty"`
}

type memStatsSnapshot struct {
	Alloc       uint64
	TotalAlloc  uint64
	HeapAlloc   uint64
	HeapObjects uint64
	NumGC       uint32
}

type memoryTraceReport struct {
	AllocBeforeBytes      uint64 `json:"alloc_before_bytes"`
	AllocAfterBytes       uint64 `json:"alloc_after_bytes"`
	AllocDeltaBytes       int64  `json:"alloc_delta_bytes"`
	TotalAllocBeforeBytes uint64 `json:"total_alloc_before_bytes"`
	TotalAllocAfterBytes  uint64 `json:"total_alloc_after_bytes"`
	TotalAllocDeltaBytes  int64  `json:"total_alloc_delta_bytes"`
	HeapAllocBeforeBytes  uint64 `json:"heap_alloc_before_bytes"`
	HeapAllocAfterBytes   uint64 `json:"heap_alloc_after_bytes"`
	HeapAllocDeltaBytes   int64  `json:"heap_alloc_delta_bytes"`
	HeapObjectsBefore     uint64 `json:"heap_objects_before"`
	HeapObjectsAfter      uint64 `json:"heap_objects_after"`
	HeapObjectsDelta      int64  `json:"heap_objects_delta"`
	NumGCBefore           uint32 `json:"num_gc_before"`
	NumGCAfter            uint32 `json:"num_gc_after"`
	NumGCDelta            int64  `json:"num_gc_delta"`
}

type ragTraceReport struct {
	PreMemoryCount            int      `json:"pre_memory_count"`
	PostMemoryCount           int      `json:"post_memory_count"`
	PreVectorChunkCount       int      `json:"pre_vector_chunk_count"`
	PostVectorChunkCount      int      `json:"post_vector_chunk_count"`
	AdaptiveDecisionCount     int      `json:"adaptive_decision_count"`
	DegradationTraceCount     int      `json:"degradation_trace_count"`
	ExecutedWithMemoryContext int      `json:"executed_with_memory_context"`
	ExecutedWithVectorHits    int      `json:"executed_with_vector_hits"`
	RetrievalHitCount         int      `json:"retrieval_hit_count"`
	RetrievalSourceKinds      []string `json:"retrieval_source_kinds,omitempty"`
}

func buildExecutionTraceReport(scenario string, run domain.ExecutionPlanRun, events []domain.StreamEvent, delivery map[string]any) executionTraceReport {
	metrics := collectMetrics(run, events)
	timelines := taskEventTimelines(events)
	report := executionTraceReport{
		Scenario:               scenario,
		RootTaskID:             run.Task.ID,
		SessionID:              run.Task.SessionID,
		CheckpointStatus:       string(run.Checkpoint.Status),
		WorkflowCount:          len(run.Workflows),
		ParallelWidth:          parallelWidthFromRun(run),
		TotalLatencyMS:         metrics.Duration.Milliseconds(),
		MeanQueueLatencyMS:     metrics.MeanQueueLatency.Milliseconds(),
		MeanExecutionLatencyMS: metrics.MeanExecutionLatency.Milliseconds(),
		ThroughputPerSecond:    metrics.ThroughputPerSecond,
		Delivery:               delivery,
		Tasks:                  make([]taskTraceSample, 0, len(run.Workflows)),
		EventKinds:             make(map[string][]string, len(run.Workflows)),
	}

	for _, workflow := range run.Workflows {
		timeline := timelines[workflow.Task.ID]
		sample := taskTraceSample{
			TaskID:       workflow.Task.ID,
			ParentTaskID: workflow.Task.ParentTaskID,
			AgentID:      workflow.Acceptance.AgentID,
			Status:       string(workflow.Acceptance.Status),
			EventKinds:   stringifyTaskKinds(timeline.Kinds),
		}
		if !timeline.Queued.IsZero() && !timeline.Running.IsZero() {
			sample.QueueLatencyMS = timeline.Running.Sub(timeline.Queued).Milliseconds()
		}
		if !timeline.Running.IsZero() && !timeline.Completed.IsZero() {
			sample.ExecutionLatencyMS = timeline.Completed.Sub(timeline.Running).Milliseconds()
		}
		if !timeline.Accepted.IsZero() && !timeline.Completed.IsZero() {
			sample.TotalLatencyMS = timeline.Completed.Sub(timeline.Accepted).Milliseconds()
		}
		if workflow.Result != nil {
			sample.Errors = append(sample.Errors, workflow.Result.Errors...)
			if workflow.Result.Status != "" {
				sample.Status = string(workflow.Result.Status)
			}
		}
		report.EventKinds[sample.TaskID] = append([]string(nil), sample.EventKinds...)
		report.Tasks = append(report.Tasks, sample)
		if sample.Status == string(domain.TaskStatusFailed) {
			report.FailedCount++
			continue
		}
		if sample.Status == string(domain.TaskStatusCompleted) {
			report.CompletedCount++
		}
	}
	return report
}

func degradationTraceFromExecutionReport(task domain.Task, report executionTraceReport) domain.DegradationTrace {
	samples := make([]domain.DegradationSample, 0, len(report.Tasks))
	for _, sample := range report.Tasks {
		samples = append(samples, domain.DegradationSample{
			TaskID:             sample.TaskID,
			ParentTaskID:       sample.ParentTaskID,
			AgentID:            sample.AgentID,
			Status:             domain.TaskStatus(sample.Status),
			QueueLatencyMS:     sample.QueueLatencyMS,
			ExecutionLatencyMS: sample.ExecutionLatencyMS,
			TotalLatencyMS:     sample.TotalLatencyMS,
			EventKinds:         append([]string(nil), sample.EventKinds...),
		})
	}
	return domain.DegradationTrace{
		TraceID:                task.ID + "-stack-trace",
		SuiteID:                "integration_stack_trace",
		Subject:                "full stack execution trace",
		SessionID:              task.SessionID,
		Branch:                 task.Context.Branch,
		Scenario:               report.Scenario,
		TaskType:               task.Type,
		WorkflowCount:          report.WorkflowCount,
		CompletedCount:         report.CompletedCount,
		FailedCount:            report.FailedCount,
		ParallelWidth:          report.ParallelWidth,
		TotalLatencyMS:         report.TotalLatencyMS,
		MeanQueueLatencyMS:     report.MeanQueueLatencyMS,
		MeanExecutionLatencyMS: report.MeanExecutionLatencyMS,
		ThroughputPerSecond:    report.ThroughputPerSecond,
		Samples:                samples,
		Metadata: map[string]any{
			"root_task_id":       report.RootTaskID,
			"checkpoint_status":  report.CheckpointStatus,
			"delivery_snapshot":  report.Delivery,
			"task_event_kinds":   report.EventKinds,
			"trace_capture_mode": "integration",
		},
		CollectedAt: time.Now().UTC(),
	}
}

func stringifyTaskKinds(kinds []taskEventKind) []string {
	out := make([]string, 0, len(kinds))
	for _, kind := range kinds {
		out = append(out, string(kind))
	}
	return out
}

func hasEventKind(events []domain.StreamEvent, want string) bool {
	for _, event := range events {
		if event.Kind == want {
			return true
		}
	}
	return false
}

func readMemStatsSnapshot() memStatsSnapshot {
	runtime.GC()
	var stats runtime.MemStats
	runtime.ReadMemStats(&stats)
	return memStatsSnapshot{
		Alloc:       stats.Alloc,
		TotalAlloc:  stats.TotalAlloc,
		HeapAlloc:   stats.HeapAlloc,
		HeapObjects: stats.HeapObjects,
		NumGC:       stats.NumGC,
	}
}

func buildMemoryTraceReport(before, after memStatsSnapshot) *memoryTraceReport {
	return &memoryTraceReport{
		AllocBeforeBytes:      before.Alloc,
		AllocAfterBytes:       after.Alloc,
		AllocDeltaBytes:       int64(after.Alloc) - int64(before.Alloc),
		TotalAllocBeforeBytes: before.TotalAlloc,
		TotalAllocAfterBytes:  after.TotalAlloc,
		TotalAllocDeltaBytes:  int64(after.TotalAlloc) - int64(before.TotalAlloc),
		HeapAllocBeforeBytes:  before.HeapAlloc,
		HeapAllocAfterBytes:   after.HeapAlloc,
		HeapAllocDeltaBytes:   int64(after.HeapAlloc) - int64(before.HeapAlloc),
		HeapObjectsBefore:     before.HeapObjects,
		HeapObjectsAfter:      after.HeapObjects,
		HeapObjectsDelta:      int64(after.HeapObjects) - int64(before.HeapObjects),
		NumGCBefore:           before.NumGC,
		NumGCAfter:            after.NumGC,
		NumGCDelta:            int64(after.NumGC) - int64(before.NumGC),
	}
}

func buildRAGTraceReport(preMemories, postMemories []domain.RAGMemoryRecord, preChunks, postChunks []domain.VectorChunk, retrievalResults []domain.VectorSearchResult, executedTasks []domain.Task) *ragTraceReport {
	sourceKinds := make(map[string]struct{}, len(retrievalResults))
	for _, result := range retrievalResults {
		if kind, ok := result.Chunk.Metadata["source_kind"].(string); ok && kind != "" {
			sourceKinds[kind] = struct{}{}
		}
	}
	orderedSourceKinds := make([]string, 0, len(sourceKinds))
	for kind := range sourceKinds {
		orderedSourceKinds = append(orderedSourceKinds, kind)
	}
	sort.Strings(orderedSourceKinds)

	executedWithMemoryContext := 0
	executedWithVectorHits := 0
	for _, task := range executedTasks {
		memoryContext, ok := task.RoutingHints["memory_context"].(map[string]any)
		if !ok {
			continue
		}
		executedWithMemoryContext++
		if hasVectorMemorySignal(memoryContext) {
			executedWithVectorHits++
		}
	}

	return &ragTraceReport{
		PreMemoryCount:            len(preMemories),
		PostMemoryCount:           len(postMemories),
		PreVectorChunkCount:       len(preChunks),
		PostVectorChunkCount:      len(postChunks),
		AdaptiveDecisionCount:     countMemoriesByType(postMemories, "adaptive_decision"),
		DegradationTraceCount:     countMemoriesByType(postMemories, "degradation_trace"),
		ExecutedWithMemoryContext: executedWithMemoryContext,
		ExecutedWithVectorHits:    executedWithVectorHits,
		RetrievalHitCount:         len(retrievalResults),
		RetrievalSourceKinds:      orderedSourceKinds,
	}
}

func countMemoriesByType(memories []domain.RAGMemoryRecord, want string) int {
	count := 0
	for _, memory := range memories {
		if memory.MemoryType == want {
			count++
		}
	}
	return count
}

func hasVectorMemorySignal(memoryContext map[string]any) bool {
	switch count := memoryContext["vector_memory_count"].(type) {
	case int:
		if count > 0 {
			return true
		}
	case int32:
		if count > 0 {
			return true
		}
	case int64:
		if count > 0 {
			return true
		}
	case float32:
		if count > 0 {
			return true
		}
	case float64:
		if count > 0 {
			return true
		}
	}
	if hits, ok := memoryContext["vector_memory_hits"].([]map[string]any); ok && len(hits) > 0 {
		return true
	}
	if hits, ok := memoryContext["vector_memory_hits"].([]any); ok && len(hits) > 0 {
		return true
	}
	if strings.TrimSpace(fmt.Sprint(memoryContext["vector_memory_brief"])) != "" {
		return true
	}
	return false
}

func snapshotExecutedTasks(agent *fakeAgent) []domain.Task {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	return append([]domain.Task(nil), agent.executedTasks...)
}
