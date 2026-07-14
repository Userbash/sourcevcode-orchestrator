package delivery

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestSupervisorDeliveryLifecycle(t *testing.T) {
	supervisor := NewSupervisor(NewMessageBus(), time.Minute)
	envelope := domain.TaskEnvelope{
		TaskID:      "task-1",
		SourceAgent: "orchestrator",
		TargetAgent: "coder-local",
		Payload: domain.TaskPayload{
			Objective: "Implement migration",
		},
	}

	snapshot := supervisor.Dispatch(context.Background(), envelope)
	if snapshot["status"] != "sent" {
		t.Fatalf("expected sent snapshot, got %#v", snapshot)
	}

	mailbox := supervisor.FetchAgentMailbox(context.Background(), "coder-local", 1)
	if len(mailbox) != 1 || mailbox[0].TaskID != "task-1" {
		t.Fatalf("unexpected mailbox fetch: %#v", mailbox)
	}

	if !supervisor.ConfirmPayload("task-1", "coder-local", mailbox[0]) {
		t.Fatalf("expected payload validation to succeed")
	}

	ack := supervisor.EstablishDelivery("task-1", "coder-local")
	if ack.AckStatus != domain.AckStatusReceived {
		t.Fatalf("expected received ack, got %#v", ack)
	}

	supervisor.messageBus.Ack("task-1", domain.AckStatusAccepted, "coder-local", "")
	snapshot = supervisor.Refresh(context.Background(), "task-1")
	if snapshot["status"] != "accepted" {
		t.Fatalf("expected accepted snapshot, got %#v", snapshot)
	}

	health := supervisor.DeliveryHealthSnapshot()
	if health["accepted"] != 1 {
		t.Fatalf("expected accepted health count, got %#v", health)
	}
}

func TestSupervisorRetriesTimedOutEnvelope(t *testing.T) {
	supervisor := NewSupervisor(NewMessageBus(), time.Second)
	baseTime := time.Now().UTC()
	supervisor.nowFn = func() time.Time { return baseTime }
	supervisor.Dispatch(context.Background(), domain.TaskEnvelope{
		TaskID:      "task-timeout",
		TargetAgent: "tester-local",
		MaxRetries:  1,
		Payload:     domain.TaskPayload{Objective: "Retry"},
	})

	supervisor.nowFn = func() time.Time { return baseTime.Add(2 * time.Second) }
	result := supervisor.InspectTimeouts(context.Background())
	if result["retried"] != 1 {
		t.Fatalf("expected one retry, got %#v", result)
	}

	supervisor.nowFn = func() time.Time { return baseTime.Add(4 * time.Second) }
	result = supervisor.InspectTimeouts(context.Background())
	if result["dead_lettered"] != 1 {
		t.Fatalf("expected one dead letter, got %#v", result)
	}
}
