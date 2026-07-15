package delivery

import (
	"context"
	"fmt"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type EnvelopeHandler func(ctx context.Context, envelope domain.TaskEnvelope) error

type DeadLetterHandler func(ctx context.Context, envelope domain.TaskEnvelope, reason string) error

type WorkerPool struct {
	supervisor        *Supervisor
	agentID           string
	concurrency       int
	pollInterval      time.Duration
	handler           EnvelopeHandler
	deadLetterHandler DeadLetterHandler
	nowFn             func() time.Time

	mu      sync.Mutex
	started bool
	metrics WorkerPoolMetrics
}

type WorkerPoolMetrics struct {
	AgentID              string     `json:"agent_id"`
	Concurrency          int        `json:"concurrency"`
	ActiveWorkers        int        `json:"active_workers"`
	Processed            int        `json:"processed"`
	Succeeded            int        `json:"succeeded"`
	Failed               int        `json:"failed"`
	Retried              int        `json:"retried"`
	DeadLettered         int        `json:"dead_lettered"`
	ValidationFailures   int        `json:"validation_failures"`
	IdlePolls            int        `json:"idle_polls"`
	LastTaskID           string     `json:"last_task_id,omitempty"`
	LastError            string     `json:"last_error,omitempty"`
	LastProcessedAt      *time.Time `json:"last_processed_at,omitempty"`
	AverageLatencyMillis float64    `json:"average_latency_ms"`
}

func NewWorkerPool(supervisor *Supervisor, agentID string, concurrency int, pollInterval time.Duration, handler EnvelopeHandler, deadLetterHandler DeadLetterHandler) *WorkerPool {
	if concurrency <= 0 {
		concurrency = 1
	}
	if pollInterval <= 0 {
		pollInterval = 250 * time.Millisecond
	}
	if handler == nil {
		handler = func(context.Context, domain.TaskEnvelope) error { return nil }
	}
	return &WorkerPool{
		supervisor:        supervisor,
		agentID:           agentID,
		concurrency:       concurrency,
		pollInterval:      pollInterval,
		handler:           handler,
		deadLetterHandler: deadLetterHandler,
		nowFn: func() time.Time {
			return time.Now().UTC()
		},
		metrics: WorkerPoolMetrics{
			AgentID:     agentID,
			Concurrency: concurrency,
		},
	}
}

func (p *WorkerPool) Start(ctx context.Context) {
	if p == nil || p.supervisor == nil {
		return
	}
	p.mu.Lock()
	if p.started {
		p.mu.Unlock()
		return
	}
	p.started = true
	p.mu.Unlock()

	if streamBus, ok := p.supervisor.messageBus.(EnvelopeDeliveryStream); ok {
		deliveries, err := streamBus.ConsumeEnvelopeDeliveries(ctx, AgentTopic(p.agentID), p.concurrency)
		if err == nil {
			for workerID := 0; workerID < p.concurrency; workerID++ {
				go p.runDeliveryWorker(ctx, workerID, deliveries)
			}
			return
		}
	}

	for workerID := 0; workerID < p.concurrency; workerID++ {
		go p.runWorker(ctx, workerID)
	}
}

func (p *WorkerPool) Snapshot() map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	snapshot := map[string]any{
		"agent_id":            p.metrics.AgentID,
		"concurrency":         p.metrics.Concurrency,
		"active_workers":      p.metrics.ActiveWorkers,
		"processed":           p.metrics.Processed,
		"succeeded":           p.metrics.Succeeded,
		"failed":              p.metrics.Failed,
		"retried":             p.metrics.Retried,
		"dead_lettered":       p.metrics.DeadLettered,
		"validation_failures": p.metrics.ValidationFailures,
		"idle_polls":          p.metrics.IdlePolls,
		"last_task_id":        p.metrics.LastTaskID,
		"last_error":          p.metrics.LastError,
		"average_latency_ms":  p.metrics.AverageLatencyMillis,
	}
	if p.metrics.LastProcessedAt != nil {
		snapshot["last_processed_at"] = *p.metrics.LastProcessedAt
	}
	return snapshot
}

func (p *WorkerPool) runWorker(ctx context.Context, workerID int) {
	ticker := time.NewTicker(p.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		mailbox := p.supervisor.FetchAgentMailbox(ctx, p.agentID, 1)
		if len(mailbox) == 0 {
			p.recordIdlePoll()
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			continue
		}

		for _, envelope := range mailbox {
			p.processEnvelope(ctx, EnvelopeDelivery{Envelope: envelope}, workerID)
		}
	}
}

func (p *WorkerPool) runDeliveryWorker(ctx context.Context, workerID int, deliveries <-chan EnvelopeDelivery) {
	for {
		select {
		case <-ctx.Done():
			return
		case delivery, ok := <-deliveries:
			if !ok {
				return
			}
			p.processEnvelope(ctx, delivery, workerID)
		}
	}
}

