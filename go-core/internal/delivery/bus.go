package delivery

import "sourcevcode-orchestrator/go-core/internal/domain"

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
