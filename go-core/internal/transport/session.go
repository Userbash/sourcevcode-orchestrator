package transport

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

var DefaultWSSubprotocols = []string{"chat.v1", "chat.json"}

type SessionError struct {
	Kind    string
	Message string
}

func (e *SessionError) Error() string {
	if e == nil {
		return "session error"
	}
	return e.Message
}

func NewAuthError(message string) error {
	return &SessionError{Kind: "auth", Message: message}
}

func NewProtocolError(message string) error {
	return &SessionError{Kind: "protocol", Message: message}
}

type AuthHandler func(ctx context.Context, headers http.Header) (string, error)
type SubscriptionHook func(ctx context.Context, binding SubscriptionBinding)
type DisconnectHook func(ctx context.Context, sessionID string)

type Envelope struct {
	Type           string         `json:"type"`
	RequestID      string         `json:"request_id,omitempty"`
	CorrelationID  string         `json:"correlation_id,omitempty"`
	Action         string         `json:"action,omitempty"`
	Data           map[string]any `json:"data,omitempty"`
	Error          any            `json:"error,omitempty"`
	Final          bool           `json:"final,omitempty"`
	Ack            bool           `json:"ack,omitempty"`
	IdempotencyKey string         `json:"idempotency_key,omitempty"`
	TimeoutMS      int            `json:"timeout_ms,omitempty"`
}

type HandshakeResult struct {
	SessionID   string    `json:"session_id"`
	Principal   string    `json:"principal"`
	Subprotocol string    `json:"subprotocol"`
	AcceptedAt  time.Time `json:"accepted_at"`
}

type SubscriptionBinding struct {
	ID        string            `json:"id"`
	Topic     string            `json:"topic"`
	Filter    map[string]string `json:"filter,omitempty"`
	CreatedAt time.Time         `json:"created_at"`
}

type Session struct {
	mu                    sync.RWMutex
	sessionID             string
	supportedSubprotocols []string
	sendTimeout           time.Duration
	heartbeatInterval     time.Duration
	authHandler           AuthHandler
	onSubscribe           SubscriptionHook
	onDisconnect          DisconnectHook

	principal      string
	subprotocol    string
	acceptedAt     time.Time
	accepted       bool
	closed         bool
	requestCancels map[string]context.CancelFunc
	subscriptions  map[string]SubscriptionBinding
}

func NewSession(supportedSubprotocols []string, sendTimeout, heartbeatInterval time.Duration, authHandler AuthHandler, onSubscribe SubscriptionHook, onDisconnect DisconnectHook) *Session {
	subs := append([]string(nil), supportedSubprotocols...)
	if len(subs) == 0 {
		subs = append(subs, DefaultWSSubprotocols...)
	}
	if sendTimeout <= 0 {
		sendTimeout = 5 * time.Second
	}
	if heartbeatInterval <= 0 {
		heartbeatInterval = 30 * time.Second
	}
	if authHandler == nil {
		authHandler = DefaultAuthHandler
	}
	return &Session{
		sessionID:             newSessionID(),
		supportedSubprotocols: subs,
		sendTimeout:           sendTimeout,
		heartbeatInterval:     heartbeatInterval,
		authHandler:           authHandler,
		onSubscribe:           onSubscribe,
		onDisconnect:          onDisconnect,
		requestCancels:        make(map[string]context.CancelFunc),
		subscriptions:         make(map[string]SubscriptionBinding),
	}
}

func DefaultAuthHandler(_ context.Context, headers http.Header) (string, error) {
	principal := strings.TrimSpace(headers.Get("X-Principal"))
	if principal == "" {
		principal = strings.TrimSpace(headers.Get("X-User"))
	}
	if principal == "" {
		principal = "anonymous"
	}
	return principal, nil
}

func NegotiateSubprotocolFromHeaders(headers http.Header, supported []string) (string, error) {
	requested := headers.Values("Sec-WebSocket-Protocol")
	if len(requested) == 0 {
		raw := headers.Get("Sec-WebSocket-Protocol")
		if raw != "" {
			requested = []string{raw}
		}
	}
	var tokens []string
	for _, item := range requested {
		for _, part := range strings.Split(item, ",") {
			if trimmed := strings.TrimSpace(part); trimmed != "" {
				tokens = append(tokens, trimmed)
			}
		}
	}
	return NegotiateSubprotocol(tokens, supported)
}

