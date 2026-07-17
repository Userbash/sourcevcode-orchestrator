package localmodels

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestRuntimeHealthDegradedWhenConfiguredModelMissing(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"models": []map[string]any{{"name": "other-model"}},
		})
	}))
	defer server.Close()

	runtime := NewRuntime(Config{Endpoint: server.URL, ModelName: "target-model", HealthTimeout: time.Second, RetryPolicy: RetryPolicy{MaxAttempts: 1}})
	health := runtime.Health(context.Background(), "")

	if !health.OK || health.Status != "degraded" || health.Ready {
		t.Fatalf("Health() = %+v", health)
	}
	if health.Error == "" {
		t.Fatal("Health() error = empty, want degradation reason")
	}
	if health.Endpoint != server.URL {
		t.Fatalf("Health() endpoint = %q, want %q", health.Endpoint, server.URL)
	}
}

func TestRuntimeDoJSONFallsBackToHealthyEndpoint(t *testing.T) {
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "busy", http.StatusServiceUnavailable)
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"models": []map[string]any{{"name": "target-model"}},
		})
	}))
	defer fallback.Close()

	runtime := NewRuntime(Config{
		Endpoint:          primary.URL,
		FallbackEndpoints: []string{fallback.URL},
		ModelName:         "target-model",
		HealthTimeout:     time.Second,
		RetryPolicy:       RetryPolicy{MaxAttempts: 1},
	})

	models, endpoint, err := runtime.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	if endpoint != fallback.URL {
		t.Fatalf("ListModels() endpoint = %q, want %q", endpoint, fallback.URL)
	}
	if runtime.ActiveEndpoint() != fallback.URL {
		t.Fatalf("ActiveEndpoint() = %q, want %q", runtime.ActiveEndpoint(), fallback.URL)
	}
	if len(models) != 1 || models[0].Name != "target-model" {
		t.Fatalf("ListModels() models = %#v", models)
	}
}

func TestManagerSyncTracksResidentsAndFailures(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/ps":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"models": []map[string]any{{"name": "model-a", "size_vram": int64(2 << 30)}}},
			)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	manager := NewManager(NewRuntime(Config{Endpoint: server.URL, ModelName: "model-a", HealthTimeout: time.Second, RetryPolicy: RetryPolicy{MaxAttempts: 1}}))
	if err := manager.Sync(context.Background()); err != nil {
		t.Fatalf("Sync() error = %v", err)
	}

	snapshot := manager.Snapshot()
	if snapshot["status"] != "ready" {
		t.Fatalf("Snapshot status = %v, want ready", snapshot["status"])
	}
	if snapshot["resident_memory_gb"].(float64) <= 0 {
		t.Fatalf("resident_memory_gb = %v, want > 0", snapshot["resident_memory_gb"])
	}
	if len(snapshot["blocked_models"].([]string)) != 0 {
		t.Fatalf("blocked_models = %#v, want empty", snapshot["blocked_models"])
	}

	broken := NewManager(NewRuntime(Config{Endpoint: "http://127.0.0.1:1", ModelName: "model-b", HealthTimeout: 10 * time.Millisecond, RetryPolicy: RetryPolicy{MaxAttempts: 1}}))
	err := broken.Sync(context.Background())
	if err == nil {
		t.Fatal("Sync() error = nil, want connection failure")
	}
	blocked := broken.Snapshot()["blocked_models"].([]string)
	if len(blocked) != 1 || blocked[0] != "model-b" {
		t.Fatalf("blocked_models = %#v", blocked)
	}
	if broken.Snapshot()["status"] != "degraded" {
		t.Fatalf("Snapshot status = %v, want degraded", broken.Snapshot()["status"])
	}
}
