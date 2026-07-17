package kernel

import (
	"reflect"
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestReadyPlanArtifactsReturnsOnlyDependencySatisfiedBatch(t *testing.T) {
	tasks := []domain.PlanTaskArtifact{
		{TaskID: "analyze"},
		{TaskID: "code-api", Dependencies: []string{"analyze"}},
		{TaskID: "code-ui", Dependencies: []string{"analyze"}},
		{TaskID: "test", Dependencies: []string{"code-api", "code-ui"}},
	}

	ready := readyPlanArtifacts(tasks, []string{"analyze", "code-api", "code-ui", "test"}, nil)
	if ids := planTaskIDs(ready); !reflect.DeepEqual(ids, []string{"analyze"}) {
		t.Fatalf("first batch = %v, want [analyze]", ids)
	}

	ready = readyPlanArtifacts(tasks, []string{"code-api", "code-ui", "test"}, []string{"analyze"})
	if ids := planTaskIDs(ready); !reflect.DeepEqual(ids, []string{"code-api", "code-ui"}) {
		t.Fatalf("second batch = %v, want [code-api code-ui]", ids)
	}

	ready = readyPlanArtifacts(tasks, []string{"test"}, []string{"analyze", "code-api", "code-ui"})
	if ids := planTaskIDs(ready); !reflect.DeepEqual(ids, []string{"test"}) {
		t.Fatalf("final batch = %v, want [test]", ids)
	}
}

func TestParallelGroupsReturnsSiblingBranchesOnly(t *testing.T) {
	steps := []domain.PlanStep{
		{ID: "analyze"},
		{ID: "code-api", Dependencies: []string{"analyze"}},
		{ID: "code-ui", Dependencies: []string{"analyze"}},
		{ID: "docs", Dependencies: []string{"analyze"}},
		{ID: "test", Dependencies: []string{"code-api", "code-ui"}},
	}

	groups := parallelGroups(steps)
	want := [][]string{{"code-api", "code-ui", "docs"}}
	if !reflect.DeepEqual(groups, want) {
		t.Fatalf("parallelGroups() = %v, want %v", groups, want)
	}
}

func planTaskIDs(tasks []domain.PlanTaskArtifact) []string {
	ids := make([]string, 0, len(tasks))
	for _, task := range tasks {
		ids = append(ids, task.TaskID)
	}
	return ids
}

func TestValidatePlanWorkflowRejectsNonSuccessfulAcceptanceStatus(t *testing.T) {
	record := domain.WorkflowRecord{
		Task: domain.Task{ID: "plan-code-api"},
		Acceptance: domain.TaskAcceptance{
			Status: domain.TaskStatusRejected,
			Reason: "routing policy denied execution",
		},
	}

	err := validatePlanWorkflow(record)
	if err == nil {
		t.Fatal("expected validation error for rejected workflow")
	}
	if got := err.Error(); got != "plan task plan-code-api finished with status rejected: routing policy denied execution" {
		t.Fatalf("unexpected error = %q", got)
	}
}

func TestValidatePlanWorkflowRejectsNonSuccessfulResultStatus(t *testing.T) {
	record := domain.WorkflowRecord{
		Task:       domain.Task{ID: "plan-test"},
		Acceptance: domain.TaskAcceptance{Status: domain.TaskStatusCompleted},
		Result: &domain.AgentResult{
			Status: domain.TaskStatusFailed,
			Output: domain.ResultOutput{Summary: "unit test execution failed"},
		},
	}

	err := validatePlanWorkflow(record)
	if err == nil {
		t.Fatal("expected validation error for failed agent result")
	}
	if got := err.Error(); got != "plan task plan-test produced result status failed: unit test execution failed" {
		t.Fatalf("unexpected error = %q", got)
	}
}

func TestValidatePlanWorkflowAcceptsSuccessfulTerminalStatuses(t *testing.T) {
	statuses := []domain.TaskStatus{domain.TaskStatusCompleted, domain.TaskStatusDone}
	for _, status := range statuses {
		t.Run(string(status), func(t *testing.T) {
			record := domain.WorkflowRecord{
				Task:       domain.Task{ID: "plan-review"},
				Acceptance: domain.TaskAcceptance{Status: status},
				Result: &domain.AgentResult{
					Status: status,
					Output: domain.ResultOutput{Summary: "ok"},
				},
			}

			if err := validatePlanWorkflow(record); err != nil {
				t.Fatalf("validatePlanWorkflow() error = %v", err)
			}
		})
	}
}

func TestBuildPlanArtifactClearsProviderPinForAnalyzeStep(t *testing.T) {
	task := domain.Task{
		ID:               "task-plan-provider-regression",
		SessionID:        "session-plan-provider-regression",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "mistral",
		AssignedModel:    "mistral-medium-2604",
	}
	plan := domain.ExecutionPlan{
		Selection: domain.ModelSelection{Provider: "mistral", ModelName: "mistral-medium-2604"},
		Steps: []domain.PlanStep{
			{ID: "task-plan-provider-regression-analyze", Title: "Analyze", Capability: "plan"},
			{ID: "task-plan-provider-regression-write", Title: "Write", Capability: "code", Dependencies: []string{"task-plan-provider-regression-analyze"}},
		},
	}

	artifact := buildPlanArtifact(task, plan)
	if got := artifact.Tasks[0].Provider; got != "" {
		t.Fatalf("analyze provider = %q, want empty", got)
	}
	if got := artifact.Tasks[0].ModelName; got != "" {
		t.Fatalf("analyze model = %q, want empty", got)
	}
	if got := artifact.Tasks[0].ExecutionContract["selected_provider"]; got != "" {
		t.Fatalf("analyze selected_provider = %v, want empty", got)
	}
	if got := artifact.Tasks[0].ExecutionContract["selected_model"]; got != "" {
		t.Fatalf("analyze selected_model = %v, want empty", got)
	}
}

func TestBuildPlanArtifactKeepsProviderPinForExecutionStep(t *testing.T) {
	task := domain.Task{
		ID:               "task-plan-provider-execution",
		SessionID:        "session-plan-provider-execution",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "mistral",
		AssignedModel:    "mistral-medium-2604",
	}
	plan := domain.ExecutionPlan{
		Selection: domain.ModelSelection{Provider: "mistral", ModelName: "mistral-medium-2604"},
		Steps: []domain.PlanStep{
			{ID: "task-plan-provider-execution-analyze", Title: "Analyze", Capability: "plan"},
			{ID: "task-plan-provider-execution-write", Title: "Write", Capability: "code", Dependencies: []string{"task-plan-provider-execution-analyze"}},
		},
	}

	artifact := buildPlanArtifact(task, plan)
	if got := artifact.Tasks[1].Provider; got != "mistral" {
		t.Fatalf("execution provider = %q, want mistral", got)
	}
	if got := artifact.Tasks[1].ModelName; got != "mistral-medium-2604" {
		t.Fatalf("execution model = %q, want mistral-medium-2604", got)
	}
	if got := artifact.Tasks[1].ExecutionContract["selected_provider"]; got != "mistral" {
		t.Fatalf("execution selected_provider = %v, want mistral", got)
	}
	if got := artifact.Tasks[1].ExecutionContract["selected_model"]; got != "mistral-medium-2604" {
		t.Fatalf("execution selected_model = %v, want mistral-medium-2604", got)
	}
}

func TestScheduleReadyArtifactsSkipsConflictingFilesInSameBatch(t *testing.T) {
	ready := []domain.PlanTaskArtifact{
		{TaskID: "code-a", Files: []string{"shared.go"}, ConflictKeys: []string{"shared.go"}, Weight: 1},
		{TaskID: "code-b", Files: []string{"shared.go"}, ConflictKeys: []string{"shared.go"}, Weight: 2},
		{TaskID: "code-c", Files: []string{"other.go"}, ConflictKeys: []string{"other.go"}, Weight: 1.5},
	}

	scheduled := scheduleReadyArtifacts(ready)
	if ids := planTaskIDs(scheduled); !reflect.DeepEqual(ids, []string{"code-a", "code-c"}) {
		t.Fatalf("scheduled batch = %v, want [code-a code-c]", ids)
	}
}

func TestScheduleReadyArtifactsSkipsConflictsWithRunningTasks(t *testing.T) {
	ready := []domain.PlanTaskArtifact{
		{TaskID: "code-a", ConflictKeys: []string{"shared.go"}, Weight: 1},
		{TaskID: "code-b", ConflictKeys: []string{"independent.go"}, Weight: 2},
	}

	scheduled := scheduleReadyArtifactsWithConflicts(ready, map[string]struct{}{"shared.go": {}})
	if ids := planTaskIDs(scheduled); !reflect.DeepEqual(ids, []string{"code-b"}) {
		t.Fatalf("scheduled batch = %v, want [code-b]", ids)
	}
}

func TestBuildPlanArtifactPropagatesClusterMetadata(t *testing.T) {
	task := domain.Task{
		ID:        "task-cluster-meta",
		SessionID: "session-cluster-meta",
		Type:      domain.TaskTypeCode,
	}
	plan := domain.ExecutionPlan{
		Complexity: domain.ComplexityHigh,
		Steps: []domain.PlanStep{
			{
				ID:            "task-cluster-meta-branch-1",
				Title:         "Implement branch",
				Capability:    "code",
				WorkerClass:   "code",
				ClusterID:     "task-cluster-meta-cluster-1",
				ContextBudget: 12,
				ConflictKeys:  []string{"a.go", "b.go"},
				Files:         []string{"a.go", "b.go"},
			},
		},
	}

	artifact := buildPlanArtifact(task, plan)
	if len(artifact.Tasks) != 1 {
		t.Fatalf("artifact task count = %d, want 1", len(artifact.Tasks))
	}
	got := artifact.Tasks[0]
	if got.WorkerClass != "code" {
		t.Fatalf("worker class = %q, want code", got.WorkerClass)
	}
	if got.ClusterID != "task-cluster-meta-cluster-1" {
		t.Fatalf("cluster id = %q, want task-cluster-meta-cluster-1", got.ClusterID)
	}
	if got.BranchID != "task-cluster-meta-cluster-1" {
		t.Fatalf("branch id = %q, want cluster id", got.BranchID)
	}
	if got.ContextBudget != 12 {
		t.Fatalf("context budget = %d, want 12", got.ContextBudget)
	}
	if !reflect.DeepEqual(got.ConflictKeys, []string{"a.go", "b.go"}) {
		t.Fatalf("conflict keys = %v, want [a.go b.go]", got.ConflictKeys)
	}
	if contractCluster := got.ExecutionContract["cluster_id"]; contractCluster != "task-cluster-meta-cluster-1" {
		t.Fatalf("execution contract cluster_id = %v, want task-cluster-meta-cluster-1", contractCluster)
	}
}

func TestBuildPlanTaskInjectsClusterExecutionContext(t *testing.T) {
	root := domain.Task{
		ID:        "root-task",
		SessionID: "session-1",
		Type:      domain.TaskTypeCode,
	}
	artifact := domain.PlanTaskArtifact{
		TaskID:        "root-task-branch-1",
		Title:         "Implement branch",
		Capability:    "code",
		WorkerClass:   "code",
		ClusterID:     "root-task-cluster-1",
		ContextBudget: 14,
		ConflictKeys:  []string{"pkg/a.go"},
		Files:         []string{"pkg/a.go"},
		BranchID:      "root-task-cluster-1",
		Weight:        2.75,
	}

	planned := buildPlanTask(root, artifact, 2)
	if planned.RoutingHints == nil {
		t.Fatal("routing hints should be initialized")
	}
	if planned.ExecutionContract == nil {
		t.Fatal("execution contract should be initialized")
	}
	if got := planned.RoutingHints["plan_step_id"]; got != "root-task-branch-1" {
		t.Fatalf("routing plan_step_id = %v, want root-task-branch-1", got)
	}
	if got := planned.ExecutionContract["worker_class"]; got != "code" {
		t.Fatalf("execution worker_class = %v, want code", got)
	}
	if got := planned.ExecutionContract["cluster_id"]; got != "root-task-cluster-1" {
		t.Fatalf("execution cluster_id = %v, want root-task-cluster-1", got)
	}
	if got := planned.ExecutionContract["context_budget"]; got != 14 {
		t.Fatalf("execution context_budget = %v, want 14", got)
	}
	if got := planned.ExecutionContract["task_weight"]; got != 2.75 {
		t.Fatalf("execution task_weight = %v, want 2.75", got)
	}
	if got := planned.RoutingHints["worker_class"]; got != "code" {
		t.Fatalf("routing worker_class = %v, want code", got)
	}
	if got := planned.RoutingHints["cluster_id"]; got != "root-task-cluster-1" {
		t.Fatalf("routing cluster_id = %v, want root-task-cluster-1", got)
	}
	if got := planned.RoutingHints["context_budget"]; got != 14 {
		t.Fatalf("routing context_budget = %v, want 14", got)
	}
	if got := planned.RoutingHints["task_weight"]; got != 2.75 {
		t.Fatalf("routing task_weight = %v, want 2.75", got)
	}
}

func TestClusterPlanFilesRespectsClusterSizingAndLimit(t *testing.T) {
	files := []string{"a.go", "b.go", "c.go", "d.go", "e.go", "f.go", "g.go"}
	clusters := clusterPlanFiles(files, 2)
	want := [][]string{{"a.go", "b.go"}, {"c.go", "d.go"}, {"e.go", "f.go"}, {"g.go"}}
	if !reflect.DeepEqual(clusters, want) {
		t.Fatalf("clusterPlanFiles() = %v, want %v", clusters, want)
	}
}
