package delivery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const DeliveryEventsTopic = "delivery.events"

type DeliveryRecord struct {
	Envelope          domain.TaskEnvelope
	Status            domain.AckStatus
	SentAt            time.Time
	LastProgressAt    time.Time
	ReceivedAt        *time.Time
	CompletedAt       *time.Time
	ReceivedBy        string
	LastReason        string
	RetryCount        int
	LastAuditedStatus string
	HandshakeState    string
	PayloadChecksum   string
	PayloadValidated  bool
}

type Supervisor struct {
	mu         sync.Mutex
	messageBus Bus
	records    map[string]*DeliveryRecord
	ackTimeout time.Duration
	nowFn      func() time.Time
}

func NewSupervisor(messageBus Bus, ackTimeout time.Duration) *Supervisor {
	if ackTimeout <= 0 {
		ackTimeout = 30 * time.Second
	}
	if messageBus == nil {
		messageBus = NewMessageBus()
	}
	return &Supervisor{
		messageBus: messageBus,
		records:    map[string]*DeliveryRecord{},
		ackTimeout: ackTimeout,
		nowFn: func() time.Time {
			return time.Now().UTC()
		},
	}
}

func (s *Supervisor) Dispatch(_ context.Context, envelope domain.TaskEnvelope) map[string]any {
	if envelope.CreatedAt.IsZero() {
		envelope.CreatedAt = s.nowFn()
	}
	s.messageBus.SendEnvelope(envelope)

	now := s.nowFn()
	record := &DeliveryRecord{
		Envelope:        envelope,
		Status:          domain.AckStatusSent,
		SentAt:          now,
		LastProgressAt:  now,
		RetryCount:      envelope.RetryCount,
		HandshakeState:  "syn",
		PayloadChecksum: payloadChecksum(envelope),
	}
	s.mu.Lock()
	s.records[envelope.TaskID] = record
	s.mu.Unlock()
	s.audit("delivery.sent", record, nil)
	return s.Refresh(context.Background(), envelope.TaskID)
}

func (s *Supervisor) Refresh(_ context.Context, taskID string) map[string]any {
	s.mu.Lock()
	record, ok := s.records[taskID]
	s.mu.Unlock()
	if !ok {
		return map[string]any{}
	}
	history := s.messageBus.AckHistory(taskID)
	s.mu.Lock()
	s.applyHistory(record, history)
	snapshot := s.snapshotLocked(record, history)
	s.mu.Unlock()
	return snapshot
}

func (s *Supervisor) Snapshot(taskID string) map[string]any {
	s.mu.Lock()
	record, ok := s.records[taskID]
	s.mu.Unlock()
	if !ok {
		return map[string]any{}
	}
	history := s.messageBus.AckHistory(taskID)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.applyHistory(record, history)
	return s.snapshotLocked(record, history)
}

func (s *Supervisor) FetchAgentMailbox(_ context.Context, agentID string, limit int) []domain.TaskEnvelope {
	if limit <= 0 {
		limit = 1
	}
	items := make([]domain.TaskEnvelope, 0, limit)
	for len(items) < limit {
		envelope, ok := s.messageBus.ReceiveForAgent(agentID)
		if !ok {
			break
		}
		now := s.nowFn()
		history := s.messageBus.AckHistory(envelope.TaskID)
		s.mu.Lock()
		record, ok := s.records[envelope.TaskID]
		if ok {
			record.ReceivedBy = agentID
			record.ReceivedAt = &now
			record.LastProgressAt = now
			record.HandshakeState = "syn_ack"
			s.applyHistory(record, history)
		}
		s.mu.Unlock()
		items = append(items, envelope)
	}
	return items
}

func (s *Supervisor) ConfirmPayload(taskID, agentID string, envelope domain.TaskEnvelope) bool {
	s.mu.Lock()
	record, ok := s.records[taskID]
	if !ok {
		s.mu.Unlock()
		return false
	}
	checksum := payloadChecksum(envelope)
	if checksum != record.PayloadChecksum {
		record.PayloadValidated = false
		record.HandshakeState = "invalid"
		record.LastReason = "payload_checksum_mismatch"
		snapshot := s.snapshotLocked(record, s.messageBus.AckHistory(taskID))
		s.mu.Unlock()
		s.audit("delivery.invalid_payload", record, snapshot)
		return false
	}
	record.PayloadValidated = true
	record.HandshakeState = "ack_valid"
	snapshot := s.snapshotLocked(record, s.messageBus.AckHistory(taskID))
	s.mu.Unlock()
	s.audit("delivery.payload_validated", record, snapshot)
	_ = agentID
	return true
}

