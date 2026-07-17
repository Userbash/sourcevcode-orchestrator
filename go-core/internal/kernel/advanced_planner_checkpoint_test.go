package kernel

import (
	"context"
	"reflect"
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestPreviewExecutionPlanPersistsSplitParallelCheckpoint(t *testing.T) {
	orchestrator, store, _ := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	preview, err := orchestrator.PreviewExecutionPlan(ctx, domain.Task{
		ID:         "task-checkpoint-split",
		SessionID:  "session-checkpoint-split",
		Type:       domain.TaskTypeCode,
		Complexity: domain.ComplexityCritical,
		Input: domain.TaskInput{
			Description: "Build a wide code plan so checkpoint persistence has meaningful payload to store.",
			Files: []string{
				"internal/api/http.go",
				"internal/kernel/orchestrator.go",
				"internal/delivery/worker_pool.go",
			},
			AcceptanceCriteria: []string{
				"checkpoint keeps execution metadata",
				"runtime checkpoint stays lightweight",
			},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	})
	if err != nil {
		t.Fatalf("PreviewExecutionPlan() error = %v", err)
	}

	runtimeState, ok, err := store.GetSessionState(ctx, preview.Task.SessionID, checkpointBranchName(preview.Task.ID))
	if err != nil {
		t.Fatalf("GetSessionState(runtime) error = %v", err)
	}
	if !ok {
		t.Fatalf("runtime checkpoint missing for %s", preview.Task.ID)
	}
	if _, exists := runtimeState.State["root_task"]; exists {
		t.Fatalf("runtime checkpoint unexpectedly persisted root_task: %#v", runtimeState.State)
	}
	if _, exists := runtimeState.State["plan"]; exists {
		t.Fatalf("runtime checkpoint unexpectedly persisted plan: %#v", runtimeState.State)
	}
	if _, exists := runtimeState.State["plan_artifact"]; exists {
		t.Fatalf("runtime checkpoint unexpectedly persisted plan_artifact: %#v", runtimeState.State)
	}

	staticState, ok, err := store.GetSessionState(ctx, preview.Task.SessionID, checkpointStaticBranchName(preview.Task.ID))
	if err != nil {
		t.Fatalf("GetSessionState(static) error = %v", err)
	}
	if !ok {
		t.Fatalf("static checkpoint missing for %s", preview.Task.ID)
	}
	if _, exists := staticState.State["root_task"]; !exists {
		t.Fatalf("static checkpoint missing root_task: %#v", staticState.State)
	}
	if _, exists := staticState.State["plan"]; !exists {
		t.Fatalf("static checkpoint missing plan: %#v", staticState.State)
	}
	if _, exists := staticState.State["plan_artifact"]; !exists {
		t.Fatalf("static checkpoint missing plan_artifact: %#v", staticState.State)
	}

	loaded, ok, err := orchestrator.LoadParallelCheckpoint(ctx, preview.Task.SessionID, preview.Task.ID)
	if err != nil {
		t.Fatalf("LoadParallelCheckpoint() error = %v", err)
	}
	if !ok {
		t.Fatalf("LoadParallelCheckpoint() = not found, want checkpoint")
	}
	if loaded.RootTask.ID != preview.Task.ID {
		t.Fatalf("loaded.RootTask.ID = %s, want %s", loaded.RootTask.ID, preview.Task.ID)
	}
	if loaded.RootTask.SessionID != preview.Task.SessionID {
		t.Fatalf("loaded.RootTask.SessionID = %s, want %s", loaded.RootTask.SessionID, preview.Task.SessionID)
	}
	if loaded.Plan.TaskID != preview.Plan.TaskID {
		t.Fatalf("loaded.Plan.TaskID = %s, want %s", loaded.Plan.TaskID, preview.Plan.TaskID)
	}
	if len(loaded.Plan.Steps) != len(preview.Plan.Steps) {
		t.Fatalf("len(loaded.Plan.Steps) = %d, want %d", len(loaded.Plan.Steps), len(preview.Plan.Steps))
	}
	if loaded.PlanArtifact.RootTaskID != preview.PlanArtifact.RootTaskID {
		t.Fatalf("loaded.PlanArtifact.RootTaskID = %s, want %s", loaded.PlanArtifact.RootTaskID, preview.PlanArtifact.RootTaskID)
	}
	if len(loaded.PlanArtifact.Tasks) != len(preview.PlanArtifact.Tasks) {
		t.Fatalf("len(loaded.PlanArtifact.Tasks) = %d, want %d", len(loaded.PlanArtifact.Tasks), len(preview.PlanArtifact.Tasks))
	}
	if !reflect.DeepEqual(loaded.PendingTaskIDs, preview.PendingTaskIDs) {
		t.Fatalf("loaded.PendingTaskIDs = %v, want %v", loaded.PendingTaskIDs, preview.PendingTaskIDs)
	}
}
