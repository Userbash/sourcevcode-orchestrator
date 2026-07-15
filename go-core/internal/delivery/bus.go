package delivery

import (
	"context"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const (
	SubmissionTopic = "scheduler.submit"
	ResultTopic     = "scheduler.results"
)

type Bus interface {
	Publish(topic string, envelope domain.TaskEnvelope)
	Consume(topic string) (domain.TaskEnvelope, bool)
	SendEnvelope(envelope domain.TaskEnvelope) domain.MessageAck
	ReceiveForAgent(agentID string) (domain.TaskEnvelope, bool)
	Ack(messageID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck
	AckHistory(messageID string) []domain.MessageAck
	Depth(topic string) int
	MarkDeadLetterEnvelope(envelope domain.TaskEnvelope, reason string) domain.MessageAck
	DeadLetters() []domain.TaskEnvelope
}

type EnvelopeStream interface {
	ConsumeEnvelopes(ctx context.Context, topic string, prefetch int) (<-chan domain.TaskEnvelope, error)
}

type EnvelopeDelivery struct {
	Envelope domain.TaskEnvelope
	Ack      func() error
	Nack     func(requeue bool) error
}

type TaskDelivery struct {
	Task domain.Task
	Ack  func() error
	Nack func(requeue bool) error
}

type ResultDelivery struct {
	Result domain.TaskResultEnvelope
	Ack    func() error
	Nack   func(requeue bool) error
}

type EnvelopeDeliveryStream interface {
	ConsumeEnvelopeDeliveries(ctx context.Context, topic string, prefetch int) (<-chan EnvelopeDelivery, error)
}

type TaskSubmissionQueue interface {
	EnqueueTask(ctx context.Context, task domain.Task) error
	ConsumeTasks(ctx context.Context, prefetch int) (<-chan domain.Task, error)
}

type TaskDeliveryStream interface {
	ConsumeTaskDeliveries(ctx context.Context, prefetch int) (<-chan TaskDelivery, error)
}

type TaskResultQueue interface {
	PublishResult(ctx context.Context, result domain.TaskResultEnvelope) error
	ConsumeResults(ctx context.Context, prefetch int) (<-chan domain.TaskResultEnvelope, error)
}

type ResultDeliveryStream interface {
	ConsumeResultDeliveries(ctx context.Context, prefetch int) (<-chan ResultDelivery, error)
}

type BrokerManagedBus interface {
	BrokerManaged() bool
}
