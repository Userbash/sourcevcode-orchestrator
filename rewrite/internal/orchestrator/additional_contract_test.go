package orchestrator_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestSubmitNormalizesIdempotencyKeyAtServiceBoundary(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.Submit(context.Background(), orchestrator.SubmitRequest{
		IdempotencyKey: " retry-safe ", Description: "write a test",
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.Submit(context.Background(), orchestrator.SubmitRequest{
		IdempotencyKey: "retry-safe", Description: "write a test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !second.Replayed || first.WorkflowID != second.WorkflowID {
		t.Fatalf("first=%#v second=%#v; whitespace must not create a second workflow", first, second)
	}
}

func TestServicesSharingAStoreAllocateDistinctWorkflowIDs(t *testing.T) {
	store := orchestrator.NewMemoryStore()
	firstService, err := orchestrator.NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	first, err := firstService.Submit(context.Background(), orchestrator.SubmitRequest{
		IdempotencyKey: "one", Description: "first",
	})
	if err != nil {
		t.Fatal(err)
	}
	secondService, err := orchestrator.NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	second, err := secondService.Submit(context.Background(), orchestrator.SubmitRequest{
		IdempotencyKey: "two", Description: "second",
	})
	if err != nil {
		t.Fatal(err)
	}
	if first.WorkflowID == second.WorkflowID || store.Count() != 2 {
		t.Fatalf("first=%#v second=%#v count=%d", first, second, store.Count())
	}
}

func TestSubmitRejectsAnIdempotencyKeyContainingOnlyWhitespace(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}

	_, err = service.Submit(context.Background(), orchestrator.SubmitRequest{
		IdempotencyKey: " \t ", Description: "write a test",
	})
	if err == nil {
		t.Fatal("whitespace-only idempotency key accepted")
	}
}

func TestSchedulerNormalizesOverlappingConflictKeys(t *testing.T) {
	started := make(chan string, 2)
	release := make(chan struct{})
	executor := orchestrator.ExecutorFunc(func(ctx context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		started <- step.ID
		<-release
		return orchestrator.StepResult{StepID: step.ID}, nil
	})
	plan := orchestrator.Plan{Steps: []orchestrator.Step{
		{ID: "first", ConflictKeys: []string{" repo\\src/../main.go "}},
		{ID: "second", ConflictKeys: []string{"repo/main.go"}},
	}}
	done := make(chan error, 1)
	go func() {
		scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 2})
		if err == nil {
			_, err = scheduler.Run(context.Background(), plan)
		}
		done <- err
	}()

	<-started
	select {
	case unexpected := <-started:
		t.Fatalf("overlapping step %q started concurrently", unexpected)
	case <-time.After(30 * time.Millisecond):
	}
	release <- struct{}{}
	<-started
	release <- struct{}{}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestSchedulerCancelsAndWaitsForActiveWorkAfterFailure(t *testing.T) {
	started := make(chan string, 2)
	bothStarted := make(chan struct{})
	var once sync.Once
	cancelled := make(chan struct{})
	executor := orchestrator.ExecutorFunc(func(ctx context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		started <- step.ID
		if len(started) == 2 {
			once.Do(func() { close(bothStarted) })
		}
		<-bothStarted
		if step.ID == "fails" {
			return orchestrator.StepResult{}, errors.New("executor failed")
		}
		<-ctx.Done()
		close(cancelled)
		return orchestrator.StepResult{}, ctx.Err()
	})
	plan := orchestrator.Plan{Steps: []orchestrator.Step{{ID: "fails"}, {ID: "active"}}}
	done := make(chan error, 1)
	go func() {
		scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 2})
		if err == nil {
			_, err = scheduler.Run(context.Background(), plan)
		}
		done <- err
	}()

	select {
	case err := <-done:
		t.Fatalf("Run returned before active work observed cancellation: %v", err)
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("active work was not cancelled")
	}
	if err := <-done; err == nil {
		t.Fatal("Run succeeded after executor failure")
	}
}

func TestSchedulerReleasesConflictKeysWhenItCancelsParallelWork(t *testing.T) {
	started := make(chan string, 2)
	bothStarted := make(chan struct{})
	var once sync.Once
	cancelled := make(chan struct{})
	executor := orchestrator.ExecutorFunc(func(ctx context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		started <- step.ID
		if len(started) == 2 {
			once.Do(func() { close(bothStarted) })
		}
		<-bothStarted
		if step.ID == "fails" {
			return orchestrator.StepResult{}, errors.New("executor failed")
		}
		<-ctx.Done()
		close(cancelled)
		return orchestrator.StepResult{}, ctx.Err()
	})
	plan := orchestrator.Plan{Steps: []orchestrator.Step{
		{ID: "fails", ConflictKeys: []string{"failure-key"}},
		{ID: "active", ConflictKeys: []string{"active-key"}},
	}}
	done := make(chan error, 1)
	go func() {
		scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 2})
		if err == nil {
			_, err = scheduler.Run(context.Background(), plan)
		}
		done <- err
	}()
	<-cancelled
	if err := <-done; err == nil {
		t.Fatal("Run succeeded after executor failure")
	}
}

func TestSchedulerCheckpointsSuccessfulSiblingAfterAnotherStepFails(t *testing.T) {
	started := make(chan string, 2)
	releaseFailure := make(chan struct{})
	var mu sync.Mutex
	starts := map[string]int{}
	executor := orchestrator.ExecutorFunc(func(ctx context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		mu.Lock()
		starts[step.ID]++
		mu.Unlock()
		started <- step.ID
		if step.ID == "fails" {
			<-releaseFailure
			return orchestrator.StepResult{}, errors.New("failed")
		}
		// The scheduler cancels this sibling after the failure. An executor may
		// still finish successfully while observing cancellation.
		<-ctx.Done()
		return orchestrator.StepResult{StepID: step.ID}, nil
	})
	scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{MaxParallelism: 2})
	if err != nil {
		t.Fatal(err)
	}
	plan := orchestrator.Plan{ID: "checkpoint", Steps: []orchestrator.Step{{ID: "fails"}, {ID: "succeeds"}}}
	done := make(chan error, 1)
	go func() { _, err := scheduler.Run(context.Background(), plan); done <- err }()
	<-started
	<-started
	close(releaseFailure)
	if err := <-done; err == nil {
		t.Fatal("Run succeeded")
	}
	if _, err := scheduler.Resume(context.Background(), "checkpoint"); err == nil {
		t.Fatal("Resume succeeded despite repeated failure")
	}
	mu.Lock()
	defer mu.Unlock()
	if starts["succeeds"] != 1 || starts["fails"] != 2 {
		t.Fatalf("starts=%#v; successful sibling must not repeat", starts)
	}
}
