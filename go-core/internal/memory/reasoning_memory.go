package memory

import (
	"context"
	"fmt"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const reasoningTraceMemoryType = "reasoning_trace"

func (m *Manager) RecordReasoningTrace(ctx context.Context, trace domain.ReasoningTrace) error {
	if m == nil || m.store == nil {
		return nil
	}
	sessionID := normalizeSessionID(firstNonEmpty(trace.SessionID, trace.TaskID, trace.TraceID))
	branch := normalizeBranch(trace.Branch)
	traceID := strings.TrimSpace(trace.TraceID)
	if traceID == "" {
		traceID = vectorChunkID(sessionID, firstNonEmpty(branch, "default"), reasoningTraceMemoryType, firstNonEmpty(trace.TaskID, string(trace.TaskType), "trace"), 0, fmt.Sprintf("%d", time.Now().UTC().UnixNano()))
	}
	createdAt := trace.CreatedAt
	if createdAt.IsZero() {
		createdAt = time.Now().UTC()
	}
	content := map[string]any{
		"trace_id":               traceID,
		"task_id":                strings.TrimSpace(trace.TaskID),
		"parent_task_id":         strings.TrimSpace(trace.ParentTaskID),
		"agent_id":               strings.TrimSpace(trace.AgentID),
		"provider":               strings.TrimSpace(trace.Provider),
		"model_name":             strings.TrimSpace(trace.ModelName),
		"task_type":              string(trace.TaskType),
		"prompt_summary":         strings.TrimSpace(trace.PromptSummary),
		"reflection_summary":     strings.TrimSpace(trace.ReflectionSummary),
		"result_summary":         strings.TrimSpace(trace.ResultSummary),
		"reasoning_mode":         strings.TrimSpace(trace.ReasoningMode),
		"retrieval_used":         trace.RetrievalUsed,
		"vector_memory_count":    trace.VectorMemoryCount,
		"route_memory_count":     trace.RouteMemoryCount,
		"reasoning_memory_count": trace.ReasoningMemoryCount,
		"latency_ms":             trace.LatencyMS,
		"decision_points":        reasoningDecisionPointsPayload(trace.DecisionPoints),
		"next_questions":         append([]string(nil), trace.NextQuestions...),
		"created_at":             createdAt.Format(time.RFC3339Nano),
	}
	metadata := cloneMap(trace.Metadata)
	if metadata == nil {
		metadata = map[string]any{}
	}
	metadata["source_kind"] = reasoningTraceMemoryType
	metadata["memory_type"] = reasoningTraceMemoryType
	metadata["trace_id"] = traceID
	metadata["task_id"] = strings.TrimSpace(trace.TaskID)
	metadata["agent_id"] = strings.TrimSpace(trace.AgentID)
	metadata["provider"] = strings.TrimSpace(trace.Provider)
	metadata["model_name"] = strings.TrimSpace(trace.ModelName)
	metadata["task_type"] = string(trace.TaskType)
	metadata["reasoning_mode"] = strings.TrimSpace(trace.ReasoningMode)
	metadata["retrieval_used"] = trace.RetrievalUsed
	metadata["vector_memory_count"] = trace.VectorMemoryCount
	metadata["route_memory_count"] = trace.RouteMemoryCount
	metadata["reasoning_memory_count"] = trace.ReasoningMemoryCount
	metadata["latency_ms"] = trace.LatencyMS

	return m.Remember(ctx, domain.RAGMemoryRecord{
		MemoryID:   traceID,
		MemoryType: reasoningTraceMemoryType,
		Scope:      "session",
		OwnerID:    sessionID,
		Content:    content,
		Summary:    reasoningTraceSummary(trace, traceID),
		Metadata:   metadata,
		Confidence: 0.86,
		Importance: reasoningTraceImportance(trace),
		Branch:     branch,
		CreatedAt:  createdAt,
		UpdatedAt:  createdAt,
	})
}

func reasoningDecisionPointsPayload(points []domain.ReasoningDecisionPoint) []map[string]any {
	if len(points) == 0 {
		return nil
	}
	payload := make([]map[string]any, 0, len(points))
	for _, point := range points {
		payload = append(payload, map[string]any{
			"kind":     strings.TrimSpace(point.Kind),
			"summary":  strings.TrimSpace(point.Summary),
			"outcome":  strings.TrimSpace(point.Outcome),
			"metadata": cloneMap(point.Metadata),
		})
	}
	return payload
}

func reasoningTraceSummary(trace domain.ReasoningTrace, traceID string) string {
	subject := firstNonEmpty(strings.TrimSpace(trace.ResultSummary), strings.TrimSpace(trace.ReflectionSummary), strings.TrimSpace(trace.PromptSummary), traceID)
	return fmt.Sprintf(
		"Reasoning trace %s for %s: provider=%s model=%s task_type=%s mode=%s retrieval_used=%t vector_memory_count=%d route_memory_count=%d reasoning_memory_count=%d latency_ms=%d.",
		traceID,
		subject,
		strings.TrimSpace(trace.Provider),
		strings.TrimSpace(trace.ModelName),
		trace.TaskType,
		strings.TrimSpace(trace.ReasoningMode),
		trace.RetrievalUsed,
		trace.VectorMemoryCount,
		trace.RouteMemoryCount,
		trace.ReasoningMemoryCount,
		trace.LatencyMS,
	)
}

func reasoningTraceImportance(trace domain.ReasoningTrace) float64 {
	importance := 0.58
	if trace.RetrievalUsed {
		importance += 0.08
	}
	if trace.VectorMemoryCount > 0 {
		importance += minFloat(0.12, float64(trace.VectorMemoryCount)/20.0)
	}
	if len(trace.DecisionPoints) > 1 {
		importance += minFloat(0.1, float64(len(trace.DecisionPoints))/10.0)
	}
	if trace.LatencyMS > 0 {
		importance += minFloat(0.08, float64(trace.LatencyMS)/12000.0)
	}
	return clamp01(importance)
}
