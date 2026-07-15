package kernel

import (
	"context"
	"reflect"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type taskEventKind string

const (
	taskEventAccepted       taskEventKind = "task.accepted"
	taskEventQueued         taskEventKind = "task.queued"
	taskEventDequeued       taskEventKind = "task.dequeued"
	taskEventRunning        taskEventKind = "task.running"
	taskEventResultReceived taskEventKind = "task.result_received"
	taskEventCompleted      taskEventKind = "task.completed"
)

func TestPreviewExecutionPlanRealResearchTaskBuildsSequentialStages(t *testing.T) {
	planner := NewPlanner(NewModelSelector(nil))
	task, plan := planner.Prepare(domain.Task{
		ID:        "task-research-sequential",
		SessionID: "session-research-sequential",
		Type:      domain.TaskTypeResearch,
		Input: domain.TaskInput{
			Description:        "Investigate the current module behavior and write an implementation note.",
			Files:              []string{"notes/analysis.txt"},
			AcceptanceCriteria: []string{"document the findings"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	})

	artifact := buildPlanArtifact(task, plan)
	if got, want := len(artifact.Tasks), 4; got != want {
		t.Fatalf("len(artifact.Tasks) = %d, want %d", got, want)
	}
	if len(artifact.ParallelGroups) != 0 {
		t.Fatalf("artifact.ParallelGroups = %v, want no parallel groups", artifact.ParallelGroups)
	}

	gotIDs := make([]string, 0, len(artifact.Tasks))
	for _, item := range artifact.Tasks {
		gotIDs = append(gotIDs, item.TaskID)
	}
	wantIDs := []string{
		task.ID + "-analyze",
		task.ID + "-collect-context",
		task.ID + "-write-output",
		task.ID + "-review-output",
	}
	if !reflect.DeepEqual(gotIDs, wantIDs) {
		t.Fatalf("task ids = %v, want %v", gotIDs, wantIDs)
	}
}

func TestPreviewExecutionPlanRealCodeTaskBuildsParallelBranches(t *testing.T) {
	planner := NewPlanner(NewModelSelector(nil))
	task, plan := planner.Prepare(domain.Task{
		ID:         "task-code-parallel",
		SessionID:  "session-code-parallel",
		Type:       domain.TaskTypeCode,
		Complexity: domain.ComplexityCritical,
		Input: domain.TaskInput{
			Description: "Implement a multi-file refactor for API, UI and persistence code.",
			Files: []string{
				"internal/api/http.go",
				"internal/kernel/orchestrator.go",
				"internal/delivery/worker_pool.go",
			},
			AcceptanceCriteria: []string{
				"api path remains stable",
				"worker pool stays responsive",
			},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	})

	artifact := buildPlanArtifact(task, plan)
	if got, want := len(artifact.Tasks), 6; got != want {
		t.Fatalf("len(artifact.Tasks) = %d, want %d", got, want)
	}
	wantGroup := [][]string{{
		task.ID + "-branch-1",
		task.ID + "-branch-2",
		task.ID + "-branch-3",
	}}
	if !reflect.DeepEqual(artifact.ParallelGroups, wantGroup) {
		t.Fatalf("artifact.ParallelGroups = %v, want %v", artifact.ParallelGroups, wantGroup)
	}
	if ids := planTaskIDs(readyPlanArtifacts(artifact.Tasks, plannedPlanTaskIDs(artifact.Tasks), nil)); !reflect.DeepEqual(ids, []string{task.ID + "-analyze"}) {
		t.Fatalf("initial ready batch = %v, want [%s-analyze]", ids, task.ID)
	}
	if ids := planTaskIDs(readyPlanArtifacts(artifact.Tasks, plannedPlanTaskIDs(artifact.Tasks[1:]), []string{task.ID + "-analyze"})); !reflect.DeepEqual(ids, wantGroup[0]) {
		t.Fatalf("parallel ready batch = %v, want %v", ids, wantGroup[0])
	}
}

func TestSubmitTaskSyncReturnsTerminalWorkflowRecord(t *testing.T) {
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
			Summary:   "sync execution completed",
			Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 7}},
		},
	}})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	task := domain.Task{
		ID:               "task-sync-terminal",
		SessionID:        "session-sync-terminal",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		RoutingHints:     map[string]any{"preferred_agent_id": "coder-local"},
		Input: domain.TaskInput{
			Description:        "Execute a real sync task and persist a terminal workflow record.",
			Files:              []string{"internal/kernel/orchestrator.go"},
			AcceptanceCriteria: []string{"persist terminal workflow"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	}

	record, err := orchestrator.SubmitTask(ctx, task)
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("record.Acceptance.Status = %s, want %s", record.Acceptance.Status, domain.TaskStatusCompleted)
	}
	if record.Result == nil {
		t.Fatal("record.Result = nil")
	}

	terminal, err := orchestrator.WaitWorkflowTerminal(ctx, task.ID)
	if err != nil {
		t.Fatalf("WaitWorkflowTerminal() error = %v", err)
	}
	if terminal.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("terminal.Acceptance.Status = %s, want %s", terminal.Acceptance.Status, domain.TaskStatusCompleted)
	}

	persisted, ok, err := store.GetWorkflow(ctx, task.ID)
	if err != nil {
		t.Fatalf("GetWorkflow() error = %v", err)
	}
	if !ok {
		t.Fatalf("workflow %s not found", task.ID)
	}
	if persisted.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("persisted.Acceptance.Status = %s, want %s", persisted.Acceptance.Status, domain.TaskStatusCompleted)
	}
	kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), task.ID)
	assertOrderedTaskEvents(t, kinds, []taskEventKind{taskEventAccepted, taskEventRunning, taskEventCompleted})
}

