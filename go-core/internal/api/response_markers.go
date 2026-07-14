package api

import (
	"context"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func workflowResponsePayload(ctx context.Context, record domain.WorkflowRecord, transport string) map[string]any {
	acceptance := record.Acceptance
	meta := metadataFromContext(ctx)
	marker := map[string]any{
		"answered_by":       "go-core-orchestrator",
		"answered_for":      meta.AnsweredFor,
		"request_origin":    meta.RequestOrigin,
		"client_kind":       meta.ClientKind,
		"transport":         transport,
		"execution_mode":    "orchestrated",
		"task_id":           record.Task.ID,
		"task_status":       acceptance.Status,
		"selected_agent":    acceptance.AgentID,
		"selected_provider": acceptance.Provider,
		"selected_model":    acceptance.ModelName,
		"capability":        acceptance.Capability,
	}
	if acceptance.AgentID == "" && acceptance.Provider == "" && acceptance.ModelName == "" {
		marker["route_state"] = "orchestrator_only"
	} else {
		marker["route_state"] = "delegated_to_agent"
	}
	return addResponseMetadata(ctx, map[string]any{
		"status":          "ok",
		"workflow":        record,
		"response_origin": marker,
	})
}
