package delivery

import (
	"context"
	"sync"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type MessageBus struct {
	mu           sync.Mutex
	queues       map[string][]domain.TaskEnvelope
	tasks        []domain.Task
	results      []domain.TaskResultEnvelope
	acks         map[string][]domain.MessageAck
	unacked      map[string]domain.TaskEnvelope
	deadLetters  []domain.TaskEnvelope
	queueSignals map[string]chan struct{}
	taskSignal   chan struct{}
	resultSignal chan struct{}
}

func NewMessageBus() *MessageBus {
	return &MessageBus{
		queues:       map[string][]domain.TaskEnvelope{},
		acks:         map[string][]domain.MessageAck{},
		unacked:      map[string]domain.TaskEnvelope{},
		queueSignals: map[string]chan struct{}{},
		taskSignal:   make(chan struct{}, 1),
		resultSignal: make(chan struct{}, 1),
	}
}

func (b *MessageBus) BrokerManaged() bool { return false }

func (b *MessageBus) queueSignal(topic string) chan struct{} {
	signal, ok := b.queueSignals[topic]
	if !ok {
		signal = make(chan struct{}, 1)
		b.queueSignals[topic] = signal
	}
	return signal
}

func notifySignal(signal chan struct{}) {
	select {
	case signal <- struct{}{}:
	default:
	}
}

func (b *MessageBus) waitForEnvelope(ctx context.Context, topic string) bool {
	b.mu.Lock()
	if len(b.queues[topic]) > 0 {
		b.mu.Unlock()
		return true
	}
	signal := b.queueSignal(topic)
	b.mu.Unlock()
	select {
	case <-ctx.Done():
		return false
	case <-signal:
		return true
	}
}

func (b *MessageBus) waitForTasks(ctx context.Context) bool {
	b.mu.Lock()
	if len(b.tasks) > 0 {
		b.mu.Unlock()
		return true
	}
	signal := b.taskSignal
	b.mu.Unlock()
	select {
	case <-ctx.Done():
		return false
	case <-signal:
		return true
	}
}

func (b *MessageBus) waitForResults(ctx context.Context) bool {
	b.mu.Lock()
	if len(b.results) > 0 {
		b.mu.Unlock()
		return true
	}
	signal := b.resultSignal
	b.mu.Unlock()
	select {
	case <-ctx.Done():
		return false
	case <-signal:
		return true
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
	b.queues[topic] = append(b.queues[topic], envelope)
	signal := b.queueSignal(topic)
	b.mu.Unlock()
	notifySignal(signal)
}

func (b *MessageBus) requeueFront(topic string, envelope domain.TaskEnvelope) {
	b.mu.Lock()
	queue := append([]domain.TaskEnvelope{envelope}, b.queues[topic]...)
	b.queues[topic] = queue
	signal := b.queueSignal(topic)
	b.mu.Unlock()
	notifySignal(signal)
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

func (b *MessageBus) ConsumeEnvelopes(ctx context.Context, topic string, prefetch int) (<-chan domain.TaskEnvelope, error) {
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
					return
				case out <- delivery.Envelope:
				}
				if delivery.Ack != nil {
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) ConsumeEnvelopeDeliveries(ctx context.Context, topic string, _ int) (<-chan EnvelopeDelivery, error) {
	out := make(chan EnvelopeDelivery)
	go func() {
		defer close(out)
		for {
			if envelope, ok := b.Consume(topic); ok {
				env := envelope
				delivery := EnvelopeDelivery{
					Envelope: env,
					Ack:      func() error { return nil },
					Nack: func(requeue bool) error {
						if requeue {
							b.requeueFront(topic, env)
						}
						return nil
					},
				}
				select {
				case <-ctx.Done():
					return
				case out <- delivery:
				}
				continue
			}
			if !b.waitForEnvelope(ctx, topic) {
				return
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) EnqueueTask(_ context.Context, task domain.Task) error {
	b.mu.Lock()
	b.tasks = append(b.tasks, task)
	signal := b.taskSignal
	b.mu.Unlock()
	notifySignal(signal)
	return nil
}

func (b *MessageBus) requeueTaskFront(task domain.Task) {
	b.mu.Lock()
	b.tasks = append([]domain.Task{task}, b.tasks...)
	signal := b.taskSignal
	b.mu.Unlock()
	notifySignal(signal)
}

func (b *MessageBus) ConsumeTasks(ctx context.Context, prefetch int) (<-chan domain.Task, error) {
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
					return
				case out <- delivery.Task:
				}
				if delivery.Ack != nil {
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) ConsumeTaskDeliveries(ctx context.Context, _ int) (<-chan TaskDelivery, error) {
	out := make(chan TaskDelivery)
	go func() {
		defer close(out)
		for {
			b.mu.Lock()
			if len(b.tasks) > 0 {
				task := b.tasks[0]
				b.tasks = append([]domain.Task(nil), b.tasks[1:]...)
				b.mu.Unlock()
				t := task
				delivery := TaskDelivery{
					Task: t,
					Ack:  func() error { return nil },
					Nack: func(requeue bool) error {
						if requeue {
							b.requeueTaskFront(t)
						}
						return nil
					},
				}
				select {
				case <-ctx.Done():
					return
				case out <- delivery:
				}
				continue
			}
			b.mu.Unlock()
			if !b.waitForTasks(ctx) {
				return
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) PublishResult(_ context.Context, result domain.TaskResultEnvelope) error {
	b.mu.Lock()
	b.results = append(b.results, result)
	signal := b.resultSignal
	b.mu.Unlock()
	notifySignal(signal)
	return nil
}

func (b *MessageBus) requeueResultFront(result domain.TaskResultEnvelope) {
	b.mu.Lock()
	b.results = append([]domain.TaskResultEnvelope{result}, b.results...)
	signal := b.resultSignal
	b.mu.Unlock()
	notifySignal(signal)
}

func (b *MessageBus) ConsumeResults(ctx context.Context, prefetch int) (<-chan domain.TaskResultEnvelope, error) {
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
					return
				case out <- delivery.Result:
				}
				if delivery.Ack != nil {
					_ = delivery.Ack()
				}
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) ConsumeResultDeliveries(ctx context.Context, _ int) (<-chan ResultDelivery, error) {
	out := make(chan ResultDelivery)
	go func() {
		defer close(out)
		for {
			b.mu.Lock()
			if len(b.results) > 0 {
				result := b.results[0]
				b.results = append([]domain.TaskResultEnvelope(nil), b.results[1:]...)
				b.mu.Unlock()
				r := result
				delivery := ResultDelivery{
					Result: r,
					Ack:    func() error { return nil },
					Nack: func(requeue bool) error {
						if requeue {
							b.requeueResultFront(r)
						}
						return nil
					},
				}
				select {
				case <-ctx.Done():
					return
				case out <- delivery:
				}
				continue
			}
			b.mu.Unlock()
			if !b.waitForResults(ctx) {
				return
			}
		}
	}()
	return out, nil
}

func (b *MessageBus) SendEnvelope(envelope domain.TaskEnvelope) domain.MessageAck {
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

func (b *MessageBus) ReceiveForAgent(agentID string) (domain.TaskEnvelope, bool) {
	return b.Consume(AgentTopic(agentID))
}

func (b *MessageBus) Ack(messageID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	ack := domain.MessageAck{MessageID: messageID, AckStatus: status, ReceivedBy: receivedBy, Reason: reason}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.acks[messageID] = append(b.acks[messageID], ack)
	if status == domain.AckStatusAccepted || status == domain.AckStatusFailed || status == domain.AckStatusDeadLettered {
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
	return b.Ack(envelope.TaskID, domain.AckStatusDeadLettered, "dead_letter_queue", reason)
}

func (b *MessageBus) DeadLetters() []domain.TaskEnvelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]domain.TaskEnvelope(nil), b.deadLetters...)
}
