package domain

import "time"

type AckStatus string

const (
	AckStatusSent     AckStatus = "sent"
	AckStatusReceived AckStatus = "received"
	AckStatusAccepted AckStatus = "accepted"
	AckStatusFailed   AckStatus = "failed"
)

type TaskPayload struct {
	Objective            string         `json:"objective,omitempty"`
	InputData            map[string]any `json:"input_data,omitempty"`
	Context              map[string]any `json:"context,omitempty"`
	AcceptanceCriteria   []string       `json:"acceptance_criteria,omitempty"`
	ExpectedOutputFormat string         `json:"expected_output_format,omitempty"`
	Artifacts            []string       `json:"artifacts,omitempty"`
}

type TaskEnvelope struct {
	ProtocolVersion  string      `json:"protocol_version,omitempty"`
	TaskID           string      `json:"task_id"`
	ParentTaskID     string      `json:"parent_task_id,omitempty"`
	TraceID          string      `json:"trace_id,omitempty"`
	CorrelationID    string      `json:"correlation_id,omitempty"`
	SourceAgent      string      `json:"source_agent,omitempty"`
	TargetAgent      string      `json:"target_agent,omitempty"`
	TargetCapability string      `json:"target_capability,omitempty"`
	Priority         string      `json:"priority,omitempty"`
	QOSClass         string      `json:"qos_class,omitempty"`
	TTL              int         `json:"ttl,omitempty"`
	Deadline         *time.Time  `json:"deadline,omitempty"`
	HopCount         int         `json:"hop_count,omitempty"`
	MaxHops          int         `json:"max_hops,omitempty"`
	RetryCount       int         `json:"retry_count,omitempty"`
	MaxRetries       int         `json:"max_retries,omitempty"`
	SecurityPolicy   string      `json:"security_policy,omitempty"`
	ContextScope     string      `json:"context_scope,omitempty"`
	Dependencies     []string    `json:"dependencies,omitempty"`
	Payload          TaskPayload `json:"payload"`
	IsDeadLetter     bool        `json:"is_dead_letter,omitempty"`
	CreatedAt        time.Time   `json:"created_at,omitempty"`
}

type MessageAck struct {
	MessageID  string    `json:"message_id"`
	AckStatus  AckStatus `json:"ack_status"`
	ReceivedBy string    `json:"received_by,omitempty"`
	Reason     string    `json:"reason,omitempty"`
}
