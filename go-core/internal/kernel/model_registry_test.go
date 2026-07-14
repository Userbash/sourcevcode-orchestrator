package kernel

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
)

func TestProviderModelRegistryValidatesInventoryAndAggregatesStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"},{"id":"beta"}]}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			if payload.Model == "alpha" {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
				return
			}
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"model is not available"}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "12")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:                "openai",
			ProviderID:              "codexsale",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "alpha",
			APIKey:                  "secret",
			RequireKey:              true,
			Timeout:                 time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected snapshot")
	}
	if snapshot.Status != "degraded" {
		t.Fatalf("unexpected provider status: %s", snapshot.Status)
	}
	if !snapshot.Available {
		t.Fatalf("expected provider to remain available: %#v", snapshot)
	}
	if len(snapshot.Models) != 2 {
		t.Fatalf("unexpected model count: %#v", snapshot.Models)
	}
	statuses := map[string]string{}
	reasons := map[string]string{}
	for _, model := range snapshot.Models {
		statuses[model.ModelName] = model.Status
		reasons[model.ModelName] = model.Reason
	}
	if statuses["alpha"] != "ready" {
		t.Fatalf("unexpected alpha status: %#v", snapshot.Models)
	}
	if statuses["beta"] != "validation_failed" {
		t.Fatalf("unexpected beta status: %#v", snapshot.Models)
	}
	if !strings.Contains(reasons["beta"], "model is not available") {
		t.Fatalf("unexpected beta reason: %q", reasons["beta"])
	}
}
