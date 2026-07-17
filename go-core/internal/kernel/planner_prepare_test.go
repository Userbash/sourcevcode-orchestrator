package kernel

import (
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestPlannerPrepareKeepsParallelPlanAnalyzeStepDynamicallyRouted(t *testing.T) {
	planner := NewPlanner(NewModelSelector(nil))

	prepared, _ := planner.Prepare(domain.Task{
		ID:                 "task-parallel-analyze",
		Type:               domain.TaskTypePlan,
		RequiredCapability: "plan",
		RoutingHints: map[string]any{
			"parallel_plan": true,
			"plan_step_id":  "task-parallel-analyze",
		},
		Input: domain.TaskInput{Description: "Analyze task scope and constraints"},
	})

	if prepared.AssignedProvider != "" {
		t.Fatalf("AssignedProvider = %q, want empty for dynamic parallel plan analyze step", prepared.AssignedProvider)
	}
	if prepared.AssignedModel != "" {
		t.Fatalf("AssignedModel = %q, want empty for dynamic parallel plan analyze step", prepared.AssignedModel)
	}
	if got := toString(prepared.RoutingHints["selected_provider"]); got != "" {
		t.Fatalf("selected_provider = %q, want empty", got)
	}
	if got := toString(prepared.RoutingHints["selected_model"]); got != "" {
		t.Fatalf("selected_model = %q, want empty", got)
	}
	if got := toString(prepared.RoutingHints["required_capability"]); got != "plan" {
		t.Fatalf("required_capability = %q, want plan", got)
	}
}

func TestPlannerPrepareKeepsExecutionStepsPinned(t *testing.T) {
	planner := NewPlanner(NewModelSelector(nil))

	prepared, _ := planner.Prepare(domain.Task{
		ID:                 "task-parallel-code",
		Type:               domain.TaskTypeCode,
		RequiredCapability: "code",
		RoutingHints: map[string]any{
			"parallel_plan": true,
			"plan_step_id":  "task-parallel-code",
		},
		Input: domain.TaskInput{Description: "Implement the requested code changes"},
	})

	if prepared.AssignedProvider == "" {
		t.Fatal("AssignedProvider is empty, want selected provider for execution step")
	}
	if prepared.AssignedModel == "" {
		t.Fatal("AssignedModel is empty, want selected model for execution step")
	}
}

func TestPlannerPreparePublishesAIKernelSupportLanes(t *testing.T) {
	now := time.Now().UTC()
	registry := NewProviderModelRegistry(nil)
	registry.snapshots["ai_kernel"] = domain.ProviderCatalogSnapshot{
		Provider:           "ai_kernel",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "ai_kernel", ModelName: modelQwenCoder, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "qwen"}}},
	}
	registry.snapshots["mistral"] = domain.ProviderCatalogSnapshot{
		Provider:           "mistral",
		Status:             "ready",
		Available:          true,
		ObservedAt:         now,
		RefreshIntervalSec: 300,
		Models:             []domain.ProviderModelStatus{{Provider: "mistral", ModelName: modelMistral, Available: true, Status: "ready", VerificationStatus: "confirmed", TransportStatus: "transport_verified", IsDefault: true, ObservedAt: now, Metadata: map[string]any{"model_family": "mistral"}}},
	}
	planner := NewPlanner(NewModelSelector(registry))

	prepared, plan := planner.Prepare(domain.Task{
		ID:    "task-review-support",
		Type:  domain.TaskTypeReview,
		Input: domain.TaskInput{Description: "Review architecture and identify risks"},
	})

	if prepared.AssignedProvider != "mistral" {
		t.Fatalf("AssignedProvider=%q want mistral", prepared.AssignedProvider)
	}
	if helper, _ := prepared.RoutingHints["ai_kernel_helper"].(bool); !helper {
		t.Fatalf("expected ai_kernel_helper=true, got %#v", prepared.RoutingHints)
	}
	lanes, ok := prepared.RoutingHints["support_lanes"].([]domain.SupportLane)
	if !ok || len(lanes) == 0 {
		t.Fatalf("support_lanes=%#v want populated []domain.SupportLane", prepared.RoutingHints["support_lanes"])
	}
	if len(plan.Selection.SupportLanes) == 0 {
		t.Fatalf("plan selection support lanes empty: %#v", plan.Selection)
	}
}
