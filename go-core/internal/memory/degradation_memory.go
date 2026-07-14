package memory

import (
	"context"
	"fmt"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const degradationTraceMemoryType = "degradation_trace"

func (m *Manager) RecordDegradationTrace(ctx context.Context, trace domain.DegradationTrace) error {
	if m == nil || m.store == nil {
		return nil
	}
	sessionID := normalizeSessionID(firstNonEmpty(trace.SessionID, trace.TraceID))
	branch := normalizeBranch(trace.Branch)
	traceID := strings.TrimSpace(trace.TraceID)
	if traceID == "" {
		traceID = vectorChunkID(sessionID, firstNonEmpty(branch, "default"), degradationTraceMemoryType, firstNonEmpty(trace.Subject, trace.Scenario, "trace"), 0, fmt.Sprintf("%d", time.Now().UTC().UnixNano()))
	}
	collectedAt := trace.CollectedAt
	if collectedAt.IsZero() {
		collectedAt = time.Now().UTC()
	}
	content := map[string]any{
		"trace_id":                  traceID,
		"suite_id":                  strings.TrimSpace(trace.SuiteID),
		"subject":                   strings.TrimSpace(trace.Subject),
		"scenario":                  strings.TrimSpace(trace.Scenario),
		"task_type":                 string(trace.TaskType),
		"workflow_count":            trace.WorkflowCount,
		"completed_count":           trace.CompletedCount,
		"failed_count":              trace.FailedCount,
		"dead_lettered_count":       trace.DeadLetteredCount,
		"parallel_width":            trace.ParallelWidth,
		"total_latency_ms":          trace.TotalLatencyMS,
		"mean_queue_latency_ms":     trace.MeanQueueLatencyMS,
		"mean_execution_latency_ms": trace.MeanExecutionLatencyMS,
		"throughput_per_second":     trace.ThroughputPerSecond,
		"collected_at":              collectedAt.Format(time.RFC3339Nano),
		"samples":                   degradationSamplesPayload(trace.Samples),
	}
	metadata := cloneMap(trace.Metadata)
	if metadata == nil {
		metadata = map[string]any{}
	}
	metadata["source_kind"] = degradationTraceMemoryType
	metadata["subject"] = strings.TrimSpace(trace.Subject)
	metadata["scenario"] = strings.TrimSpace(trace.Scenario)
	metadata["task_type"] = string(trace.TaskType)
	metadata["suite_id"] = strings.TrimSpace(trace.SuiteID)
	metadata["trace_id"] = traceID
	metadata["workflow_count"] = trace.WorkflowCount
	metadata["throughput_per_second"] = trace.ThroughputPerSecond
	metadata["parallel_width"] = trace.ParallelWidth

	return m.Remember(ctx, domain.RAGMemoryRecord{
		MemoryID:   traceID,
		MemoryType: degradationTraceMemoryType,
		Scope:      "session",
		OwnerID:    sessionID,
		Content:    content,
		Summary:    degradationTraceSummary(trace, traceID),
		Metadata:   metadata,
		Confidence: 0.9,
		Importance: degradationTraceImportance(trace),
		Branch:     branch,
		CreatedAt:  collectedAt,
		UpdatedAt:  collectedAt,
	})
}

func degradationSamplesPayload(samples []domain.DegradationSample) []map[string]any {
	if len(samples) == 0 {
		return nil
	}
	payload := make([]map[string]any, 0, len(samples))
	for _, sample := range samples {
		payload = append(payload, map[string]any{
			"task_id":              sample.TaskID,
			"parent_task_id":       sample.ParentTaskID,
			"agent_id":             sample.AgentID,
			"status":               string(sample.Status),
			"queue_latency_ms":     sample.QueueLatencyMS,
			"execution_latency_ms": sample.ExecutionLatencyMS,
			"total_latency_ms":     sample.TotalLatencyMS,
			"event_kinds":          append([]string(nil), sample.EventKinds...),
		})
	}
	return payload
}

func degradationTraceSummary(trace domain.DegradationTrace, traceID string) string {
	subject := firstNonEmpty(strings.TrimSpace(trace.Subject), strings.TrimSpace(trace.Scenario), traceID)
	return fmt.Sprintf(
		"Degradation trace %s for %s: workflows=%d completed=%d failed=%d dead_lettered=%d total_latency_ms=%d throughput_per_second=%.2f mean_queue_latency_ms=%d mean_execution_latency_ms=%d parallel_width=%d.",
		traceID,
		subject,
		trace.WorkflowCount,
		trace.CompletedCount,
		trace.FailedCount,
		trace.DeadLetteredCount,
		trace.TotalLatencyMS,
		trace.ThroughputPerSecond,
		trace.MeanQueueLatencyMS,
		trace.MeanExecutionLatencyMS,
		trace.ParallelWidth,
	)
}

func degradationTraceImportance(trace domain.DegradationTrace) float64 {
	importance := 0.55
	if trace.FailedCount > 0 || trace.DeadLetteredCount > 0 {
		importance += 0.2
	}
	if trace.TotalLatencyMS > 0 {
		importance += minFloat(0.2, float64(trace.TotalLatencyMS)/10000.0)
	}
	if trace.WorkflowCount > 1 {
		importance += minFloat(0.05, float64(trace.WorkflowCount)/100.0)
	}
	return clamp01(importance)
}