func (s *Supervisor) Ack(taskID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	ack := s.messageBus.Ack(taskID, status, receivedBy, reason)
	now := s.nowFn()
	s.mu.Lock()
	record, ok := s.records[taskID]
	if ok {
		record.Status = status
		record.ReceivedBy = receivedBy
		record.LastReason = reason
		record.LastProgressAt = now
		if status == domain.AckStatusReceived {
			if record.ReceivedAt == nil {
				received := now
				record.ReceivedAt = &received
			}
			if record.HandshakeState == "syn" {
				record.HandshakeState = "syn_ack"
			}
		}
		if status == domain.AckStatusAccepted || status == domain.AckStatusFailed {
			completed := now
			record.CompletedAt = &completed
		}
	}
	s.mu.Unlock()
	if ok {
		s.audit("delivery.ack", record, s.Snapshot(taskID))
	}
	return ack
}

func (s *Supervisor) EstablishDelivery(taskID, agentID string) domain.MessageAck {
	s.mu.Lock()
	record, ok := s.records[taskID]
	s.mu.Unlock()
	if !ok {
		return domain.MessageAck{}
	}
	if !record.PayloadValidated {
		ack := s.messageBus.Ack(taskID, domain.AckStatusFailed, agentID, "payload_not_validated")
		s.mu.Lock()
		now := s.nowFn()
		record.Status = domain.AckStatusFailed
		record.LastReason = "payload_not_validated"
		record.HandshakeState = "invalid"
		record.CompletedAt = &now
		record.LastProgressAt = now
		s.mu.Unlock()
		s.Refresh(context.Background(), taskID)
		return ack
	}
	ack := s.messageBus.Ack(taskID, domain.AckStatusReceived, agentID, "")
	s.mu.Lock()
	record.HandshakeState = "established"
	record.LastProgressAt = s.nowFn()
	snapshot := s.snapshotLocked(record, s.messageBus.AckHistory(taskID))
	s.mu.Unlock()
	s.audit("delivery.established", record, snapshot)
	s.Refresh(context.Background(), taskID)
	return ack
}

func (s *Supervisor) MailboxSnapshot(agentID string) map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	tracked := make([]string, 0)
	for taskID, record := range s.records {
		if record.Envelope.TargetAgent != agentID {
			continue
		}
		if record.Status == domain.AckStatusAccepted || record.Status == domain.AckStatusFailed {
			continue
		}
		tracked = append(tracked, taskID)
	}
	return map[string]any{
		"agent_id":         agentID,
		"queue_depth":      s.messageBus.Depth(AgentTopic(agentID)),
		"tracked_task_ids": tracked,
	}
}

func (s *Supervisor) InspectTimeouts(_ context.Context) map[string]any {
	retried := 0
	deadLettered := 0
	now := s.nowFn()
	s.mu.Lock()
	records := make([]*DeliveryRecord, 0, len(s.records))
	for _, record := range s.records {
		records = append(records, record)
	}
	s.mu.Unlock()

	for _, record := range records {
		if record.Status == domain.AckStatusAccepted || record.Status == domain.AckStatusFailed {
			continue
		}
		if now.Sub(record.LastProgressAt) < s.ackTimeout {
			continue
		}
		if record.RetryCount < maxRetries(record.Envelope) {
			record.Envelope.RetryCount++
			record.RetryCount = record.Envelope.RetryCount
			record.LastProgressAt = now
			s.messageBus.SendEnvelope(record.Envelope)
			s.audit("delivery.retry", record, s.Snapshot(record.Envelope.TaskID))
			retried++
			continue
		}
		s.messageBus.MarkDeadLetterEnvelope(record.Envelope, "ack_timeout")
		s.mu.Lock()
		record.Status = domain.AckStatusFailed
		record.LastReason = "ack_timeout"
		record.HandshakeState = "dead_letter"
		completed := now
		record.CompletedAt = &completed
		record.LastProgressAt = now
		s.mu.Unlock()
		s.audit("delivery.dead_letter", record, s.Snapshot(record.Envelope.TaskID))
		deadLettered++
	}
	return map[string]any{
		"retried":       retried,
		"dead_lettered": deadLettered,
	}
}

