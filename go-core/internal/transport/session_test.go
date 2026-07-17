package transport

import (
	"context"
	"net/http"
	"testing"
	"time"
)

func TestNegotiateSubprotocolFromHeadersParsesCommaSeparatedValues(t *testing.T) {
	headers := http.Header{}
	headers.Set("Sec-WebSocket-Protocol", "chat.legacy, chat.json, chat.v1")

	got, err := NegotiateSubprotocolFromHeaders(headers, []string{"chat.v1", "chat.json"})
	if err != nil {
		t.Fatalf("NegotiateSubprotocolFromHeaders() error = %v", err)
	}
	if got != "chat.json" {
		t.Fatalf("NegotiateSubprotocolFromHeaders() = %q, want chat.json", got)
	}
}

func TestSessionRegisterSubscriptionClonesFilterAndCloseCancelsRequests(t *testing.T) {
	ctx := context.Background()
	disconnected := make(chan string, 1)
	triggered := make(chan string, 1)
	session := NewSession(nil, 0, 0, nil, nil, func(_ context.Context, sessionID string) {
		disconnected <- sessionID
	})

	filter := map[string]string{"branch": "main"}
	binding, err := session.RegisterSubscription(ctx, SubscriptionBinding{Topic: "events", Filter: filter})
	if err != nil {
		t.Fatalf("RegisterSubscription() error = %v", err)
	}
	if binding.ID != "events:1" {
		t.Fatalf("RegisterSubscription() id = %q, want events:1", binding.ID)
	}

	filter["branch"] = "mutated"
	subscriptions := session.Subscriptions()
	if len(subscriptions) != 1 {
		t.Fatalf("Subscriptions() len = %d, want 1", len(subscriptions))
	}
	if subscriptions[0].Filter["branch"] != "main" {
		t.Fatalf("Subscriptions() filter branch = %q, want main", subscriptions[0].Filter["branch"])
	}

	if err := session.TrackRequest("req-1", func() { triggered <- "req-1" }); err != nil {
		t.Fatalf("TrackRequest() error = %v", err)
	}
	session.Close(ctx)

	select {
	case got := <-triggered:
		if got != "req-1" {
			t.Fatalf("Close() canceled request %q, want req-1", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Close() did not cancel tracked request")
	}

	select {
	case got := <-disconnected:
		if got != session.SessionID() {
			t.Fatalf("Close() disconnect session = %q, want %q", got, session.SessionID())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Close() did not trigger disconnect hook")
	}

	if len(session.ActiveRequestIDs()) != 0 {
		t.Fatalf("ActiveRequestIDs() len = %d, want 0", len(session.ActiveRequestIDs()))
	}
	if len(session.Subscriptions()) != 0 {
		t.Fatalf("Subscriptions() len = %d after Close(), want 0", len(session.Subscriptions()))
	}
}

func TestSessionHandleControlFrameCancelAndUnsubscribe(t *testing.T) {
	ctx := context.Background()
	canceled := make(chan string, 2)
	session := NewSession(nil, 0, 0, nil, nil, nil)

	if err := session.TrackRequest("req-1", func() { canceled <- "req-1" }); err != nil {
		t.Fatalf("TrackRequest(req-1) error = %v", err)
	}
	if err := session.TrackRequest("sub-1", func() { canceled <- "sub-1" }); err != nil {
		t.Fatalf("TrackRequest(sub-1) error = %v", err)
	}
	if _, err := session.RegisterSubscription(ctx, SubscriptionBinding{ID: "sub-1", Topic: "events"}); err != nil {
		t.Fatalf("RegisterSubscription() error = %v", err)
	}

	env, handled, err := session.HandleControlFrame(ctx, Envelope{
		Action:    "cancel",
		RequestID: "control-1",
		Data:      map[string]any{"target_request_id": "req-1"},
	})
	if err != nil {
		t.Fatalf("HandleControlFrame(cancel) error = %v", err)
	}
	if !handled {
		t.Fatal("HandleControlFrame(cancel) handled = false, want true")
	}
	if env.Type != "control.ack" || env.Action != "cancel" {
		t.Fatalf("HandleControlFrame(cancel) envelope = %#v", env)
	}
	if got := env.Data["canceled"]; got != true {
		t.Fatalf("HandleControlFrame(cancel) canceled = %v, want true", got)
	}

	select {
	case got := <-canceled:
		if got != "req-1" {
			t.Fatalf("cancel request = %q, want req-1", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancel control frame did not invoke request cancel func")
	}

	env, handled, err = session.HandleControlFrame(ctx, Envelope{
		Action:    "unsubscribe",
		RequestID: "control-2",
		Data:      map[string]any{"subscription_id": "sub-1"},
	})
	if err != nil {
		t.Fatalf("HandleControlFrame(unsubscribe) error = %v", err)
	}
	if !handled {
		t.Fatal("HandleControlFrame(unsubscribe) handled = false, want true")
	}
	if got := env.Data["removed"]; got != true {
		t.Fatalf("HandleControlFrame(unsubscribe) removed = %v, want true", got)
	}

	select {
	case got := <-canceled:
		if got != "sub-1" {
			t.Fatalf("unsubscribe canceled request = %q, want sub-1", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("unsubscribe control frame did not cancel subscription request")
	}

	if len(session.Subscriptions()) != 0 {
		t.Fatalf("Subscriptions() len after unsubscribe = %d, want 0", len(session.Subscriptions()))
	}
}