func (p *WorkerPool) processEnvelope(ctx context.Context, delivery EnvelopeDelivery, workerID int) {
	envelope := delivery.Envelope
	start := p.nowFn()
	p.recordWorkStarted()
	defer p.recordWorkFinished(start, envelope.TaskID)

	if !p.supervisor.ConfirmPayload(envelope.TaskID, p.agentID, envelope) {
		reason := "payload_validation_failed"
		p.supervisor.Ack(envelope.TaskID, domain.AckStatusFailed, p.agentID, reason)
		p.recordValidationFailure(envelope.TaskID)
		p.handleDeadLetter(ctx, delivery, reason)
		return
	}
	if ack := p.supervisor.EstablishDelivery(envelope.TaskID, p.agentID); ack.AckStatus == domain.AckStatusFailed {
		reason := firstNonEmptyString(ack.Reason, "delivery_handshake_failed")
		p.recordFailure(envelope.TaskID, reason)
		p.retryOrDeadLetter(ctx, delivery, reason)
		return
	}
	if err := p.handler(ctx, envelope); err != nil {
		reason := fmt.Sprintf("worker_%d: %v", workerID, err)
		p.recordFailure(envelope.TaskID, reason)
		p.retryOrDeadLetter(ctx, delivery, reason)
		return
	}
	p.supervisor.Ack(envelope.TaskID, domain.AckStatusAccepted, p.agentID, "worker_completed")
	if delivery.Ack != nil {
		_ = delivery.Ack()
	}
	p.recordSuccess(envelope.TaskID)
}

func (p *WorkerPool) retryOrDeadLetter(ctx context.Context, delivery EnvelopeDelivery, reason string) {
	envelope := delivery.Envelope
	if broker, ok := p.supervisor.messageBus.(BrokerManagedBus); ok && broker.BrokerManaged() {
		p.supervisor.mu.Lock()
		record, tracked := p.supervisor.records[envelope.TaskID]
		if !tracked {
			p.supervisor.mu.Unlock()
			if delivery.Nack != nil {
				_ = delivery.Nack(true)
			}
			return
		}
		if record.RetryCount < maxRetries(record.Envelope) {
			record.RetryCount++
			record.LastReason = reason
			record.LastProgressAt = p.nowFn()
			record.HandshakeState = "retrying"
			p.supervisor.mu.Unlock()
			p.supervisor.Ack(envelope.TaskID, domain.AckStatusRetrying, p.agentID, reason)
			if delivery.Nack != nil {
				_ = delivery.Nack(true)
			}
			p.recordRetried(envelope.TaskID, reason)
			return
		}
		record.LastReason = reason
		record.LastProgressAt = p.nowFn()
		record.HandshakeState = "dead_letter"
		p.supervisor.mu.Unlock()
		p.handleDeadLetter(ctx, delivery, reason)
		return
	}

	p.supervisor.mu.Lock()
	record, ok := p.supervisor.records[envelope.TaskID]
	if !ok {
		p.supervisor.mu.Unlock()
		return
	}
	if record.RetryCount < maxRetries(record.Envelope) {
		record.Envelope.RetryCount++
		record.RetryCount = record.Envelope.RetryCount
		record.LastReason = reason
		record.LastProgressAt = p.nowFn()
		record.HandshakeState = "retrying"
		retryEnvelope := record.Envelope
		p.supervisor.mu.Unlock()
		p.supervisor.Ack(envelope.TaskID, domain.AckStatusRetrying, p.agentID, reason)
		p.supervisor.messageBus.SendEnvelope(retryEnvelope)
		p.recordRetried(envelope.TaskID, reason)
		return
	}
	record.LastReason = reason
	record.LastProgressAt = p.nowFn()
	record.HandshakeState = "dead_letter"
	deadEnvelope := record.Envelope
	p.supervisor.mu.Unlock()
	p.supervisor.messageBus.MarkDeadLetterEnvelope(deadEnvelope, reason)
	p.recordDeadLettered(envelope.TaskID, reason)
	if p.deadLetterHandler != nil {
		_ = p.deadLetterHandler(ctx, deadEnvelope, reason)
	}
}

func (p *WorkerPool) handleDeadLetter(ctx context.Context, delivery EnvelopeDelivery, reason string) {
	envelope := delivery.Envelope
	p.supervisor.Ack(envelope.TaskID, domain.AckStatusDeadLettered, p.agentID, reason)
	if p.deadLetterHandler != nil {
		_ = p.deadLetterHandler(ctx, envelope, reason)
	}
	if delivery.Nack != nil {
		_ = delivery.Nack(false)
	}
	p.recordDeadLettered(envelope.TaskID, reason)
}

func (p *WorkerPool) recordWorkStarted() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.ActiveWorkers++
	p.metrics.Processed++
}

func (p *WorkerPool) recordWorkFinished(start time.Time, taskID string) {
	finished := p.nowFn()
	latency := finished.Sub(start).Seconds() * 1000
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.metrics.ActiveWorkers > 0 {
		p.metrics.ActiveWorkers--
	}
	p.metrics.LastTaskID = taskID
	p.metrics.LastProcessedAt = &finished
	processed := float64(p.metrics.Processed)
	if processed == 1 {
		p.metrics.AverageLatencyMillis = latency
		return
	}
	p.metrics.AverageLatencyMillis = ((p.metrics.AverageLatencyMillis * (processed - 1)) + latency) / processed
}

func (p *WorkerPool) recordIdlePoll() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.IdlePolls++
}

func (p *WorkerPool) recordSuccess(taskID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.Succeeded++
	p.metrics.LastTaskID = taskID
	p.metrics.LastError = ""
}

func (p *WorkerPool) recordFailure(taskID, reason string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.Failed++
	p.metrics.LastTaskID = taskID
	p.metrics.LastError = reason
}

func (p *WorkerPool) recordRetried(taskID, reason string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.Retried++
	p.metrics.LastTaskID = taskID
	p.metrics.LastError = reason
}

func (p *WorkerPool) recordDeadLettered(taskID, reason string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.DeadLettered++
	p.metrics.LastTaskID = taskID
	p.metrics.LastError = reason
}

func (p *WorkerPool) recordValidationFailure(taskID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.metrics.ValidationFailures++
	p.metrics.LastTaskID = taskID
	p.metrics.LastError = "payload_validation_failed"
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
