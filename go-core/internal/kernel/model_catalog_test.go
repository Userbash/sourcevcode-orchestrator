package kernel

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"sourcevcode-orchestrator/go-core/internal/agents"
)

func TestProviderModelRegistryFiltersModelsThroughCatalog(t *testing.T) {
	path := filepath.Join(t.TempDir(), "kernel-models.txt")
	if err := os.WriteFile(path, []byte("antigravity:claude-sonnet-4-6\n"), 0o644); err != nil {
		t.Fatalf("write catalog: %v", err)
	}
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", path)
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"claude-sonnet-4-6"},{"id":"gpt-5.5"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"antigravity": {
			Provider:                "antigravity",
			BaseURL:                 server.URL,
			ModelsEndpoint:          server.URL + "/models",
			DefaultModel:            "claude-sonnet-4-6",
			ChatCompletionsEndpoint: "",
		},
	})

	registry.Refresh(context.Background())

	snapshot, ok := registry.Snapshot("antigravity")
	if !ok {
		t.Fatal("antigravity snapshot missing")
	}
	if len(snapshot.Models) != 1 {
		t.Fatalf("filtered models=%d want 1: %#v", len(snapshot.Models), snapshot.Models)
	}
	if snapshot.Models[0].ModelName != "claude-sonnet-4-6" {
		t.Fatalf("model=%q want claude-sonnet-4-6", snapshot.Models[0].ModelName)
	}
}

func TestProviderModelRegistrySyncsDiscoveredModelsToCatalog(t *testing.T) {
	path := filepath.Join(t.TempDir(), "kernel-models.txt")
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", path)
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"mistral-large-latest"},{"id":"mistral-small-latest"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"mistral": {
			Provider:       "mistral",
			BaseURL:        server.URL,
			ModelsEndpoint: server.URL + "/models",
			DefaultModel:   "mistral-large-latest",
		},
	})

	registry.Refresh(context.Background())

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read catalog: %v", err)
	}
	text := string(content)
	if !strings.Contains(text, "mistral:mistral-large-latest") {
		t.Fatalf("catalog missing default model: %s", text)
	}
	if !strings.Contains(text, "mistral:mistral-small-latest") {
		t.Fatalf("catalog missing discovered model: %s", text)
	}
}
