package api

import (
	"net/http"
	"strings"
)

type routeProfile struct {
	Transport       string
	RequestOrigin   string
	ClientKind      string
	AnsweredFor     string
	MigrationAction string
}

func routeProfileForRequest(r *http.Request) (routeProfile, bool) {
	if r == nil || r.URL == nil {
		return routeProfile{}, false
	}

	method := r.Method
	path := r.URL.Path

	switch {
	case strings.HasPrefix(path, "/chat/ws"):
		return routeProfile{
			Transport:     "websocket",
			RequestOrigin: "chat_ws",
			ClientKind:    "external_chat",
			AnsweredFor:   "user",
		}, true
	case strings.HasPrefix(path, "/ws/runtime/events"):
		return routeProfile{
			Transport:     "websocket",
			RequestOrigin: "runtime_ws",
			ClientKind:    "runtime_stream",
			AnsweredFor:   "observer",
		}, true
	case strings.HasPrefix(path, "/ws/providers/inventory"):
		return routeProfile{
			Transport:     "websocket",
			RequestOrigin: "inventory_ws",
			ClientKind:    "inventory_client",
			AnsweredFor:   "observer",
		}, true
	case strings.HasPrefix(path, "/events/"):
		return routeProfile{
			Transport:     "sse",
			RequestOrigin: "runtime_events",
			ClientKind:    "event_stream",
			AnsweredFor:   "observer",
		}, true
	case strings.HasPrefix(path, "/sourcecraft/delegate"):
		profile := routeProfile{
			Transport:     "http",
			RequestOrigin: "sourcecraft_http",
			ClientKind:    "http_client",
			AnsweredFor:   "user",
		}
		if method == http.MethodPost && path == "/sourcecraft/delegate" {
			profile.MigrationAction = "sourcecraft.delegate.get"
		}
		return profile, true
	case strings.HasPrefix(path, "/sourcecraft/parallel_delegate"):
		profile := routeProfile{
			Transport:     "http",
			RequestOrigin: "sourcecraft_http",
			ClientKind:    "http_client",
			AnsweredFor:   "user",
		}
		if method == http.MethodPost && path == "/sourcecraft/parallel_delegate" {
			profile.MigrationAction = "sourcecraft.parallel_delegate.get"
		}
		return profile, true
	case path == "/health" || path == "/health/full" || path == "/api/health":
		return routeProfile{
			Transport:     "http",
			RequestOrigin: "health_http",
			ClientKind:    "http_client",
			AnsweredFor:   "observer",
		}, true
	case strings.HasPrefix(path, "/tasks/") && strings.HasSuffix(path, "/checkpoint"):
		profile := routeProfile{
			Transport:     "http",
			RequestOrigin: "tasks_http",
			ClientKind:    "http_client",
			AnsweredFor:   "user",
		}
		if method == http.MethodGet {
			profile.MigrationAction = "tasks.plan.checkpoint.get"
		}
		return profile, true
	case strings.HasPrefix(path, "/tasks/") && strings.HasSuffix(path, "/resume_plan"):
		profile := routeProfile{
			Transport:     "http",
			RequestOrigin: "tasks_http",
			ClientKind:    "http_client",
			AnsweredFor:   "user",
		}
		if method == http.MethodPost {
			profile.MigrationAction = "tasks.plan.resume"
		}
		return profile, true
	case strings.HasPrefix(path, "/tasks"):
		profile := routeProfile{
			Transport:     "http",
			RequestOrigin: "tasks_http",
			ClientKind:    "http_client",
			AnsweredFor:   "user",
		}
		if method == http.MethodPost && path == "/tasks/preview_plan" {
			profile.MigrationAction = "tasks.plan.preview"
		}
		if method == http.MethodPost && path == "/tasks/run_plan" {
			profile.MigrationAction = "tasks.plan.run"
		}
		return profile, true
	default:
		return routeProfile{}, false
	}
}
