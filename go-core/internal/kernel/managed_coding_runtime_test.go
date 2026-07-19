package kernel

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestNewWithStoreAttachesManagedCodingRuntimeByDefault(t *testing.T) {
	t.Setenv("GO_CORE_CODING_RUNTIME_ENABLED", "true")
	t.Setenv("GO_CORE_CODING_RUNTIME_BACKEND", "managed")
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("GO_CORE_SUBMIT_MODE", "sync")

	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	orchestrator := NewWithStore(store)
	if orchestrator.codingRuntime == nil {
		t.Fatalf("expected managed coding runtime to be attached")
	}
	snapshot := orchestrator.StateSnapshot(context.Background())
	runtimeState, ok := snapshot["coding_runtime"].(map[string]any)
	if !ok {
		t.Fatalf("expected coding runtime snapshot map, got %#v", snapshot["coding_runtime"])
	}
	if runtimeState["attached"] != true {
		t.Fatalf("expected coding runtime to be attached, got %#v", runtimeState)
	}
	if runtimeState["backend"] != "managed" {
		t.Fatalf("expected managed backend, got %#v", runtimeState["backend"])
	}
}

func TestManagedCodingRuntimeDelegatesAndCompletesTask(t *testing.T) {
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	t.Setenv("GO_CORE_CODING_RUNTIME_ENABLED", "false")

	orchestrator, _, _ := newBudgetTestOrchestrator(t)
	runtime := newManagedCodingRuntime(orchestrator, managedCodingRuntimeConfig{Name: "managed-realtime", Backend: "managed"})
	orchestrator.AttachExternalCodingRuntime(runtime)
	agent := &budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-test",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "unit-model",
		Capabilities: []string{"code", "fix", "test"},
		Status:       domain.AgentStatusReady,
	}}
	orchestrator.registry.RegisterAgent(agent)

	record, err := orchestrator.SubmitTask(context.Background(), domain.Task{
		ID:               "runtime-code-task",
		Type:             domain.TaskTypeCode,
		Priority:         domain.PriorityHigh,
		AssignedProvider: "local",
		AssignedModel:    "unit-model",
		Input: domain.TaskInput{
			Description: "Implement realtime coding execution",
			Files:       []string{"internal/kernel/runtime.go"},
		},
		Context:   domain.TaskContext{Project: "go-core", RepoPath: "/tmp/go-core"},
		CreatedAt: time.Now().UTC(),
	})
	if err != nil {
		t.Fatalf("submit task: %v", err)
	}
	if record.Result == nil {
		t.Fatalf("expected workflow result, got %#v", record)
	}
	if record.Result.Status != domain.TaskStatusCompleted {
		t.Fatalf("expected completed result, got %#v", record.Result)
	}
	if len(agent.ExecutedTasks()) == 0 {
		t.Fatalf("expected managed runtime to execute registered coding agent")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	record, err = orchestrator.WaitWorkflowTerminal(ctx, "runtime-code-task")
	if err != nil {
		t.Fatalf("wait workflow: %v", err)
	}
	if got := record.Task.ExecutionContract["coding_runtime"]; got != "managed-realtime" {
		t.Fatalf("expected coding runtime contract marker, got %#v", got)
	}
	snapshot := orchestrator.StateSnapshot(context.Background())
	runtimeState := snapshot["coding_runtime"].(map[string]any)
	if runtimeState["session_count"] == nil {
		t.Fatalf("expected session count in runtime snapshot: %#v", runtimeState)
	}
	sessions, ok := runtimeState["sessions"].([]map[string]any)
	if !ok || len(sessions) == 0 {
		t.Fatalf("expected runtime sessions in snapshot: %#v", runtimeState["sessions"])
	}
	live, ok := sessions[0]["live"].(map[string]any)
	if !ok {
		t.Fatalf("expected live session snapshot map, got %#v", sessions[0]["live"])
	}
	if _, ok := live["capabilities"].(domain.ModelCapabilities); !ok {
		t.Fatalf("expected typed capabilities in live session snapshot: %#v", live["capabilities"])
	}
	events := orchestrator.RuntimeEventSnapshot("runtime_session:managed-realtime:runtime-code-task")
	if len(events) == 0 {
		t.Fatalf("expected runtime session events to be published")
	}
	foundCompleted := false
	for _, event := range events {
		if event.Kind == "task.completed" {
			foundCompleted = true
			break
		}
	}
	if !foundCompleted {
		t.Fatalf("expected completed event in runtime session stream, got %#v", events)
	}
}

