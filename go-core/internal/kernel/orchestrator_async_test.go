package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/delivery"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestForwardTaskDeliveriesStopsOnContextCancel(t *testing.T) {
	o := &Orchestrator{}
	ctx, cancel := context.WithCancel(context.Background())
	plainTasks := make(chan domain.Task, 1)
	out := make(chan delivery.TaskDelivery)
	done := make(chan struct{})
	go func() {
		defer close(done)
		o.forwardTaskDeliveries(ctx, plainTasks, out)
	}()
	plainTasks <- domain.Task{ID: "task-1"}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("forwardTaskDeliveries did not stop after context cancel")
	}
}

func TestForwardResultDeliveriesStopsOnContextCancel(t *testing.T) {
	o := &Orchestrator{}
	ctx, cancel := context.WithCancel(context.Background())
	plainResults := make(chan domain.TaskResultEnvelope, 1)
	results := make(chan delivery.ResultDelivery)
	done := make(chan struct{})
	go func() {
		defer close(done)
		o.forwardResultDeliveries(ctx, plainResults, results)
	}()
	plainResults <- domain.TaskResultEnvelope{TaskID: "task-1"}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("forwardResultDeliveries did not stop after context cancel")
	}
}
