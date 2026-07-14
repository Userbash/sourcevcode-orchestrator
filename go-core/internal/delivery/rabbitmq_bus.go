package delivery

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"strings"
	"sync"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"

	"sourcevcode-orchestrator/go-core/internal/app"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

const deadLetterTopic = "dead_letter_queue"

type RabbitMQBus struct {
	url         string
	conn        *amqp.Connection
	channel     *amqp.Channel
	mu          sync.Mutex
	acks        map[string][]domain.MessageAck
	unacked     map[string]domain.TaskEnvelope
	deadLetters []domain.TaskEnvelope
}

func OpenBusFromEnv() Bus {
	url := strings.TrimSpace(os.Getenv("AI_BRIDGE_RABBITMQ_URL"))
	if url == "" {
		url = app.ResolveRabbitMQConnectionInfo().AMQPURL
	}
	if url == "" {
		return NewMessageBus()
	}
	bus, err := NewRabbitMQBus(url)
	if err != nil {
		log.Printf("delivery: RabbitMQ unavailable (%v), falling back to in-memory bus", err)
		return NewMessageBus()
	}
	return bus
}

func NewRabbitMQBus(url string) (*RabbitMQBus, error) {
	conn, err := amqp.Dial(url)
	if err != nil {
		return nil, err
	}
	channel, err := conn.Channel()
	if err != nil {
		_ = conn.Close()
		return nil, err
	}
	bus := &RabbitMQBus{
		url:     url,
		conn:    conn,
		channel: channel,
		acks:    map[string][]domain.MessageAck{},
		unacked: map[string]domain.TaskEnvelope{},
	}
	if err := bus.ensureQueue(deadLetterTopic); err != nil {
		_ = channel.Close()
		_ = conn.Close()
		return nil, err
	}
	return bus, nil
}

func (b *RabbitMQBus) ensureQueue(topic string) error {
	_, err := b.channel.QueueDeclare(topic, true, false, false, false, nil)
	return err
}

func (b *RabbitMQBus) Publish(topic string, envelope domain.TaskEnvelope) {
	body, err := json.Marshal(envelope)
	if err != nil {
		return
	}
	if err := b.ensureQueue(topic); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = b.channel.PublishWithContext(ctx, "", topic, false, false, amqp.Publishing{
		ContentType:  "application/json",
		DeliveryMode: amqp.Persistent,
		Timestamp:    time.Now().UTC(),
		Body:         body,
	})
}

func (b *RabbitMQBus) Consume(topic string) (domain.TaskEnvelope, bool) {
	if err := b.ensureQueue(topic); err != nil {
		return domain.TaskEnvelope{}, false
	}
	msg, ok, err := b.channel.Get(topic, true)
	if err != nil || !ok {
		return domain.TaskEnvelope{}, false
	}
	var envelope domain.TaskEnvelope
	if err := json.Unmarshal(msg.Body, &envelope); err != nil {
		return domain.TaskEnvelope{}, false
	}
	return envelope, true
}

func (b *RabbitMQBus) SendEnvelope(envelope domain.TaskEnvelope) domain.MessageAck {
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

func (b *RabbitMQBus) ReceiveForAgent(agentID string) (domain.TaskEnvelope, bool) {
	return b.Consume(AgentTopic(agentID))
}

func (b *RabbitMQBus) Ack(messageID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	ack := domain.MessageAck{MessageID: messageID, AckStatus: status, ReceivedBy: receivedBy, Reason: reason}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.acks[messageID] = append(b.acks[messageID], ack)
	if status == domain.AckStatusAccepted || status == domain.AckStatusFailed {
		delete(b.unacked, messageID)
	}
	return ack
}

func (b *RabbitMQBus) AckHistory(messageID string) []domain.MessageAck {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]domain.MessageAck(nil), b.acks[messageID]...)
}

func (b *RabbitMQBus) Depth(topic string) int {
	if err := b.ensureQueue(topic); err != nil {
		return 0
	}
	queue, err := b.channel.QueueInspect(topic)
	if err != nil {
		return 0
	}
	return queue.Messages
}

func (b *RabbitMQBus) MarkDeadLetterEnvelope(envelope domain.TaskEnvelope, reason string) domain.MessageAck {
	envelope.IsDeadLetter = true
	b.Publish(deadLetterTopic, envelope)
	b.mu.Lock()
	b.deadLetters = append(b.deadLetters, envelope)
	delete(b.unacked, envelope.TaskID)
	b.mu.Unlock()
	return b.Ack(envelope.TaskID, domain.AckStatusFailed, deadLetterTopic, reason)
}

func (b *RabbitMQBus) DeadLetters() []domain.TaskEnvelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]domain.TaskEnvelope(nil), b.deadLetters...)
}
