package transport

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

var requestFrameTypes = map[string]struct{}{
	"command": {}, "subscribe": {}, "unsubscribe": {}, "cancel": {},
}

var serverFrameTypes = map[string]struct{}{
	"ack": {}, "event": {}, "response": {}, "error": {}, "snapshot": {},
	"delta": {}, "heartbeat": {}, "pong": {}, "control.ack": {},
}

type ErrorDefinition struct {
	Retryable bool
	Category  string
	Message   string
}

var ErrorTaxonomy = map[string]ErrorDefinition{
	"INVALID_JSON":            {Category: "protocol", Message: "frame payload is not valid JSON"},
	"INVALID_FRAME":           {Category: "protocol", Message: "frame payload must be a JSON object"},
	"INVALID_TYPE":            {Category: "protocol", Message: "frame type is not supported"},
	"INVALID_ACTION":          {Category: "protocol", Message: "request frames require action"},
	"INVALID_REQUEST_ID":      {Category: "protocol", Message: "request_id must be a non-empty string"},
	"INVALID_TIMEOUT":         {Category: "protocol", Message: "timeout_ms must be a positive integer"},
	"INVALID_IDEMPOTENCY_KEY": {Category: "protocol", Message: "idempotency_key must be a non-empty string"},
	"BAD_REQUEST":             {Category: "client", Message: "request payload is semantically invalid"},
	"UNAUTHORIZED":            {Category: "auth", Message: "authentication is required or invalid"},
	"FORBIDDEN":               {Category: "auth", Message: "caller is not allowed to perform this action"},
	"NOT_FOUND":               {Category: "routing", Message: "resource was not found"},
	"CONFLICT":                {Category: "mutation", Message: "request conflicts with current state"},
	"UNSUPPORTED_ACTION":      {Category: "routing", Message: "action is not registered in the dispatcher"},
	"TIMEOUT":                 {Retryable: true, Category: "mutation", Message: "request timed out"},
	"CANCELED":                {Retryable: true, Category: "mutation", Message: "request was canceled"},
	"RATE_LIMITED":            {Retryable: true, Category: "provider", Message: "request was rate limited"},
	"UNAVAILABLE":             {Retryable: true, Category: "provider", Message: "service is unavailable"},
	"INTERNAL_ERROR":          {Retryable: true, Category: "server", Message: "request handling failed"},
}

type ProtocolError struct {
	Code      string         `json:"code"`
	Message   string         `json:"message"`
	Retryable bool           `json:"retryable"`
	Category  string         `json:"category,omitempty"`
	Details   map[string]any `json:"details,omitempty"`
}

func (e *ProtocolError) Error() string {
	if e == nil {
		return "protocol error"
	}
	return e.Message
}

func NewProtocolFrameError(code, message string, details map[string]any) *ProtocolError {
	code = strings.ToUpper(strings.TrimSpace(code))
	definition, ok := ErrorTaxonomy[code]
	if !ok {
		code = "INTERNAL_ERROR"
		definition = ErrorTaxonomy[code]
	}
	if strings.TrimSpace(message) == "" {
		message = definition.Message
	}
	return &ProtocolError{Code: code, Message: message, Retryable: definition.Retryable, Category: definition.Category, Details: cloneMap(details)}
}

func ParseEnvelope(raw []byte) (Envelope, error) {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return Envelope{}, NewProtocolFrameError("INVALID_JSON", "", nil)
	}
	frame, ok := decoded.(map[string]any)
	if !ok {
		return Envelope{}, NewProtocolFrameError("INVALID_FRAME", "", nil)
	}
	if rawTimeout, exists := frame["timeout_ms"]; exists && rawTimeout != nil {
		timeout, valid := rawTimeout.(float64)
		if !valid || timeout <= 0 || timeout != float64(int(timeout)) {
			return Envelope{}, NewProtocolFrameError("INVALID_TIMEOUT", "", nil)
		}
	}
	if rawKey, exists := frame["idempotency_key"]; exists && rawKey != nil {
		key, valid := rawKey.(string)
		if !valid || strings.TrimSpace(key) == "" {
			return Envelope{}, NewProtocolFrameError("INVALID_IDEMPOTENCY_KEY", "", nil)
		}
	}
	if rawData, exists := frame["data"]; exists && rawData != nil {
		if _, valid := rawData.(map[string]any); !valid {
			return Envelope{}, NewProtocolFrameError("INVALID_FRAME", "data must be a JSON object", nil)
		}
	}
	encoded, err := json.Marshal(frame)
	if err != nil {
		return Envelope{}, NewProtocolFrameError("INVALID_FRAME", err.Error(), nil)
	}
	var envelope Envelope
	if err := json.Unmarshal(encoded, &envelope); err != nil {
		return Envelope{}, NewProtocolFrameError("INVALID_FRAME", err.Error(), nil)
	}
	envelope.Type = strings.ToLower(strings.TrimSpace(envelope.Type))
	envelope.Action = strings.TrimSpace(envelope.Action)
	if err := ValidateEnvelope(envelope); err != nil {
		return Envelope{}, err
	}
	return envelope, nil
}

func ValidateEnvelope(envelope Envelope) error {
	typ := strings.ToLower(strings.TrimSpace(envelope.Type))
	if _, request := requestFrameTypes[typ]; !request {
		if _, server := serverFrameTypes[typ]; !server {
			return NewProtocolFrameError("INVALID_TYPE", fmt.Sprintf("unsupported frame type: %s", typ), nil)
		}
	}
	if strings.TrimSpace(envelope.RequestID) == "" && typ != "heartbeat" {
		return NewProtocolFrameError("INVALID_REQUEST_ID", "", nil)
	}
	if _, request := requestFrameTypes[typ]; request && strings.TrimSpace(envelope.Action) == "" {
		return NewProtocolFrameError("INVALID_ACTION", "", nil)
	}
	if envelope.TimeoutMS < 0 {
		return NewProtocolFrameError("INVALID_TIMEOUT", "", nil)
	}
	return nil
}

func AckEnvelope(request Envelope, mode string) Envelope {
	return Envelope{Type: "ack", RequestID: request.RequestID, CorrelationID: request.CorrelationID, Action: request.Action, Ack: true, Data: map[string]any{
		"accepted": true, "mode": mode, "idempotency_key": request.IdempotencyKey, "timeout_ms": request.TimeoutMS,
	}}
}

func ResponseEnvelope(request Envelope, data map[string]any) Envelope {
	return Envelope{Type: "response", RequestID: request.RequestID, CorrelationID: request.CorrelationID, Action: request.Action, Data: cloneMap(data), Final: true}
}

func EventEnvelope(request Envelope, frameType string, data map[string]any, final bool) Envelope {
	if frameType != "snapshot" && frameType != "delta" {
		frameType = "event"
	}
	return Envelope{Type: frameType, RequestID: request.RequestID, CorrelationID: request.CorrelationID, Action: request.Action, Data: cloneMap(data), Final: final}
}

func ErrorEnvelope(request Envelope, code, message string, details map[string]any) Envelope {
	return Envelope{Type: "error", RequestID: request.RequestID, CorrelationID: request.CorrelationID, Action: request.Action, Error: NewProtocolFrameError(code, message, details), Final: true}
}


func ErrorCode(err error) string {
	var protocolErr *ProtocolError
	if errors.As(err, &protocolErr) {
		return protocolErr.Code
	}
	return "INTERNAL_ERROR"
}
