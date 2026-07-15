package delivery

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestWorkerPoolRetriesAndAccepts(t *testing.T) {
	supervisor := NewSupervisor(NewMessageBus(), 5*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var mu sync.Mutex
	attempts := map[string]int{}
	pool := NewWorkerPool(supervisor, "coder-local", 2, 5*time.Millisecond, func(_ context.Context, envelope domain.TaskEnvelope) error {
		mu.Lock()
		defer mu.Unlock()
		attempts[envelope.TaskID]++
		if attempts[envelope.TaskID] == 1 {
			return errors.New("transient tool timeout")
		}
		return nil
	}, nil)
	pool.Start(ctx)

	supervisor.Dispatch(context.Background(), domain.TaskEnvelope{
		TaskID:      "task-retry",
		TargetAgent: "coder-local",
		MaxRetries:  2,
		Payload:     domain.TaskPayload{Objective: "retry and complete"},
	})

	waitFor(t, 2*time.Second, func() bool {
		snapshot := supervisor.Snapshot("task-retry")
		return snapshot["status"] == "accepted"
	})

	history := supervisor.messageBus.AckHistory("task-retry")
	if !containsAck(history, domain.AckStatusRetrying) {
		t.Fatalf("expected retrying status in history, got %#v", history)
	}
	if !containsAck(history, domain.AckStatusAccepted) {
		t.Fatalf("expected accepted status in history, got %#v", history)
	}

	metrics := pool.Snapshot()
	if metrics["retried"] != 1 {
		t.Fatalf("expected one retry in pool metrics, got %#v", metrics)
	}
	if metrics["succeeded"] != 1 {
		t.Fatalf("expected one success in pool metrics, got %#v", metrics)
	}
}

func TestWorkerPoolDeadLettersAfterRetryBudget(t *testing.T) {
	supervisor := NewSupervisor(NewMessageBus(), 5*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewWorkerPool(supervisor, "reviewer-local", 1, 5*time.Millisecond, func(context.Context, domain.TaskEnvelope) error {
		return errors.New("permanent failure")
	}, nil)
	pool.Start(ctx)

	supervisor.Dispatch(context.Background(), domain.TaskEnvelope{
		TaskID:      "task-dead-letter",
		TargetAgent: "reviewer-local",
		MaxRetries:  1,
		Payload:     domain.TaskPayload{Objective: "must dead-letter"},
	})

	waitFor(t, 2*time.Second, func() bool {
		snapshot := supervisor.Snapshot("task-dead-letter")
		return snapshot["status"] == "dead_lettered"
	})

	deadLetters := supervisor.messageBus.DeadLetters()
	if len(deadLetters) != 1 || deadLetters[0].TaskID != "task-dead-letter" {
		t.Fatalf("expected one dead letter, got %#v", deadLetters)
	}

	metrics := pool.Snapshot()
	if metrics["dead_lettered"] != 1 {
		t.Fatalf("expected one dead-lettered metric, got %#v", metrics)
	}
}

func waitFor(t *testing.T, timeout time.Duration, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ready() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", timeout)
}

func containsAck(history []domain.MessageAck, status domain.AckStatus) bool {
	for _, ack := range history {
		if ack.AckStatus == status {
			return true
		}
	}
	return false
}