func TestRunExecutionPlanRealTaskCollectsWorkflowEvidence(t *testing.T) {
	orchestrator, _, registry := newBudgetTestOrchestrator(t)
	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, result: domain.AgentResult{
		Output: domain.ResultOutput{
			Summary:   "completed test workload",
			Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 11}},
		},
	}})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	run, err := orchestrator.RunExecutionPlan(ctx, domain.Task{
		ID:               "task-run-plan",
		SessionID:        "session-run-plan",
		Type:             domain.TaskTypeCode,
		Complexity:       domain.ComplexityHigh,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		Input: domain.TaskInput{
			Description: "Refactor orchestrator scheduling and worker pool behavior across multiple files.",
			Files: []string{
				"internal/kernel/orchestrator.go",
				"internal/delivery/worker_pool.go",
				"internal/kernel/advanced_planner.go",
			},
			AcceptanceCriteria: []string{"scheduler still fair", "worker pool still healthy"},
		},
		Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
		RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
	})
	if err != nil {
		t.Fatalf("RunExecutionPlan() error = %v", err)
	}
	if run.Checkpoint.Status != domain.ParallelPlanStatusCompleted {
		t.Fatalf("Checkpoint.Status = %s, want %s", run.Checkpoint.Status, domain.ParallelPlanStatusCompleted)
	}
	if len(run.Workflows) != len(run.PlanArtifact.Tasks) {
		t.Fatalf("len(run.Workflows) = %d, want %d", len(run.Workflows), len(run.PlanArtifact.Tasks))
	}
	if len(run.Checkpoint.ResultsByTaskID) != len(run.PlanArtifact.Tasks) {
		t.Fatalf("len(ResultsByTaskID) = %d, want %d", len(run.Checkpoint.ResultsByTaskID), len(run.PlanArtifact.Tasks))
	}
	for _, workflow := range run.Workflows {
		if workflow.Acceptance.Status != domain.TaskStatusCompleted {
			t.Fatalf("workflow %s status = %s, want %s", workflow.Task.ID, workflow.Acceptance.Status, domain.TaskStatusCompleted)
		}
		if workflow.Result == nil {
			t.Fatalf("workflow %s result = nil", workflow.Task.ID)
		}
		persisted, ok, err := orchestrator.store.GetWorkflow(ctx, workflow.Task.ID)
		if err != nil {
			t.Fatalf("GetWorkflow(%s) error = %v", workflow.Task.ID, err)
		}
		if !ok {
			t.Fatalf("workflow %s not persisted", workflow.Task.ID)
		}
		if persisted.Acceptance.Status != domain.TaskStatusCompleted {
			t.Fatalf("persisted workflow %s status = %s, want %s", workflow.Task.ID, persisted.Acceptance.Status, domain.TaskStatusCompleted)
		}
		kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), workflow.Task.ID)
		assertOrderedTaskEvents(t, kinds, []taskEventKind{taskEventAccepted, taskEventRunning, taskEventCompleted})
	}
	for _, planned := range run.PlanArtifact.Tasks {
		if _, ok := run.Checkpoint.ResultsByTaskID[planned.TaskID]; !ok {
			t.Fatalf("missing checkpoint result for task %s", planned.TaskID)
		}
	}
}

