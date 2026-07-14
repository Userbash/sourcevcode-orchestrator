package delivery

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"strconv"
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
	backend := strings.ToLower(strings.TrimSpace(os.Getenv("GO_CORE_MESSAGE_BUS_BACKEND")))
	if backend == "" {
		backend = strings.ToLower(strings.TrimSpace(os.Getenv("AI_BRIDGE_MESSAGE_BUS_BACKEND")))
	}
	if backend == "memory" || backend == "inmemory" || backend == "in-memory" {
		return NewMessageBus()
	}
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
	for _, topic := range []string{deadLetterTopic, SubmissionTopic, ResultTopic} {
		if err := bus.ensureQueue(topic); err != nil {
			_ = channel.Close()
			_ = conn.Close()
			return nil, err
		}
	}
	return bus, nil
}

func (b *RabbitMQBus) BrokerManaged() bool {
	return true
}

func (b *RabbitMQBus) ensureQueue(topic string) error {
	_, err := b.channel.QueueDeclare(topic, true, false, false, false, b.queueArgs(topic))
	return err
}

func (b *RabbitMQBus) queueArgs(topic string) amqp.Table {
	args := amqp.Table{}
	if topic != deadLetterTopic {
		args["x-dead-letter-exchange"] = ""
		args["x-dead-letter-routing-key"] = deadLetterTopic
	}
	if ttl := brokerMessageTTL(); ttl > 0 {
		args["x-message-ttl"] = int32(ttl)
	}
	if len(args) == 0 {
		return nil
	}
	return args
}

func brokerMessageTTL() int {
	raw := strings.TrimSpace(os.Getenv("GO_CORE_RABBITMQ_MESSAGE_TTL_MS"))
	if raw == "" {
		return 0
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return 0
	}
	return value
}

func (b *RabbitMQBus) publishJSON(ctx context.Context, topic string, body []byte, expiration string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.ensureQueue(topic); err != nil {
		return err
	}
	return b.channel.PublishWithContext(ctx, "", topic, false, false, amqp.Publishing{
		ContentType:  "application/json",
		DeliveryMode: amqp.Persistent,
		Timestamp:    time.Now().UTC(),
		Expiration:   expiration,
		Body:         body,
	})
}

