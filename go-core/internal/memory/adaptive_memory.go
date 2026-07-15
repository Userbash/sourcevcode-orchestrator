package memory

import (
	"context"
	"fmt"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func (m *Manager) RecordAdaptiveDecision(ctx context.Context, task domain.Task, decision domain.AdaptiveDecision) error {
	if m == nil || m.store == nil {
		return nil
	}
	sessionID := firstNonEmpty(task.SessionID, task.ID)
	branch := task.Context.Branch
	text := strings.TrimSpace(strings.Join([]string{
		strings.TrimSpace(task.Input.Description),
		fmt.Sprintf("adaptive mode=%s reason=%s", decision.Mode, strings.TrimSpace(decision.Reason)),
		fmt.Sprintf("healthy=%d degraded=%d suppressed=%d avg_error_rate=%.2f max_parallelism=%d",
			decision.Diagnostics.HealthyAgents,
			decision.Diagnostics.DegradedAgents,
			decision.Diagnostics.SuppressedAgents,
			decision.Diagnostics.AverageErrorRate,
			decision.MaxParallelism,
		),
	}, "\n\n"))
	if text == "" {
		return nil
	}
	metadata := map[string]any{
		"task_id":            task.ID,
		"task_type":          string(task.Type),
		"priority":           string(task.Priority),
		"complexity":         string(task.Complexity),
		"adaptive_mode":      string(decision.Mode),
		"adaptive_reason":    decision.Reason,
		"max_parallelism":    decision.MaxParallelism,
		"recovery_actions":   append([]string(nil), decision.RecoveryActions...),
		"suppressed_agents":  append([]string(nil), decision.SuppressedAgents...),
		"healthy_agents":     decision.Diagnostics.HealthyAgents,
		"degraded_agents":    decision.Diagnostics.DegradedAgents,
		"suppressed_count":   decision.Diagnostics.SuppressedAgents,
		"offline_agents":     decision.Diagnostics.OfflineAgents,
		"average_error_rate": decision.Diagnostics.AverageErrorRate,
		"source_kind":        "adaptive_decision",
	}
	return m.Remember(ctx, domain.RAGMemoryRecord{
		MemoryType: "adaptive_decision",
		Scope:      "session",
		OwnerID:    sessionID,
		Summary:    text,
		Content: map[string]any{
			"task_id":         task.ID,
			"description":     strings.TrimSpace(task.Input.Description),
			"adaptive_mode":   string(decision.Mode),
			"adaptive_reason": decision.Reason,
			"decision":        decision,
		},
		Metadata:   metadata,
		Confidence: 0.80,
		Importance: 0.70,
		Branch:     branch,
	})
}