func TestRealTaskExecutionScenariosCollectLifecycleEvidence(t *testing.T) {
	t.Setenv("GO_CORE_SUBMIT_MODE", "async")

	tests := []struct {
		name        string
		task        domain.Task
		wantSummary string
	}{
		{
			name: "code refactor",
			task: domain.Task{
				ID:               "task-real-code-trace",
				SessionID:        "session-real-code-trace",
				Type:             domain.TaskTypeCode,
				Priority:         domain.PriorityHigh,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Refactor agent scheduling and mailbox consumption for a multi-file code change.",
					Files:              []string{"internal/kernel/orchestrator.go", "internal/delivery/worker_pool.go"},
					AcceptanceCriteria: []string{"scheduler remains fair", "mailbox remains responsive"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			wantSummary: "completed code refactor",
		},
		{
			name: "research investigation",
			task: domain.Task{
				ID:               "task-real-research-trace",
				SessionID:        "session-real-research-trace",
				Type:             domain.TaskTypeResearch,
				Priority:         domain.PriorityNormal,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Investigate orchestrator runtime behavior and document bottlenecks.",
					Files:              []string{"docs/runtime.md"},
					AcceptanceCriteria: []string{"document bottlenecks", "capture execution notes"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			wantSummary: "completed research investigation",
		},
		{
			name: "docs update",
			task: domain.Task{
				ID:               "task-real-docs-trace",
				SessionID:        "session-real-docs-trace",
				Type:             domain.TaskTypeDocs,
				Priority:         domain.PriorityLow,
				AssignedProvider: "local",
				AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
				Input: domain.TaskInput{
					Description:        "Update delivery and worker documentation after async orchestration changes.",
					Files:              []string{"docs/p2p_delivery.md"},
					AcceptanceCriteria: []string{"document queue lifecycle", "explain result callbacks"},
				},
				Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
				RoutingHints: map[string]any{"preferred_agent_id": "coder-local"},
			},
			wantSummary: "completed docs update",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			orchestrator, store, registry := newBudgetTestOrchestrator(t)
			agent := &budgetTestAgent{info: domain.AgentInfo{
				ID:           "coder-local",
				Type:         "coding",
				Provider:     "local",
				ModelName:    "qwen2.5:32b-instruct-q4_k_m",
				Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
				Status:       domain.AgentStatusReady,
			}, result: domain.AgentResult{
				Status: domain.TaskStatusCompleted,
				Output: domain.ResultOutput{
					Summary: tt.wantSummary,
					Artifacts: map[string]any{
						"trace_id":   tt.task.ID,
						"task_type":  string(tt.task.Type),
						"file_count": len(tt.task.Input.Files),
					},
				},
			}}
			registry.RegisterAgent(agent)

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			orchestrator.startAgentWorkerPools(ctx, 1)

			record, err := orchestrator.SubmitTask(ctx, tt.task)
			if err != nil {
				t.Fatalf("SubmitTask() error = %v", err)
			}
			if record.Acceptance.Status != domain.TaskStatusQueued {
				t.Fatalf("initial Acceptance.Status = %s, want %s", record.Acceptance.Status, domain.TaskStatusQueued)
			}

			terminal, err := orchestrator.WaitWorkflowTerminal(ctx, tt.task.ID)
			if err != nil {
				t.Fatalf("WaitWorkflowTerminal() error = %v", err)
			}
			if terminal.Acceptance.Status != domain.TaskStatusCompleted {
				t.Fatalf("terminal status = %s, want %s", terminal.Acceptance.Status, domain.TaskStatusCompleted)
			}
			if terminal.Result == nil {
				t.Fatal("terminal result = nil")
			}
			if terminal.Result.Output.Summary != tt.wantSummary {
				t.Fatalf("terminal summary = %q, want %q", terminal.Result.Output.Summary, tt.wantSummary)
			}

			persisted, ok, err := store.GetWorkflow(ctx, tt.task.ID)
			if err != nil {
				t.Fatalf("GetWorkflow() error = %v", err)
			}
			if !ok {
				t.Fatalf("workflow %s not found", tt.task.ID)
			}
			if persisted.Result == nil {
				t.Fatal("persisted result = nil")
			}
			if persisted.Result.Output.Artifacts["trace_id"] != tt.task.ID {
				t.Fatalf("trace_id = %v, want %s", persisted.Result.Output.Artifacts["trace_id"], tt.task.ID)
			}

			kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), tt.task.ID)
			assertOrderedTaskEvents(t, kinds, []taskEventKind{
				taskEventQueued,
				taskEventDequeued,
				taskEventRunning,
				taskEventResultReceived,
				taskEventCompleted,
			})
			if len(agent.executedTasks) != 1 {
				t.Fatalf("agent executed %d tasks, want 1", len(agent.executedTasks))
			}
		})
	}
}

func TestAsyncTaskLifecycleTraceCapturesFailureRuntimeEvents(t *testing.T) {
	t.Setenv("GO_CORE_SUBMIT_MODE", "async")

	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	agent := &budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, result: domain.AgentResult{
		Status: domain.TaskStatusFailed,
		Errors: []string{"synthetic failure for trace test"},
		Output: domain.ResultOutput{
			Summary:   "async execution failed",
			Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 3}},
		},
	}}
	registry.RegisterAgent(agent)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	orchestrator.startAgentWorkerPools(ctx, 1)

	task := domain.Task{
		ID:               "task-async-failure-trace",
		SessionID:        "session-async-failure-trace",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		RoutingHints: map[string]any{
			"preferred_agent_id": "coder-local",
		},
		Input: domain.TaskInput{
			Description:        "Attempt async execution and capture a failed lifecycle.",
			Files:              []string{"internal/kernel/orchestrator.go"},
			AcceptanceCriteria: []string{"collect failure runtime events"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	}
	record, err := orchestrator.SubmitTask(ctx, task)
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Acceptance.Status != domain.TaskStatusQueued {
		t.Fatalf("initial Acceptance.Status = %s, want queued", record.Acceptance.Status)
	}

	terminal, err := orchestrator.WaitWorkflowTerminal(ctx, task.ID)
	if err != nil {
		t.Fatalf("WaitWorkflowTerminal() error = %v", err)
	}
	if terminal.Acceptance.Status != domain.TaskStatusFailed {
		t.Fatalf("terminal status = %s, want %s", terminal.Acceptance.Status, domain.TaskStatusFailed)
	}
	if terminal.Result == nil {
		t.Fatal("terminal result = nil")
	}
	if terminal.Result.Output.Summary != "async execution failed" {
		t.Fatalf("terminal summary = %q, want %q", terminal.Result.Output.Summary, "async execution failed")
	}

	persisted, ok, err := store.GetWorkflow(ctx, task.ID)
	if err != nil {
		t.Fatalf("GetWorkflow() error = %v", err)
	}
	if !ok {
		t.Fatalf("workflow %s not found", task.ID)
	}
	if persisted.Acceptance.Status != domain.TaskStatusFailed {
		t.Fatalf("persisted status = %s, want %s", persisted.Acceptance.Status, domain.TaskStatusFailed)
	}

	kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), task.ID)
	assertOrderedTaskEvents(t, kinds, []taskEventKind{
		taskEventQueued,
		taskEventDequeued,
		taskEventRunning,
		taskEventResultReceived,
	})
	if len(agent.executedTasks) != 1 {
		t.Fatalf("agent executed %d tasks, want 1", len(agent.executedTasks))
	}
}

