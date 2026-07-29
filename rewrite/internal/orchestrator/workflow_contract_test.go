package orchestrator_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestPlanRejectsCyclesAndUnknownDependenciesBeforeExecution(t *testing.T) {
	for _, plan := range []orchestrator.Plan{
		{Steps: []orchestrator.Step{{ID: "a", DependsOn: []string{"missing"}}}},
		{Steps: []orchestrator.Step{{ID: "a", DependsOn: []string{"b"}}, {ID: "b", DependsOn: []string{"a"}}}},
	} {
		if err := plan.Validate(); err == nil {
			t.Fatalf("Validate() accepted invalid plan: %#v", plan)
		}
	}
}

func TestSchedulerRunsIndependentStepsInParallelButNeverConflictingSteps(t *testing.T) {
	var mu sync.Mutex
	active := map[string]int{}
	maxActive := map[string]int{}
	started := make(chan string, 3)
	release := map[string]chan struct{}{"a": make(chan struct{}), "b": make(chan struct{}), "c": make(chan struct{})}
	executor := orchestrator.ExecutorFunc(func(ctx context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		mu.Lock()
		for _, key := range step.ConflictKeys {
			active[key]++
			if active[key] > maxActive[key] {
				maxActive[key] = active[key]
			}
		}
		mu.Unlock()
		started <- step.ID
		select {
		case <-release[step.ID]:
		case <-ctx.Done():
			return orchestrator.StepResult{}, ctx.Err()
		}
		mu.Lock()
		for _, key := range step.ConflictKeys {
			active[key]--
		}
		mu.Unlock()
		return orchestrator.StepResult{StepID: step.ID}, nil
	})
	plan := orchestrator.Plan{Steps: []orchestrator.Step{{ID: "a", ConflictKeys: []string{"repo/a.go"}}, {ID: "b", ConflictKeys: []string{"repo/a.go"}}, {ID: "c", ConflictKeys: []string{"repo/c.go"}}}}
	done := make(chan error, 1)
	go func() {
		scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 3})
		if err == nil {
			_, err = scheduler.Run(context.Background(), plan)
		}
		done <- err
	}()
	first := receiveStep(t, started)
	second := receiveStep(t, started)
	conflicting := first
	if first == "c" {
		conflicting = second
	}
	if first == second || (conflicting != "a" && conflicting != "b") || (first != "c" && second != "c") {
		t.Fatalf("initial starts = %q, %q; want c and exactly one of a/b", first, second)
	}
	select {
	case third := <-started:
		t.Fatalf("conflicting step %q ran before a completed", third)
	case <-time.After(30 * time.Millisecond):
	}
	close(release[conflicting])
	waiting := "a"
	if conflicting == "a" {
		waiting = "b"
	}
	if got := receiveStep(t, started); got != waiting {
		t.Fatalf("next start = %q, want %s", got, waiting)
	}
	close(release[waiting])
	close(release["c"])
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if maxActive["repo/a.go"] != 1 {
		t.Fatalf("conflicting max activity = %d, want 1", maxActive["repo/a.go"])
	}
	if maxActive["repo/c.go"] != 1 {
		t.Fatalf("c max activity = %d, want 1", maxActive["repo/c.go"])
	}
}

func receiveStep(t *testing.T, started <-chan string) string {
	t.Helper()
	select {
	case id := <-started:
		return id
	case <-time.After(time.Second):
		t.Fatal("scheduler did not start expected step")
		return ""
	}
}

func TestSchedulerDoesNotStartDependentStepUntilAllPrerequisitesSucceed(t *testing.T) {
	executor := orchestrator.NewRecordingExecutor(map[string]error{"compile": errors.New("compile failed")})
	plan := orchestrator.Plan{Steps: []orchestrator.Step{{ID: "compile"}, {ID: "test", DependsOn: []string{"compile"}}}}
	scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 2})
	if err != nil {
		t.Fatal(err)
	}
	run, err := scheduler.Run(context.Background(), plan)
	if err == nil {
		t.Fatal("Run() succeeded after prerequisite failure")
	}
	if executor.WasStarted("test") {
		t.Fatal("dependent step started despite failed prerequisite")
	}
	if run.Status != orchestrator.WorkflowFailed {
		t.Fatalf("status = %s", run.Status)
	}
}

func TestFailedPlanIsResumableWithoutRepeatingCompletedSteps(t *testing.T) {
	executor := orchestrator.NewRecordingExecutor(map[string]error{"second": errors.New("temporary")})
	scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	plan := orchestrator.Plan{ID: "plan-1", Steps: []orchestrator.Step{{ID: "first"}, {ID: "second", DependsOn: []string{"first"}}}}
	if _, err := scheduler.Run(context.Background(), plan); err == nil {
		t.Fatal("first run unexpectedly succeeded")
	}
	executor.ClearFailure("second")
	if _, err := scheduler.Resume(context.Background(), "plan-1"); err != nil {
		t.Fatal(err)
	}
	if executor.StartCount("first") != 1 {
		t.Fatalf("completed step ran %d times", executor.StartCount("first"))
	}
}