func NegotiateSubprotocol(requested, supported []string) (string, error) {
	if len(supported) == 0 {
		supported = DefaultWSSubprotocols
	}
	if len(requested) == 0 {
		return supported[0], nil
	}
	supportedSet := make(map[string]struct{}, len(supported))
	for _, item := range supported {
		supportedSet[item] = struct{}{}
	}
	for _, item := range requested {
		if _, ok := supportedSet[item]; ok {
			return item, nil
		}
	}
	return "", NewProtocolError("no compatible websocket subprotocol")
}

func BuildEnvelope(typ, requestID, correlationID, action string, data map[string]any, err error, final bool) Envelope {
	env := Envelope{
		Type:          typ,
		RequestID:     requestID,
		CorrelationID: correlationID,
		Action:        action,
		Data:          cloneMap(data),
		Final:         final,
	}
	if err != nil {
		env.Error = err.Error()
	}
	return env
}

func (s *Session) SessionID() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.sessionID
}

func (s *Session) Principal() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.principal
}

func (s *Session) Subscriptions() []SubscriptionBinding {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return cloneSubscriptions(s.subscriptions)
}

func (s *Session) ActiveRequestIDs() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.requestCancels))
	for requestID := range s.requestCancels {
		ids = append(ids, requestID)
	}
	return ids
}

func (s *Session) NegotiatedSubprotocol() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.subprotocol
}

func (s *Session) Accept(ctx context.Context, headers http.Header) (HandshakeResult, error) {
	principal, err := s.authHandler(ctx, headers)
	if err != nil {
		return HandshakeResult{}, err
	}
	subprotocol, err := NegotiateSubprotocolFromHeaders(headers, s.supportedSubprotocols)
	if err != nil {
		return HandshakeResult{}, err
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return HandshakeResult{}, errors.New("session already closed")
	}
	s.principal = principal
	s.subprotocol = subprotocol
	s.acceptedAt = time.Now().UTC()
	s.accepted = true
	return HandshakeResult{
		SessionID:   s.sessionID,
		Principal:   s.principal,
		Subprotocol: s.subprotocol,
		AcceptedAt:  s.acceptedAt,
	}, nil
}

func (s *Session) TrackRequest(requestID string, cancel context.CancelFunc) error {
	if strings.TrimSpace(requestID) == "" {
		return NewProtocolError("request_id is required")
	}
	if cancel == nil {
		return NewProtocolError("request cancel func is required")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errors.New("session already closed")
	}
	s.requestCancels[requestID] = cancel
	return nil
}

func (s *Session) CancelRequest(requestID string) bool {
	s.mu.Lock()
	cancel, ok := s.requestCancels[requestID]
	if ok {
		delete(s.requestCancels, requestID)
	}
	s.mu.Unlock()
	if ok {
		cancel()
	}
	return ok
}

func (s *Session) FinishRequest(requestID string) {
	s.mu.Lock()
	delete(s.requestCancels, requestID)
	s.mu.Unlock()
}

func (s *Session) CancelAllRequests() int {
	s.mu.Lock()
	cancels := make([]context.CancelFunc, 0, len(s.requestCancels))
	for requestID, cancel := range s.requestCancels {
		cancels = append(cancels, cancel)
		delete(s.requestCancels, requestID)
	}
	s.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
	return len(cancels)
}

func (s *Session) RegisterSubscription(ctx context.Context, binding SubscriptionBinding) (SubscriptionBinding, error) {
	if strings.TrimSpace(binding.Topic) == "" {
		return SubscriptionBinding{}, NewProtocolError("subscription topic is required")
	}
	if strings.TrimSpace(binding.ID) == "" {
		binding.ID = subscriptionIDFromData(binding.Topic, binding.Filter)
	}
	if binding.CreatedAt.IsZero() {
		binding.CreatedAt = time.Now().UTC()
	}
	binding.Filter = cloneStringMap(binding.Filter)

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return SubscriptionBinding{}, errors.New("session already closed")
	}
	s.subscriptions[binding.ID] = binding
	s.mu.Unlock()

	if s.onSubscribe != nil {
		s.onSubscribe(ctx, binding)
	}
	return binding, nil
}

func (s *Session) RemoveSubscription(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.subscriptions[id]; ok {
		delete(s.subscriptions, id)
		return true
	}
	return false
}

