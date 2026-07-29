package orchestrator_test

import (
	"context"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestSubmitValidatesInputAndDoesNotPersistInvalidTask(t *testing.T) {
	store := orchestrator.NewMemoryStore()
	service, err := orchestrator.NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	_, err = service.Submit(context.Background(), orchestrator.SubmitRequest{IdempotencyKey: "k1", Description: "  "})
	if err == nil {
		t.Fatal("blank description accepted")
	}
	if store.Count() != 0 {
		t.Fatalf("invalid request persisted %d workflows", store.Count())
	}
}

func TestSubmitIsIdempotentAcrossRetries(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.Submit(context.Background(), orchestrator.SubmitRequest{IdempotencyKey: "same", Description: "write a test"})
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.Submit(context.Background(), orchestrator.SubmitRequest{IdempotencyKey: "same", Description: "write a test"})
	if err != nil {
		t.Fatal(err)
	}
	if first.WorkflowID != second.WorkflowID || !second.Replayed {
		t.Fatalf("first=%#v second=%#v", first, second)
	}
}

func TestTerminalWorkflowCannotTransitionBackToRunning(t *testing.T) {
	w := orchestrator.NewWorkflow("task-1", "key-1")
	if err := w.Transition(orchestrator.WorkflowRunning); err != nil {
		t.Fatal(err)
	}
	if err := w.Transition(orchestrator.WorkflowCompleted); err != nil {
		t.Fatal(err)
	}
	if err := w.Transition(orchestrator.WorkflowRunning); err == nil {
		t.Fatal("terminal workflow reopened")
	}
}
