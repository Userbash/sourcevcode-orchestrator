package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"
	"time"
)

type requestContextKey struct{}

type requestMetadata struct {
	RequestID     string `json:"request_id"`
	Transport     string `json:"transport"`
	RequestOrigin string `json:"request_origin"`
	ClientKind    string `json:"client_kind"`
	AnsweredFor   string `json:"answered_for"`
	Principal     string `json:"principal,omitempty"`
}

func (s *Server) withRequestContext(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		meta := metadataFromRequest(r)
		w.Header().Set("X-Request-Id", meta.RequestID)
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
	requestID := strings.TrimSpace(r.Header.Get("X-Request-Id"))
	if requestID == "" {
		requestID = newRequestID()
	}
	return requestMetadata{
		RequestID:     requestID,
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

	if profile, ok := routeProfileForRequest(r); ok {
		transport = valueOrDefault(transport, profile.Transport)
		origin = valueOrDefault(origin, profile.RequestOrigin)
		clientKind = valueOrDefault(clientKind, profile.ClientKind)
		answeredFor = valueOrDefault(answeredFor, profile.AnsweredFor)
	} else {
		transport = valueOrDefault(transport, "http")
		origin = valueOrDefault(origin, "http_api")
		clientKind = valueOrDefault(clientKind, "http_client")
		answeredFor = valueOrDefault(answeredFor, "user")
	}

	return transport, origin, clientKind, answeredFor
}

func addResponseMetadata(ctx context.Context, payload map[string]any) map[string]any {
	meta := metadataFromContext(ctx)
	payload["request_id"] = meta.RequestID
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

func newRequestID() string {
	var entropy [8]byte
	if _, err := rand.Read(entropy[:]); err != nil {
		return time.Now().UTC().Format("20060102T150405.000000000")
	}
	return time.Now().UTC().Format("20060102T150405.000000000") + "-" + hex.EncodeToString(entropy[:])
}

func valueOrDefault(value, fallback string) string {
	if strings.TrimSpace(value) != "" {
		return value
	}
	return fallback
}