func (s *Supervisor) DeliveryHealthSnapshot() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	byAgent := map[string]map[string]any{}
	tracked := 0
	pending := 0
	accepted := 0
	failed := 0
	maxLag := 0.0
	now := s.nowFn()
	for _, record := range s.records {
		tracked++
		switch record.Status {
		case domain.AckStatusAccepted:
			accepted++
		case domain.AckStatusFailed:
			failed++
		default:
			pending++
			lag := now.Sub(record.LastProgressAt).Seconds()
			if lag > maxLag {
				maxLag = lag
			}
		}
		agentID := record.Envelope.TargetAgent
		if _, ok := byAgent[agentID]; !ok {
			byAgent[agentID] = map[string]any{
				"queue_depth": s.messageBus.Depth(AgentTopic(agentID)),
				"pending":     0,
				"accepted":    0,
				"failed":      0,
			}
		}
		metrics := byAgent[agentID]
		switch record.Status {
		case domain.AckStatusAccepted:
			metrics["accepted"] = metrics["accepted"].(int) + 1
		case domain.AckStatusFailed:
			metrics["failed"] = metrics["failed"].(int) + 1
		default:
			metrics["pending"] = metrics["pending"].(int) + 1
		}
	}
	return map[string]any{
		"tracked":     tracked,
		"pending":     pending,
		"accepted":    accepted,
		"failed":      failed,
		"max_lag_sec": maxLag,
		"by_agent":    byAgent,
	}
}

func (s *Supervisor) RecordsSnapshot() map[string]any {
	s.mu.Lock()
	ids := make([]string, 0, len(s.records))
	for taskID := range s.records {
		ids = append(ids, taskID)
	}
	s.mu.Unlock()
	items := make(map[string]any, len(ids))
	for _, taskID := range ids {
		items[taskID] = s.Snapshot(taskID)
	}
	return items
}

func (s *Supervisor) applyHistory(record *DeliveryRecord, history []domain.MessageAck) {
	for _, ack := range history {
		now := s.nowFn()
		switch ack.AckStatus {
		case domain.AckStatusSent:
			record.Status = domain.AckStatusSent
		case domain.AckStatusReceived:
			record.Status = domain.AckStatusReceived
			record.ReceivedBy = ack.ReceivedBy
			record.LastProgressAt = now
			if record.ReceivedAt == nil {
				received := now
				record.ReceivedAt = &received
			}
			if record.HandshakeState == "syn" {
				record.HandshakeState = "syn_ack"
			}
		case domain.AckStatusAccepted:
			record.Status = domain.AckStatusAccepted
			record.ReceivedBy = ack.ReceivedBy
			record.LastReason = ack.Reason
			record.LastProgressAt = now
			if record.CompletedAt == nil {
				completed := now
				record.CompletedAt = &completed
			}
			if record.PayloadValidated {
				record.HandshakeState = "established"
			}
		case domain.AckStatusFailed:
			record.Status = domain.AckStatusFailed
			record.ReceivedBy = ack.ReceivedBy
			record.LastReason = ack.Reason
			record.LastProgressAt = now
			if record.CompletedAt == nil {
				completed := now
				record.CompletedAt = &completed
			}
		}
	}
}

func (s *Supervisor) snapshotLocked(record *DeliveryRecord, history []domain.MessageAck) map[string]any {
	ackHistory := make([]string, 0, len(history))
	for _, ack := range history {
		ackHistory = append(ackHistory, string(ack.AckStatus))
	}
	snapshot := map[string]any{
		"task_id":           record.Envelope.TaskID,
		"target_agent":      record.Envelope.TargetAgent,
		"status":            string(record.Status),
		"retry_count":       record.RetryCount,
		"max_retries":       maxRetries(record.Envelope),
		"received_by":       record.ReceivedBy,
		"last_reason":       record.LastReason,
		"sent_at":           record.SentAt,
		"ack_history":       ackHistory,
		"queue_depth":       s.messageBus.Depth(AgentTopic(record.Envelope.TargetAgent)),
		"handshake_state":   record.HandshakeState,
		"payload_validated": record.PayloadValidated,
		"payload_checksum":  record.PayloadChecksum,
	}
	if record.ReceivedAt != nil {
		snapshot["received_at"] = *record.ReceivedAt
	}
	if record.CompletedAt != nil {
		snapshot["completed_at"] = *record.CompletedAt
	}
	return snapshot
}

func (s *Supervisor) audit(kind string, record *DeliveryRecord, snapshot map[string]any) {
	if record == nil {
		return
	}
	_ = kind
	_ = snapshot
	// Delivery events are already exposed through live supervisor snapshots.
}

func payloadChecksum(envelope domain.TaskEnvelope) string {
	payload, _ := json.Marshal(envelope.Payload)
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func maxRetries(envelope domain.TaskEnvelope) int {
	if envelope.MaxRetries <= 0 {
		return 3
	}
	return envelope.MaxRetries
}
