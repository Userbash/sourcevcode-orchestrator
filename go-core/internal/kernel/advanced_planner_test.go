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
