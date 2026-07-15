package api

import (
	"context"
	"net/http"
	"strings"
)

type requestContextKey struct{}

type requestMetadata struct {
	Transport     string `json:"transport"`
	RequestOrigin string `json:"request_origin"`
	ClientKind    string `json:"client_kind"`
	AnsweredFor   string `json:"answered_for"`
	Principal     string `json:"principal,omitempty"`
}

func (s *Server) withRequestContext(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		meta := metadataFromRequest(r)
		ctx := context.WithValue(r.Context(), requestContextKey{}, meta)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func metadataFromContext(ctx context.Context) requestMetadata {
	if ctx == nil {
		return requestMetadata{}
	}
	meta, _ := ctx.Value(requestContextKey{}).(requestMetadata)
	return meta
}

func metadataFromRequest(r *http.Request) requestMetadata {
	transport, origin, clientKind, answeredFor := inferRequestMetadata(r)
	principal := strings.TrimSpace(r.Header.Get("X-Principal"))
	if principal == "" {
		principal = strings.TrimSpace(r.Header.Get("X-User"))
	}
	return requestMetadata{
		Transport:     transport,
		RequestOrigin: origin,
		ClientKind:    clientKind,
		AnsweredFor:   answeredFor,
		Principal:     principal,
	}
}

func inferRequestMetadata(r *http.Request) (string, string, string, string) {
	transport := strings.TrimSpace(r.Header.Get("X-Transport"))
	origin := strings.TrimSpace(r.Header.Get("X-Request-Origin"))
	clientKind := strings.TrimSpace(r.Header.Get("X-Client-Kind"))
	answeredFor := strings.TrimSpace(r.Header.Get("X-Answered-For"))

	switch {
	case strings.HasPrefix(r.URL.Path, "/chat/ws"):
		transport = valueOrDefault(transport, "websocket")
		origin = valueOrDefault(origin, "chat_ws")
		clientKind = valueOrDefault(clientKind, "external_chat")
		answeredFor = valueOrDefault(answeredFor, "user")
	case strings.HasPrefix(r.URL.Path, "/control/ws"):
		transport = valueOrDefault(transport, "websocket")
		origin = valueOrDefault(origin, "control_ws")
		clientKind = valueOrDefault(clientKind, "control_client")
		answeredFor = valueOrDefault(answeredFor, "operator")
	case strings.HasPrefix(r.URL.Path, "/ws/runtime/events"):
		transport = valueOrDefault(transport, "websocket")
		origin = valueOrDefault(origin, "runtime_ws")
		clientKind = valueOrDefault(clientKind, "runtime_stream")
		answeredFor = valueOrDefault(answeredFor, "observer")
	case strings.HasPrefix(r.URL.Path, "/ws/providers/inventory"):
		transport = valueOrDefault(transport, "websocket")
		origin = valueOrDefault(origin, "inventory_ws")
		clientKind = valueOrDefault(clientKind, "inventory_client")
		answeredFor = valueOrDefault(answeredFor, "observer")
	case strings.HasPrefix(r.URL.Path, "/events/"):
		transport = valueOrDefault(transport, "sse")
		origin = valueOrDefault(origin, "runtime_events")
		clientKind = valueOrDefault(clientKind, "event_stream")
		answeredFor = valueOrDefault(answeredFor, "observer")
	case strings.HasPrefix(r.URL.Path, "/sourcecraft/delegate"), strings.HasPrefix(r.URL.Path, "/sourcecraft/parallel_delegate"):
		transport = valueOrDefault(transport, "http")
		origin = valueOrDefault(origin, "sourcecraft_http")
		clientKind = valueOrDefault(clientKind, "external_chat")
		answeredFor = valueOrDefault(answeredFor, "user")
	case r.URL.Path == "/health" || r.URL.Path == "/health/full" || r.URL.Path == "/api/health":
		transport = valueOrDefault(transport, "http")
		origin = valueOrDefault(origin, "health_http")
		clientKind = valueOrDefault(clientKind, "http_client")
		answeredFor = valueOrDefault(answeredFor, "observer")
	case strings.HasPrefix(r.URL.Path, "/tasks"):
		transport = valueOrDefault(transport, "http")
		origin = valueOrDefault(origin, "tasks_http")
		clientKind = valueOrDefault(clientKind, "http_client")
		answeredFor = valueOrDefault(answeredFor, "user")
	default:
		transport = valueOrDefault(transport, "http")
		origin = valueOrDefault(origin, "http_api")
		clientKind = valueOrDefault(clientKind, "http_client")
		answeredFor = valueOrDefault(answeredFor, "user")
	}

	return transport, origin, clientKind, answeredFor
}

func addResponseMetadata(ctx context.Context, payload map[string]any) map[string]any {
	meta := metadataFromContext(ctx)
	payload["request_origin"] = meta.RequestOrigin
	payload["client_kind"] = meta.ClientKind
	payload["answered_for"] = meta.AnsweredFor
	if meta.Transport != "" {
		payload["transport"] = meta.Transport
	}
	if meta.Principal != "" {
		payload["principal"] = meta.Principal
	}
	return payload
}

func valueOrDefault(value, fallback string) string {
	if strings.TrimSpace(value) != "" {
		return value
	}
	return fallback
}