func (b *RabbitMQBus) Publish(topic string, envelope domain.TaskEnvelope) {
	body, err := json.Marshal(envelope)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = b.publishJSON(ctx, topic, body, ttlExpiration(envelope.TTL))
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

func (b *RabbitMQBus) ConsumeEnvelopes(ctx context.Context, topic string, prefetch int) (<-chan domain.TaskEnvelope, error) {
	deliveries, err := b.ConsumeEnvelopeDeliveries(ctx, topic, prefetch)
	if err != nil {
		return nil, err
	}
	out := make(chan domain.TaskEnvelope)
	go func() {
		defer close(out)
		for {
			select {
			case <-ctx.Done():
				return
			case delivery, ok := <-deliveries:
				if !ok {
					return
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery.Envelope:
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) ConsumeEnvelopeDeliveries(ctx context.Context, topic string, prefetch int) (<-chan EnvelopeDelivery, error) {
	if err := b.ensureQueue(topic); err != nil {
		return nil, err
	}
	channel, err := b.conn.Channel()
	if err != nil {
		return nil, err
	}
	if prefetch <= 0 {
		prefetch = 1
	}
	if err := channel.Qos(prefetch, 0, false); err != nil {
		_ = channel.Close()
		return nil, err
	}
	rawDeliveries, err := channel.Consume(topic, "", false, false, false, false, nil)
	if err != nil {
		_ = channel.Close()
		return nil, err
	}
	out := make(chan EnvelopeDelivery)
	go func() {
		defer close(out)
		defer channel.Close()
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-rawDeliveries:
				if !ok {
					return
				}
				var envelope domain.TaskEnvelope
				if err := json.Unmarshal(msg.Body, &envelope); err != nil {
					_ = msg.Nack(false, false)
					continue
				}
				delivery := EnvelopeDelivery{
					Envelope: envelope,
					Ack: func() error {
						return msg.Ack(false)
					},
					Nack: func(requeue bool) error {
						return msg.Nack(false, requeue)
					},
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery:
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) EnqueueTask(ctx context.Context, task domain.Task) error {
	body, err := json.Marshal(task)
	if err != nil {
		return err
	}
	return b.publishJSON(ctx, SubmissionTopic, body, "")
}

func (b *RabbitMQBus) ConsumeTasks(ctx context.Context, prefetch int) (<-chan domain.Task, error) {
	deliveries, err := b.ConsumeTaskDeliveries(ctx, prefetch)
	if err != nil {
		return nil, err
	}
	out := make(chan domain.Task)
	go func() {
		defer close(out)
		for {
			select {
			case <-ctx.Done():
				return
			case delivery, ok := <-deliveries:
				if !ok {
					return
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery.Task:
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) ConsumeTaskDeliveries(ctx context.Context, prefetch int) (<-chan TaskDelivery, error) {
	if err := b.ensureQueue(SubmissionTopic); err != nil {
		return nil, err
	}
	channel, err := b.conn.Channel()
	if err != nil {
		return nil, err
	}
	if prefetch <= 0 {
		prefetch = 1
	}
	if err := channel.Qos(prefetch, 0, false); err != nil {
		_ = channel.Close()
		return nil, err
	}
	rawDeliveries, err := channel.Consume(SubmissionTopic, "", false, false, false, false, nil)
	if err != nil {
		_ = channel.Close()
		return nil, err
	}
	out := make(chan TaskDelivery)
	go func() {
		defer close(out)
		defer channel.Close()
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-rawDeliveries:
				if !ok {
					return
				}
				var task domain.Task
				if err := json.Unmarshal(msg.Body, &task); err != nil {
					_ = msg.Nack(false, false)
					continue
				}
				delivery := TaskDelivery{
					Task: task,
					Ack: func() error {
						return msg.Ack(false)
					},
					Nack: func(requeue bool) error {
						return msg.Nack(false, requeue)
					},
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery:
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) PublishResult(ctx context.Context, result domain.TaskResultEnvelope) error {
	body, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return b.publishJSON(ctx, ResultTopic, body, "")
}

func (b *RabbitMQBus) ConsumeResults(ctx context.Context, prefetch int) (<-chan domain.TaskResultEnvelope, error) {
	deliveries, err := b.ConsumeResultDeliveries(ctx, prefetch)
	if err != nil {
		return nil, err
	}
	out := make(chan domain.TaskResultEnvelope)
	go func() {
		defer close(out)
		for {
			select {
			case <-ctx.Done():
				return
			case delivery, ok := <-deliveries:
				if !ok {
					return
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery.Result:
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) ConsumeResultDeliveries(ctx context.Context, prefetch int) (<-chan ResultDelivery, error) {
	if err := b.ensureQueue(ResultTopic); err != nil {
		return nil, err
	}
	channel, err := b.conn.Channel()
	if err != nil {
		return nil, err
	}
	if prefetch <= 0 {
		prefetch = 1
	}
	if err := channel.Qos(prefetch, 0, false); err != nil {
		_ = channel.Close()
		return nil, err
	}
	rawDeliveries, err := channel.Consume(ResultTopic, "", false, false, false, false, nil)
	if err != nil {
		_ = channel.Close()
		return nil, err
	}
	out := make(chan ResultDelivery)
	go func() {
		defer close(out)
		defer channel.Close()
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-rawDeliveries:
				if !ok {
					return
				}
				var result domain.TaskResultEnvelope
				if err := json.Unmarshal(msg.Body, &result); err != nil {
					_ = msg.Nack(false, false)
					continue
				}
				delivery := ResultDelivery{
					Result: result,
					Ack: func() error {
						return msg.Ack(false)
					},
					Nack: func(requeue bool) error {
						return msg.Nack(false, requeue)
					},
				}
				select {
				case <-ctx.Done():
					_ = delivery.Nack(true)
					return
				case out <- delivery:
				}
			}
		}
	}()
	return out, nil
}

func (b *RabbitMQBus) SendEnvelope(envelope domain.TaskEnvelope) domain.MessageAck {
	if envelope.MaxHops > 0 && envelope.HopCount >= envelope.MaxHops {
		return b.MarkDeadLetterEnvelope(envelope, "max_hops_exceeded")
	}
	envelope.HopCount++
	b.Publish(AgentTopic(envelope.TargetAgent), envelope)
	sentAck := domain.MessageAck{MessageID: envelope.TaskID, AckStatus: domain.AckStatusSent}
	queuedAck := domain.MessageAck{MessageID: envelope.TaskID, AckStatus: domain.AckStatusQueued}
	b.mu.Lock()
	b.unacked[envelope.TaskID] = envelope
	b.acks[envelope.TaskID] = append(b.acks[envelope.TaskID], sentAck, queuedAck)
	b.mu.Unlock()
	return queuedAck
}

func (b *RabbitMQBus) ReceiveForAgent(agentID string) (domain.TaskEnvelope, bool) {
	return b.Consume(AgentTopic(agentID))
}

func (b *RabbitMQBus) Ack(messageID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	ack := domain.MessageAck{MessageID: messageID, AckStatus: status, ReceivedBy: receivedBy, Reason: reason}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.acks[messageID] = append(b.acks[messageID], ack)
	if status == domain.AckStatusAccepted || status == domain.AckStatusFailed || status == domain.AckStatusDeadLettered {
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
	return b.Ack(envelope.TaskID, domain.AckStatusDeadLettered, deadLetterTopic, reason)
}

func (b *RabbitMQBus) DeadLetters() []domain.TaskEnvelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]domain.TaskEnvelope(nil), b.deadLetters...)
}

func ttlExpiration(ttl int) string {
	if ttl <= 0 {
		return ""
	}
	return strconv.Itoa(ttl)
}
