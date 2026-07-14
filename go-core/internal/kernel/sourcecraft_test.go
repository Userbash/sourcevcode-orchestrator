package kernel

import (
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestPlannerPrepareAddsSourcecraftHintsAndPlanStages(t *testing.T) {
	planner := NewPlanner(NewModelSelector(nil))
	task := domain.Task{
		ID:   "sourcecraft-plan",
		Type: domain.TaskTypePlan,
		Input: domain.TaskInput{
			Description: "Prepare a PR workflow for release branch governance",
			Files:       []string{"README.md"},
		},
	}

	prepared, plan := planner.Prepare(task)

	if got := prepared.RoutingHints["sourcecraft_work"]; got != true {
		t.Fatalf("sourcecraft_work = %v, want true", got)
	}
	if got := prepared.RoutingHints["sourcecraft_task_family"]; got != "pr_flow" {
		t.Fatalf("sourcecraft_task_family = %v, want pr_flow", got)
	}
	actions, ok := prepared.RoutingHints["sourcecraft_recommended_actions"].([]string)
	if !ok {
		t.Fatalf("sourcecraft_recommended_actions type = %T", prepared.RoutingHints["sourcecraft_recommended_actions"])
	}
	if len(actions) == 0 || actions[0] != "repo_summary" {
		t.Fatalf("sourcecraft_recommended_actions = %#v, want repo_summary-led safe actions", actions)
	}
	if got := prepared.RoutingHints["sourcecraft_runtime_mode"]; got != "planning_only" {
		t.Fatalf("sourcecraft_runtime_mode = %v, want planning_only", got)
	}
	if got := prepared.RoutingHints["sourcecraft_mutation_supported"]; got != false {
		t.Fatalf("sourcecraft_mutation_supported = %v, want false", got)
	}
	if len(plan.Steps) != 5 {
		t.Fatalf("len(plan.Steps) = %d, want 5", len(plan.Steps))
	}
	if plan.Steps[1].Title != "inspect repository workflow context" {
		t.Fatalf("plan.Steps[1].Title = %q", plan.Steps[1].Title)
	}
	if plan.Steps[3].Title != "draft pr_flow delegation plan" {
		t.Fatalf("plan.Steps[3].Title = %q", plan.Steps[3].Title)
	}
	if plan.Steps[4].Capability != "review" {
		t.Fatalf("plan.Steps[4].Capability = %q, want review", plan.Steps[4].Capability)
	}
}