func TestAsyncTaskLifecycleTraceCapturesOrderedRuntimeEvents(t *testing.T) {
	t.Setenv("GO_CORE_SUBMIT_MODE", "async")

	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	agent := &budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-local",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, result: domain.AgentResult{
		Status: domain.TaskStatusCompleted,
		Output: domain.ResultOutput{
			Summary:   "async execution completed",
			Artifacts: map[string]any{"usage": map[string]any{"total_tokens": 17}},
		},
	}}
	registry.RegisterAgent(agent)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	orchestrator.startAgentWorkerPools(ctx, 1)

	task := domain.Task{
		ID:               "task-async-trace",
		SessionID:        "session-async-trace",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		RoutingHints: map[string]any{
			"preferred_agent_id": "coder-local",
		},
		Input: domain.TaskInput{
			Description:        "Implement async task tracing across orchestrator and agent workers.",
			Files:              []string{"internal/kernel/orchestrator.go"},
			AcceptanceCriteria: []string{"collect runtime events"},
		},
		Context: domain.TaskContext{Branch: "main", Project: "go-core"},
	}
	record, err := orchestrator.SubmitTask(ctx, task)
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Acceptance.Status != domain.TaskStatusQueued {
		t.Fatalf("initial Acceptance.Status = %s, want queued", record.Acceptance.Status)
	}

	terminal, err := orchestrator.WaitWorkflowTerminal(ctx, task.ID)
	if err != nil {
		t.Fatalf("WaitWorkflowTerminal() error = %v", err)
	}
	if terminal.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("terminal status = %s, want %s (reason=%s)", terminal.Acceptance.Status, domain.TaskStatusCompleted, terminal.Acceptance.Reason)
	}

	persisted, ok, err := store.GetWorkflow(ctx, task.ID)
	if err != nil {
		t.Fatalf("GetWorkflow() error = %v", err)
	}
	if !ok {
		t.Fatalf("workflow %s not found", task.ID)
	}
	if persisted.Result == nil {
		t.Fatal("persisted workflow result = nil")
	}
	if persisted.Result.Output.Summary != "async execution completed" {
		t.Fatalf("persisted summary = %q, want %q", persisted.Result.Output.Summary, "async execution completed")
	}

	kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), task.ID)
	assertOrderedTaskEvents(t, kinds, []taskEventKind{
		taskEventQueued,
		taskEventDequeued,
		taskEventRunning,
		taskEventResultReceived,
		taskEventCompleted,
	})
	if len(agent.executedTasks) != 1 {
		t.Fatalf("agent executed %d tasks, want 1", len(agent.executedTasks))
	}
}

