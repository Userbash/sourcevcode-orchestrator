package selflearn

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

const selfLearningTraceMemoryType = "self_learning_trace"

type StoreTraceRecorder struct {
	store state.Store
	scope string
}

func NewStoreTraceRecorder(store state.Store, scope string) *StoreTraceRecorder {
	return &StoreTraceRecorder{
		store: store,
		scope: strings.TrimSpace(scope),
	}
}

func (r *StoreTraceRecorder) RecordTrace(ctx context.Context, trace domain.TraceRecord) error {
	if r == nil || r.store == nil {
		return fmt.Errorf("trace recorder is not configured")
	}
	record := normalizeTraceRecord(trace)
	memory, err := traceMemoryFromRecord(record, r.scope)
	if err != nil {
		return err
	}
	return r.store.UpsertRAGMemories(ctx, []domain.RAGMemoryRecord{memory})
}

func normalizeTraceRecord(trace domain.TraceRecord) domain.TraceRecord {
	record := trace
	if strings.TrimSpace(record.TraceID) == "" {
		record.TraceID = fmt.Sprintf("trace-%d", time.Now().UTC().UnixNano())
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = time.Now().UTC()
	}
	return record
}

func traceMemoryFromRecord(trace domain.TraceRecord, scope string) (domain.RAGMemoryRecord, error) {
	payload, err := tracePayload(trace)
	if err != nil {
		return domain.RAGMemoryRecord{}, fmt.Errorf("marshal trace: %w", err)
	}
	summary := strings.TrimSpace(trace.Prompt)
	if len(summary) > 160 {
		summary = summary[:160]
	}
	if summary == "" {
		summary = trace.TraceID
	}
	return domain.RAGMemoryRecord{
		MemoryID:   trace.TraceID,
		MemoryType: selfLearningTraceMemoryType,
		Scope:      firstNonEmpty(scope, "self_learning"),
		OwnerID:    firstNonEmpty(trace.SessionID, trace.TaskID, trace.TraceID),
		Content:    payload,
		Summary:    summary,
		Metadata: map[string]any{
			"status":     trace.Evaluation.Status,
			"score":      trace.Evaluation.Score,
			"provider":   trace.Provider,
			"model_name": trace.ModelName,
			"task_id":    trace.TaskID,
			"session_id": trace.SessionID,
		},
		Confidence: 1,
		Importance: trace.Evaluation.Score,
		CreatedAt:  trace.CreatedAt,
		UpdatedAt:  trace.CreatedAt,
	}, nil
}

func decodeTraceMemory(memory domain.RAGMemoryRecord) (domain.TraceRecord, bool) {
	if memory.MemoryType != selfLearningTraceMemoryType {
		return domain.TraceRecord{}, false
	}
	payload, err := json.Marshal(memory.Content)
	if err != nil {
		return domain.TraceRecord{}, false
	}
	var trace domain.TraceRecord
	if err := json.Unmarshal(payload, &trace); err != nil {
		return domain.TraceRecord{}, false
	}
	return normalizeTraceRecord(trace), true
}

func tracePayload(trace domain.TraceRecord) (map[string]any, error) {
	bytes, err := json.Marshal(trace)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(bytes, &payload); err != nil {
		return nil, err
	}
	return payload, nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed != "" {
			return trimmed
		}
	}
	return ""
}
