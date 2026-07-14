package delivery

import (
	"sync"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type MessageBus struct {
	mu          sync.Mutex
	queues      map[string][]domain.TaskEnvelope
	acks        map[string][]domain.MessageAck
	unacked     map[string]domain.TaskEnvelope
	deadLetters []domain.TaskEnvelope
}

func NewMessageBus() *MessageBus {
	return &MessageBus{
		queues:  map[string][]domain.TaskEnvelope{},
		acks:    map[string][]domain.MessageAck{},
		unacked: map[string]domain.TaskEnvelope{},
	}
}

func AgentTopic(agentID string) string {
	if agentID == "" {
		return "orchestrator.inbox"
	}
	return "agent." + agentID + ".inbox"
}

func (b *MessageBus) Publish(topic string, envelope domain.TaskEnvelope) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.queues[topic] = append(b.queues[topic], envelope)
}

func (b *MessageBus) Consume(topic string) (domain.TaskEnvelope, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	queue := b.queues[topic]
	if len(queue) == 0 {
		return domain.TaskEnvelope{}, false
	}
	item := queue[0]
	b.queues[topic] = append([]domain.TaskEnvelope(nil), queue[1:]...)
	return item, true
}

func (b *MessageBus) SendEnvelope(envelope domain.TaskEnvelope) domain.MessageAck {
	if envelope.MaxHops > 0 && envelope.HopCount >= envelope.MaxHops {
		return b.MarkDeadLetterEnvelope(envelope, "max_hops_exceeded")
	}
	envelope.HopCount++
	b.Publish(AgentTopic(envelope.TargetAgent), envelope)
	ack := domain.MessageAck{MessageID: envelope.TaskID, AckStatus: domain.AckStatusSent}
	b.mu.Lock()
	b.unacked[envelope.TaskID] = envelope
	b.acks[envelope.TaskID] = append(b.acks[envelope.TaskID], ack)
	b.mu.Unlock()
	return ack
}

func (b *MessageBus) ReceiveForAgent(agentID string) (domain.TaskEnvelope, bool) {
	return b.Consume(AgentTopic(agentID))
}

func (b *MessageBus) Ack(messageID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	ack := domain.MessageAck{MessageID: messageID, AckStatus: status, ReceivedBy: receivedBy, Reason: reason}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.acks[messageID] = append(b.acks[messageID], ack)
	if status == domain.AckStatusAccepted || status == domain.AckStatusFailed {
		delete(b.unacked, messageID)
	}
	return ack
}

func (b *MessageBus) AckHistory(messageID string) []domain.MessageAck {
	b.mu.Lock()
	defer b.mu.Unlock()
	history := b.acks[messageID]
	return append([]domain.MessageAck(nil), history...)
}

func (b *MessageBus) Depth(topic string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.queues[topic])
}

func (b *MessageBus) MarkDeadLetterEnvelope(envelope domain.TaskEnvelope, reason string) domain.MessageAck {
	envelope.IsDeadLetter = true
	b.mu.Lock()
	b.deadLetters = append(b.deadLetters, envelope)
	delete(b.unacked, envelope.TaskID)
	b.mu.Unlock()
	return b.Ack(envelope.TaskID, domain.AckStatusFailed, "dead_letter_queue", reason)
}

func (b *MessageBus) DeadLetters() []domain.TaskEnvelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]domain.TaskEnvelope(nil), b.deadLetters...)
}