func plannedPlanTaskIDs(tasks []domain.PlanTaskArtifact) []string {
	ids := make([]string, 0, len(tasks))
	for _, task := range tasks {
		ids = append(ids, task.TaskID)
	}
	return ids
}

func eventKindsForEntity(events []domain.StreamEvent, entityID string) []taskEventKind {
	kinds := make([]taskEventKind, 0, len(events))
	for _, event := range events {
		if event.EntityID != entityID {
			continue
		}
		kinds = append(kinds, taskEventKind(event.Kind))
	}
	return kinds
}

func assertOrderedTaskEvents(t *testing.T, got []taskEventKind, want []taskEventKind) {
	t.Helper()
	index := 0
	for _, kind := range got {
		if index < len(want) && kind == want[index] {
			index++
		}
	}
	if index != len(want) {
		t.Fatalf("event sequence = %v, want subsequence %v", got, want)
	}
}

func TestAsyncSubmissionStartsWorkerPoolForLateRegisteredAgent(t *testing.T) {
	t.Setenv("GO_CORE_SUBMIT_MODE", "async")

	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	agent := &budgetTestAgent{info: domain.AgentInfo{
		ID:           "coder-late",
		Type:         "coding",
		Provider:     "local",
		ModelName:    "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "plan", "review", "test", "research", "docs"},
		Status:       domain.AgentStatusReady,
	}, result: domain.AgentResult{
		Status: domain.TaskStatusCompleted,
		Output: domain.ResultOutput{
			Summary:   "late registration completed",
			Artifacts: map[string]any{"trace_mode": "late_registration"},
		},
	}}
	registry.RegisterAgent(agent)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	task := domain.Task{
		ID:               "task-async-late-registration",
		SessionID:        "session-async-late-registration",
		Type:             domain.TaskTypeCode,
		Priority:         domain.PriorityHigh,
		AssignedProvider: "local",
		AssignedModel:    "qwen2.5:32b-instruct-q4_k_m",
		Input: domain.TaskInput{
			Description:        "Verify async execution after late agent registration without manual worker startup.",
			Files:              []string{"internal/kernel/orchestrator.go"},
			AcceptanceCriteria: []string{"auto start worker pool", "complete queued task"},
		},
		Context:      domain.TaskContext{Branch: "main", Project: "go-core"},
		RoutingHints: map[string]any{"preferred_agent_id": "coder-late"},
	}

	record, err := orchestrator.SubmitTask(ctx, task)
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Acceptance.Status != domain.TaskStatusQueued {
		t.Fatalf("initial Acceptance.Status = %s, want %s", record.Acceptance.Status, domain.TaskStatusQueued)
	}

	terminal, err := orchestrator.WaitWorkflowTerminal(ctx, task.ID)
	if err != nil {
		t.Fatalf("WaitWorkflowTerminal() error = %v", err)
	}
	if terminal.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("terminal status = %s, want %s", terminal.Acceptance.Status, domain.TaskStatusCompleted)
	}
	if terminal.Result == nil || terminal.Result.Output.Summary != "late registration completed" {
		t.Fatalf("unexpected terminal result = %#v", terminal.Result)
	}

	persisted, ok, err := store.GetWorkflow(ctx, task.ID)
	if err != nil {
		t.Fatalf("GetWorkflow() error = %v", err)
	}
	if !ok {
		t.Fatalf("workflow %s not found", task.ID)
	}
	if persisted.Acceptance.Status != domain.TaskStatusCompleted {
		t.Fatalf("persisted status = %s, want %s", persisted.Acceptance.Status, domain.TaskStatusCompleted)
	}

	kinds := eventKindsForEntity(orchestrator.RuntimeEventSnapshot("tasks"), task.ID)
	assertOrderedTaskEvents(t, kinds, []taskEventKind{
		taskEventQueued,
		taskEventDequeued,
		taskEventRunning,
		taskEventResultReceived,
		taskEventCompleted,
	})
	if len(agent.executedTasks) != 1 {
		t.Fatalf("agent executed %d tasks, want 1", len(agent.executedTasks))
	}
}
