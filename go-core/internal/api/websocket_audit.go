package api

import (
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/transport"
)

type websocketAuditRecord struct {
	Timestamp       time.Time      `json:"timestamp"`
	Path            string         `json:"path"`
	RemoteAddr      string         `json:"remote_addr,omitempty"`
	SessionID       string         `json:"session_id,omitempty"`
	Stage           string         `json:"stage"`
	AutomaticAction string         `json:"automatic_action,omitempty"`
	Raw             string         `json:"raw,omitempty"`
	Error           string         `json:"error,omitempty"`
	Envelope        map[string]any `json:"envelope,omitempty"`
}

type websocketAuditLog struct {
	mu      sync.RWMutex
	limit   int
	records []websocketAuditRecord
}

func newWebsocketAuditLog(limit int) *websocketAuditLog {
	if limit <= 0 {
		limit = 100
	}
	return &websocketAuditLog{limit: limit}
}

func (l *websocketAuditLog) add(record websocketAuditRecord) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.records = append(l.records, record)
	if len(l.records) > l.limit {
		l.records = append([]websocketAuditRecord(nil), l.records[len(l.records)-l.limit:]...)
	}
}

func (l *websocketAuditLog) snapshot() []websocketAuditRecord {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return append([]websocketAuditRecord(nil), l.records...)
}

func (l *websocketAuditLog) capacity() int {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.limit
}

func (s *Server) recordWebsocketAudit(path string, remoteAddr string, sessionID string, automaticAction string, raw []byte, envelope *transport.Envelope, err error, stage string) {
	record := websocketAuditRecord{
		Timestamp:       time.Now().UTC(),
		Path:            path,
		RemoteAddr:      remoteAddr,
		SessionID:       sessionID,
		Stage:           stage,
		AutomaticAction: automaticAction,
		Raw:             string(raw),
	}
	if err != nil {
		record.Error = err.Error()
	}
	if envelope != nil {
		record.Envelope = map[string]any{
			"type":           envelope.Type,
			"request_id":     envelope.RequestID,
			"correlation_id": envelope.CorrelationID,
			"action":         envelope.Action,
			"ack":            envelope.Ack,
			"data":           envelope.Data,
		}
	}
	s.wsAudit.add(record)
}
