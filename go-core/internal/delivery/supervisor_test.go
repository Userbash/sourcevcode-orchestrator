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
	if snapshot["status"] != "queued" {
		t.Fatalf("expected queued snapshot, got %#v", snapshot)
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

	snapshot := supervisor.Snapshot("task-timeout")
	if snapshot["status"] != "queued" {
		t.Fatalf("expected queued after retry requeue, got %#v", snapshot)
	}

	supervisor.nowFn = func() time.Time { return baseTime.Add(4 * time.Second) }
	result = supervisor.InspectTimeouts(context.Background())
	if result["dead_lettered"] != 1 {
		t.Fatalf("expected one dead letter, got %#v", result)
	}

	snapshot = supervisor.Snapshot("task-timeout")
	if snapshot["status"] != "dead_lettered" {
		t.Fatalf("expected dead_lettered snapshot, got %#v", snapshot)
	}
}

func TestSupervisorDispatchAllowsImmediatePayloadConfirmation(t *testing.T) {
	supervisor := NewSupervisor(NewMessageBus(), time.Minute)
	envelope := domain.TaskEnvelope{
		TaskID:      "task-immediate-confirm",
		SourceAgent: "orchestrator",
		TargetAgent: "coder-local",
		Payload: domain.TaskPayload{
			Objective: "Validate payload immediately after dispatch",
		},
	}

	supervisor.Dispatch(context.Background(), envelope)

	deliveries, ok := any(supervisor.messageBus).(EnvelopeDeliveryStream)
	if !ok {
		t.Fatal("message bus does not support envelope delivery stream")
	}
	stream, err := deliveries.ConsumeEnvelopeDeliveries(context.Background(), AgentTopic("coder-local"), 1)
	if err != nil {
		t.Fatalf("ConsumeEnvelopeDeliveries() error = %v", err)
	}

	var delivery EnvelopeDelivery
	select {
	case delivery = <-stream:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for envelope delivery")
	}

	if !supervisor.ConfirmPayload(envelope.TaskID, "coder-local", delivery.Envelope) {
		t.Fatal("expected immediate payload confirmation to succeed")
	}

	snapshot := supervisor.Snapshot(envelope.TaskID)
	if snapshot["payload_validated"] != true {
		t.Fatalf("payload_validated = %#v, want true", snapshot["payload_validated"])
	}
	if snapshot["handshake_state"] != "ack_valid" {
		t.Fatalf("handshake_state = %#v, want ack_valid", snapshot["handshake_state"])
	}
}
