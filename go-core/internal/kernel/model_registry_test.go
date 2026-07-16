package kernel

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestProviderModelRegistryMarksTransientProbeFailuresAsDegradedButAvailable(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
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
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"error":"upstream busy, try again later"}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "12")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"codexsale": {
			Provider:                "codexsale",
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
	snapshot, ok := registry.Snapshot("codexsale")
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

	alpha := findModel(snapshot.Models, "alpha")
	beta := findModel(snapshot.Models, "beta")
	if alpha.Status != "ready" {
		t.Fatalf("unexpected alpha status: %#v", alpha)
	}
	if beta.Status != "probe_failed" {
		t.Fatalf("unexpected beta status: %#v", beta)
	}
	if !beta.Available {
		t.Fatalf("expected beta to stay available after transient failure: %#v", beta)
	}
	if got, _ := beta.Metadata["probe_status"].(string); got != "transient_failure" {
		t.Fatalf("unexpected beta probe status: %#v", beta.Metadata)
	}
}

func TestProviderModelRegistryMergesCodexInventoryAndBuildsDisplayVariants(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
		case "/backend-api/codex":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"catalog":{"featured":[{"slug":"gpt-5.6-sol","supported_reasoning_levels":["low","medium","high"]}]}}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"codexsale": {
			Provider:       "codexsale",
			ProviderID:     "codexsale",
			BaseURL:        server.URL + "/v1",
			ModelsEndpoint: server.URL + "/v1/models",
			CodexEndpoint:  server.URL + "/backend-api/codex",
			DefaultModel:   "alpha",
			APIKey:         "secret",
			RequireKey:     true,
			Timeout:        time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("codexsale")
	if !ok {
		t.Fatal("expected snapshot")
	}
	if snapshot.Status != "ready" {
		t.Fatalf("unexpected provider status: %s", snapshot.Status)
	}
	if !snapshot.Available {
		t.Fatalf("expected provider to be available: %#v", snapshot)
	}
	if len(snapshot.Models) != 2 {
		t.Fatalf("unexpected model count: %#v", snapshot.Models)
	}

	sol := findModel(snapshot.Models, "gpt-5.6-sol")
	if sol.Status != "discovered" {
		t.Fatalf("unexpected sol status: %#v", sol)
	}
	sources := stringSliceMetadata(sol.Metadata["inventory_sources"])
	if len(sources) != 1 || sources[0] != "codex" {
		t.Fatalf("unexpected inventory sources: %#v", sol.Metadata)
	}
	variants, ok := sol.Metadata["display_variants"].([]map[string]any)
	if !ok {
		t.Fatalf("display variants missing: %#v", sol.Metadata)
	}
	if len(variants) != 3 {
		t.Fatalf("unexpected display variants: %#v", variants)
	}
}

func TestProviderModelRegistryAnnotatesClaudeFamilyMetadata(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"claude-sonnet-4-6"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"codexsale": {
			Provider:       "codexsale",
			ProviderID:     "codexsale",
			BaseURL:        server.URL + "/v1",
			ModelsEndpoint: server.URL + "/v1/models",
			DefaultModel:   "claude-sonnet-4-6",
			APIKey:         "secret",
			RequireKey:     true,
			Timeout:        time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("codexsale")
	if !ok {
		t.Fatal("expected snapshot")
	}
	model := findModel(snapshot.Models, "claude-sonnet-4-6")
	if family, _ := model.Metadata["model_family"].(string); family != "claude" {
		t.Fatalf("unexpected model family: %#v", model.Metadata)
	}
	aliases := stringSliceMetadata(model.Metadata["family_aliases"])
	if len(aliases) != 1 || aliases[0] != "anthropic" {
		t.Fatalf("unexpected family aliases: %#v", model.Metadata)
	}
	pools := stringSliceMetadata(model.Metadata["resource_pools"])
	if len(pools) != 2 || pools[0] != "anthropic" || pools[1] != "claude" {
		t.Fatalf("unexpected resource pools: %#v", model.Metadata)
	}
}

func findModel(models []domain.ProviderModelStatus, name string) domain.ProviderModelStatus {
	for _, model := range models {
		if model.ModelName == name {
			return model
		}
	}
	return domain.ProviderModelStatus{}
}

func TestProviderModelRegistryValidatesNonCodexProviders(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"gpt-5.4"},{"id":"ghost-model"}]}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			if payload.Model == "gpt-5.4" {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"id":"ok"}`))
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
			ProviderID:              "openai",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "gpt-5.4",
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
	good := findModel(snapshot.Models, "gpt-5.4")
	if good.Status != "ready" || !good.Available {
		t.Fatalf("expected healthy validated model, got %#v", good)
	}
	ghost := findModel(snapshot.Models, "ghost-model")
	if ghost.Status != "validation_failed" {
		t.Fatalf("unexpected ghost status: %#v", ghost)
	}
	if ghost.Available {
		t.Fatalf("expected ghost model to be unavailable: %#v", ghost)
	}
}
