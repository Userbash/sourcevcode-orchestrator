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