func TestManagedCodingRuntimeExecutesPlanLanes(t *testing.T) {
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	t.Setenv("GO_CORE_CODING_RUNTIME_ENABLED", "false")

	orchestrator, _, _ := newBudgetTestOrchestrator(t)
	runtime := newManagedCodingRuntime(orchestrator, managedCodingRuntimeConfig{Name: "managed-realtime", Backend: "managed"})
	orchestrator.AttachExternalCodingRuntime(runtime)

	orchestrator.registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID: "planner-1", Type: "planner", Provider: "local", ModelName: "unit-model",
		Capabilities: []string{"plan"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Status: domain.TaskStatusDone, Output: domain.ResultOutput{Summary: "plan ready"}}})
	orchestrator.registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-1", Type: "coding", Provider: "local", ModelName: "unit-model",
		Capabilities: []string{"code"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Status: domain.TaskStatusDone, Output: domain.ResultOutput{Summary: "code ready"}}})
	orchestrator.registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID: "reviewer-1", Type: "review", Provider: "local", ModelName: "unit-model",
		Capabilities: []string{"review"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Status: domain.TaskStatusDone, Output: domain.ResultOutput{Summary: "review ready"}}})
	orchestrator.registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID: "tester-1", Type: "test", Provider: "local", ModelName: "unit-model",
		Capabilities: []string{"test"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Status: domain.TaskStatusDone, Output: domain.ResultOutput{Summary: "test ready"}}})

	session, err := runtime.StartTask(context.Background(), domain.CodingRuntimeRequest{
		Task: domain.Task{
			ID:        "runtime-plan-task",
			Type:      domain.TaskTypeCode,
			Priority:  domain.PriorityHigh,
			Input:     domain.TaskInput{Description: "implement workflow"},
			Context:   domain.TaskContext{Project: "go-core", RepoPath: "/tmp/go-core"},
			CreatedAt: time.Now().UTC(),
		},
		Plan: domain.ExecutionPlan{
			TaskID:     "runtime-plan-task",
			Complexity: domain.ComplexityMedium,
			Steps: []domain.PlanStep{
				{ID: "plan", Title: "Plan the work", Capability: "plan", WorkerClass: "planner"},
				{ID: "code", Title: "Implement", Capability: "code", WorkerClass: "code", Dependencies: []string{"plan"}},
				{ID: "review", Title: "Review changes", Capability: "review", WorkerClass: "review", Dependencies: []string{"code"}},
				{ID: "test", Title: "Run tests", Capability: "test", WorkerClass: "test", Dependencies: []string{"code"}},
			},
		},
		Mode: domain.CodingRuntimeModeBuild,
	})
	if err != nil {
		t.Fatalf("start task: %v", err)
	}
	result, err := runtime.WaitTask(context.Background(), session)
	if err != nil {
		t.Fatalf("wait task: %v", err)
	}
	if result.Status != domain.TaskStatusCompleted {
		t.Fatalf("unexpected status: %s errors=%v", result.Status, result.Errors)
	}
	for _, want := range []string{"plan ready", "code ready", "review ready", "test ready"} {
		if !strings.Contains(result.Output.Summary, want) {
			t.Fatalf("expected summary %q in %q", want, result.Output.Summary)
		}
	}
	events := orchestrator.RuntimeEventSnapshot("runtime_session:managed-realtime:runtime-plan-task")
	seenStarted := false
	seenCompleted := false
	for _, event := range events {
		if event.Kind == "lane.started" {
			seenStarted = true
		}
		if event.Kind == "lane.completed" {
			seenCompleted = true
		}
	}
	if !seenStarted || !seenCompleted {
		t.Fatalf("expected lane lifecycle events, got %#v", events)
	}
}