func (s *Session) ClearSubscriptions() int {
	s.mu.Lock()
	count := len(s.subscriptions)
	s.subscriptions = make(map[string]SubscriptionBinding)
	s.mu.Unlock()
	return count
}

func (s *Session) HeartbeatEnvelope() Envelope {
	return BuildEnvelope("heartbeat", "", "", "", map[string]any{
		"session_id": s.SessionID(),
		"ts":         time.Now().UTC().Format(time.RFC3339Nano),
	}, nil, false)
}

func (s *Session) HandleControlFrame(ctx context.Context, envelope Envelope) (Envelope, bool, error) {
	switch strings.ToLower(strings.TrimSpace(firstNonEmpty(envelope.Action, envelope.Type))) {
	case "ping":
		return BuildEnvelope("pong", envelope.RequestID, envelope.CorrelationID, "pong", map[string]any{"session_id": s.SessionID()}, nil, false), true, nil
	case "heartbeat":
		return s.HeartbeatEnvelope(), true, nil
	case "cancel":
		requestID := firstNonEmpty(asString(envelope.Data, "request_id"), asString(envelope.Data, "target_request_id"), envelope.RequestID)
		canceled := s.CancelRequest(requestID)
		return BuildEnvelope("control.ack", envelope.RequestID, envelope.CorrelationID, "cancel", map[string]any{"request_id": requestID, "canceled": canceled}, nil, true), true, nil
	case "unsubscribe":
		subscriptionID := firstNonEmpty(asString(envelope.Data, "subscription_id"), asString(envelope.Data, "id"))
		removed := s.RemoveSubscription(subscriptionID)
		if removed {
			s.CancelRequest(subscriptionID)
		}
		return BuildEnvelope("control.ack", envelope.RequestID, envelope.CorrelationID, "unsubscribe", map[string]any{"subscription_id": subscriptionID, "removed": removed}, nil, true), true, nil
	default:
		return Envelope{}, false, nil
	}
}

func (s *Session) HeartbeatInterval() time.Duration {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.heartbeatInterval
}

func (s *Session) SendTimeout() time.Duration {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.sendTimeout
}

func (s *Session) Close(ctx context.Context) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	cancels := make([]context.CancelFunc, 0, len(s.requestCancels))
	for requestID, cancel := range s.requestCancels {
		cancels = append(cancels, cancel)
		delete(s.requestCancels, requestID)
	}
	s.subscriptions = make(map[string]SubscriptionBinding)
	sessionID := s.sessionID
	onDisconnect := s.onDisconnect
	s.mu.Unlock()

	for _, cancel := range cancels {
		cancel()
	}
	if onDisconnect != nil {
		onDisconnect(ctx, sessionID)
	}
}

func cloneMap(data map[string]any) map[string]any {
	if len(data) == 0 {
		return nil
	}
	cloned := make(map[string]any, len(data))
	for key, value := range data {
		cloned[key] = value
	}
	return cloned
}

func cloneStringMap(data map[string]string) map[string]string {
	if len(data) == 0 {
		return nil
	}
	cloned := make(map[string]string, len(data))
	for key, value := range data {
		cloned[key] = value
	}
	return cloned
}

func cloneSubscriptions(bindings map[string]SubscriptionBinding) []SubscriptionBinding {
	if len(bindings) == 0 {
		return nil
	}
	out := make([]SubscriptionBinding, 0, len(bindings))
	for _, binding := range bindings {
		copyBinding := binding
		copyBinding.Filter = cloneStringMap(binding.Filter)
		out = append(out, copyBinding)
	}
	return out
}

func subscriptionIDFromData(topic string, filter map[string]string) string {
	if len(filter) == 0 {
		return topic
	}
	return fmt.Sprintf("%s:%d", topic, len(filter))
}

func asString(data map[string]any, key string) string {
	if len(data) == 0 {
		return ""
	}
	raw, ok := data[key]
	if !ok || raw == nil {
		return ""
	}
	switch value := raw.(type) {
	case string:
		return strings.TrimSpace(value)
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func newSessionID() string {
	buf := make([]byte, 12)
	if _, err := rand.Read(buf); err == nil {
		return "sess_" + hex.EncodeToString(buf)
	}
	return fmt.Sprintf("sess_%d", time.Now().UnixNano())
}
