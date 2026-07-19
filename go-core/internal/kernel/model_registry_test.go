package kernel

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestProviderModelRegistryMarksTransientProbeFailuresAsPendingAndUnavailable(t *testing.T) {
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
	if beta.Status != "verification_cooldown" {
		t.Fatalf("unexpected beta status: %#v", beta)
	}
	if beta.Available {
		t.Fatalf("expected beta to remain unavailable until probe succeeds: %#v", beta)
	}
	if beta.VerificationStatus != "verifying" || beta.TransportStatus != "retryable_failure" || beta.QueueStatus != "cooldown" || beta.NextVerificationAt == nil {
		t.Fatalf("unexpected beta verification lifecycle: %#v", beta)
	}
	if beta.LastError == nil || !beta.LastError.Retryable || beta.LastError.HTTPStatus != http.StatusServiceUnavailable {
		t.Fatalf("unexpected beta last error: %#v", beta.LastError)
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
	if snapshot.Status != "verifying" {
		t.Fatalf("unexpected provider status: %s", snapshot.Status)
	}
	if snapshot.Available {
		t.Fatalf("expected provider inventory without confirmed models to remain unavailable: %#v", snapshot)
	}
	if len(snapshot.Models) != 2 {
		t.Fatalf("unexpected model count: %#v", snapshot.Models)
	}

	sol := findModel(snapshot.Models, "gpt-5.6-sol")
	if sol.Status != "verification_pending" || sol.VerificationStatus != "verifying" || sol.InventoryStatus != "inventory_verified" {
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

func TestProviderModelRegistryIgnoresOptionalCodexNotFoundWhenModelsInventoryWorks(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
		case "/backend-api/codex":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"error":{"message":"Not Found","type":"invalid_request_error","code":"not_found"}}`))
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
	if snapshot.Status != "verifying" {
		t.Fatalf("unexpected provider status: %s error=%q", snapshot.Status, snapshot.Error)
	}
	if snapshot.Error != "alpha: model discovered in provider inventory and awaiting transport verification" {
		t.Fatalf("unexpected provider error: %q", snapshot.Error)
	}
	if len(snapshot.Models) != 1 {
		t.Fatalf("unexpected model count: %#v", snapshot.Models)
	}
}

func TestProviderModelRegistryTreatsPlaceholderEndpointAsNotConfigured(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"antigravity": {
			Provider:                "antigravity",
			ProviderID:              "antigravity",
			BaseURL:                 "https://api.antigravity.example/v1",
			ModelsEndpoint:          "https://api.antigravity.example/v1/models",
			ChatCompletionsEndpoint: "https://api.antigravity.example/v1/chat/completions",
			DefaultModel:            "antigravity-coder",
			APIKey:                  "secret",
			RequireKey:              true,
			Timeout:                 time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("antigravity")
	if !ok {
		t.Fatal("expected snapshot")
	}
	if snapshot.Status != "not_configured" {
		t.Fatalf("unexpected provider status: %s", snapshot.Status)
	}
	if snapshot.Error != "provider endpoint uses a placeholder host and must be replaced with a real upstream URL" {
		t.Fatalf("unexpected provider error: %q", snapshot.Error)
	}
	if len(snapshot.Models) != 0 {
		t.Fatalf("expected no models for placeholder endpoint: %#v", snapshot.Models)
	}
}

func TestProviderModelRegistrySkipsNonChatInventoryModels(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "12")

	var probed []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"codestral-latest","capabilities":{"completion_chat":true}},{"id":"codestral-embed","capabilities":{"completion_chat":false}}]}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			probed = append(probed, payload.Model)
			if payload.Model != "codestral-latest" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"unexpected model probe"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"mistral": {
			Provider:                "mistral",
			ProviderID:              "mistral",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "codestral-latest",
			APIKey:                  "secret",
			RequireKey:              true,
			Timeout:                 time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("mistral")
	if !ok {
		t.Fatal("expected snapshot")
	}
	if snapshot.Status != "ready" {
		t.Fatalf("unexpected provider status: %s error=%q", snapshot.Status, snapshot.Error)
	}
	if len(probed) != 1 || probed[0] != "codestral-latest" {
		t.Fatalf("unexpected probed models: %#v", probed)
	}

	chat := findModel(snapshot.Models, "codestral-latest")
	if chat.Status != "ready" || !chat.Available {
		t.Fatalf("unexpected chat model state: %#v", chat)
	}

	embed := findModel(snapshot.Models, "codestral-embed")
	if embed.Status != "transport_inapplicable" || embed.VerificationStatus != "skipped" || embed.TransportStatus != "transport_inapplicable" {
		t.Fatalf("unexpected embed model state: %#v", embed)
	}
	if embed.LastError != nil || embed.Available {
		t.Fatalf("expected embed model to be skipped without transport failure: %#v", embed)
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
	if model.Metadata["streaming"] != true {
		t.Fatalf("expected streaming metadata in %#v", model.Metadata)
	}
	if model.Metadata["tool_calling"] != true {
		t.Fatalf("expected tool_calling metadata in %#v", model.Metadata)
	}
	if model.Metadata["long_context"] != true {
		t.Fatalf("expected long_context metadata in %#v", model.Metadata)
	}
}

func TestInferModelFamilyRecognizesConfirmedProviderFamilies(t *testing.T) {
	cases := map[string]string{
		"codestral-latest":                 "mistral",
		"devstral-medium-latest":           "mistral",
		"magistral-medium-latest":          "mistral",
		"qwen2.5:0.5b":                     "qwen",
		"gemma4-12b-agentic-fable5:q4_k_m": "gemma",
	}
	for modelName, want := range cases {
		if got := inferModelFamily(modelName); got != want {
			t.Fatalf("inferModelFamily(%q) = %q, want %q", modelName, got, want)
		}
	}
}

func TestProviderModelRegistrySeparatesInventoryTrafficFromAgents(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")
	t.Setenv("GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY", "1")
	t.Setenv("GO_CORE_PROVIDER_MAX_RETRIES", "0")

	var active atomic.Int32
	var maxActive atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := active.Add(1)
		defer active.Add(-1)
		for {
			observed := maxActive.Load()
			if current <= observed || maxActive.CompareAndSwap(observed, current) {
				break
			}
		}
		time.Sleep(25 * time.Millisecond)
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"shared-model"}]}`))
		case "/v1/chat/completions":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"id":"completion-shared","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"total_tokens":4}}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	cfg := agents.OpenAICompatibleConfig{
		Provider:                "shared",
		ProviderID:              "shared",
		BaseURL:                 server.URL + "/v1",
		ModelsEndpoint:          server.URL + "/v1/models",
		ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
		DefaultModel:            "shared-model",
		APIKey:                  "secret",
		RequireKey:              true,
		Timeout:                 time.Second,
	}
	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{"shared": cfg})
	agent := agents.NewOpenAICompatibleAgent(agents.AgentDescriptor{
		ID:           "coder",
		Type:         "coding",
		Provider:     "shared",
		Capabilities: []string{"code"},
	}, cfg)

	var wg sync.WaitGroup
	errCh := make(chan error, 2)
	wg.Add(2)
	go func() {
		defer wg.Done()
		registry.Refresh(context.Background())
		snapshot, ok := registry.Snapshot("shared")
		if !ok {
			errCh <- context.Canceled
			return
		}
		if len(snapshot.Models) != 1 || snapshot.Models[0].ModelName != "shared-model" {
			errCh <- context.DeadlineExceeded
		}
	}()
	go func() {
		defer wg.Done()
		result := agent.Execute(context.Background(), domain.Task{
			ID:                 "shared-task",
			Type:               domain.TaskTypeCode,
			RequiredCapability: "code",
			AssignedModel:      "shared-model",
			Input:              domain.TaskInput{Description: "Run concurrently with registry refresh"},
		})
		if result.Status != domain.TaskStatusDone {
			errCh <- context.DeadlineExceeded
		}
	}()
	wg.Wait()
	close(errCh)
	for err := range errCh {
		if err != nil {
			t.Fatalf("unexpected concurrent failure: %v", err)
		}
	}
	if got := maxActive.Load(); got < 2 {
		t.Fatalf("expected inventory and primary traffic classes to run in parallel, max active = %d", got)
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
	if ghost.Status != "validation_failed" || ghost.VerificationStatus != "unconfirmed" || ghost.LastError == nil || ghost.LastError.Category != "invalid_request" {
		t.Fatalf("unexpected ghost status: %#v", ghost)
	}
	if ghost.Available {
		t.Fatalf("expected ghost model to be unavailable: %#v", ghost)
	}
}

func TestProviderModelRegistryPrioritizesDefaultModelValidation(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"},{"id":"omega"}]}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			if payload.Model != "omega" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"only the default model should be probed first"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "1")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:                "openai",
			ProviderID:              "openai",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "omega",
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
	omega := findModel(snapshot.Models, "omega")
	alpha := findModel(snapshot.Models, "alpha")
	if omega.VerificationStatus != "confirmed" || omega.Status != "ready" || !omega.Available {
		t.Fatalf("unexpected omega status: %#v", omega)
	}
	if alpha.VerificationStatus != "verifying" || alpha.Status != "registration_queued" || alpha.Available || alpha.QueueStatus != "queued" || alpha.QueuePosition != 2 {
		t.Fatalf("unexpected alpha status: %#v", alpha)
	}
}

func TestProviderModelRegistryFallsBackToDefaultModelWhenInventoryUnavailable(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"error":"inventory route is not available"}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			if payload.Model != "mistral-large-latest" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"unexpected model"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "4")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"mistral": {
			Provider:                "mistral",
			ProviderID:              "mistral",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "mistral-large-latest",
			APIKey:                  "secret",
			RequireKey:              true,
			Timeout:                 time.Second,
		},
	})

	registry.Refresh(context.Background())
	snapshot, ok := registry.Snapshot("mistral")
	if !ok {
		t.Fatal("expected snapshot")
	}
	if snapshot.Status != "degraded" || !snapshot.Available {
		t.Fatalf("unexpected provider snapshot: %#v", snapshot)
	}
	model := findModel(snapshot.Models, "mistral-large-latest")
	if model.VerificationStatus != "confirmed" || model.Status != "ready" || !model.Available {
		t.Fatalf("unexpected model status: %#v", model)
	}
	if synthetic, _ := model.Metadata["synthetic_inventory"].(bool); !synthetic {
		t.Fatalf("expected synthetic inventory marker: %#v", model.Metadata)
	}
}

func TestProviderModelRegistryRefreshIfStaleSkipsFreshSnapshot(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	var modelsHits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		modelsHits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:       "openai",
			ProviderID:     "openai",
			BaseURL:        server.URL + "/v1",
			ModelsEndpoint: server.URL + "/v1/models",
			DefaultModel:   "alpha",
			APIKey:         "secret",
			RequireKey:     true,
			Timeout:        time.Second,
		},
	})

	registry.Refresh(context.Background())
	registry.RefreshIfStale(context.Background())
	if got := modelsHits.Load(); got != 1 {
		t.Fatalf("expected one models refresh, got %d", got)
	}
}

func TestProviderModelRegistryRefreshIfStaleRefreshesExpiredSnapshot(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	var modelsHits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		modelsHits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:       "openai",
			ProviderID:     "openai",
			BaseURL:        server.URL + "/v1",
			ModelsEndpoint: server.URL + "/v1/models",
			DefaultModel:   "alpha",
			APIKey:         "secret",
			RequireKey:     true,
			Timeout:        time.Second,
		},
	})

	registry.Refresh(context.Background())
	registry.mu.Lock()
	snapshot := registry.snapshots["openai"]
	snapshot.ObservedAt = time.Now().Add(-6 * time.Minute)
	registry.snapshots["openai"] = snapshot
	registry.mu.Unlock()
	registry.RefreshIfStale(context.Background())
	if got := modelsHits.Load(); got != 2 {
		t.Fatalf("expected expired snapshot to trigger second refresh, got %d", got)
	}
}

func TestProviderModelRegistryConfirmationTTLMarksModelsStaleAndReRegistersThem(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "1")
	t.Setenv("AI_BRIDGE_MODEL_CONFIRMATION_TTL", "1s")

	probeCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
		case "/v1/chat/completions":
			probeCalls++
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:                "openai",
			ProviderID:              "openai",
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
	first, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected initial snapshot")
	}
	model := findModel(first.Models, "alpha")
	if model.Status != "ready" || model.VerificationStatus != "confirmed" || model.ExpiresAt == nil || model.NextVerificationAt == nil {
		t.Fatalf("unexpected initial model state: %#v", model)
	}
	if probeCalls != 1 {
		t.Fatalf("expected one probe call, got %d", probeCalls)
	}

	time.Sleep(1200 * time.Millisecond)
	registry.Refresh(context.Background())
	second, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected second snapshot")
	}
	model = findModel(second.Models, "alpha")
	if model.Status != "ready" || model.VerificationStatus != "confirmed" || !model.Available {
		t.Fatalf("expected model to be re-registered after TTL expiry: %#v", model)
	}
	if probeCalls < 2 {
		t.Fatalf("expected model to be probed again after TTL expiry, got %d calls", probeCalls)
	}
}

func TestProviderModelRegistryAppliesRetryCooldownAndHonorsNextAttempt(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("GO_CORE_PROVIDER_MAX_RETRIES", "0")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "1")
	t.Setenv("AI_BRIDGE_MODEL_RETRY_COOLDOWN", "1s")

	probeCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
		case "/v1/chat/completions":
			probeCalls++
			if probeCalls == 1 {
				w.WriteHeader(http.StatusServiceUnavailable)
				_, _ = w.Write([]byte(`{"error":"upstream busy"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:                "openai",
			ProviderID:              "openai",
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
	first, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected initial snapshot")
	}
	model := findModel(first.Models, "alpha")
	if model.Status != "verification_cooldown" || model.QueueStatus != "cooldown" || model.NextVerificationAt == nil {
		t.Fatalf("unexpected cooldown state: %#v", model)
	}
	if probeCalls != 1 {
		t.Fatalf("expected one probe call, got %d", probeCalls)
	}

	registry.Refresh(context.Background())
	second, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected second snapshot")
	}
	model = findModel(second.Models, "alpha")
	if model.Status != "verification_cooldown" || probeCalls != 1 {
		t.Fatalf("expected cooldown to suppress immediate re-probe: state=%#v calls=%d", model, probeCalls)
	}

	time.Sleep(1200 * time.Millisecond)
	registry.Refresh(context.Background())
	third, ok := registry.Snapshot("openai")
	if !ok {
		t.Fatal("expected third snapshot")
	}
	model = findModel(third.Models, "alpha")
	if model.Status != "ready" || model.VerificationStatus != "confirmed" || !model.Available {
		t.Fatalf("expected model to recover after cooldown elapsed: %#v", model)
	}
	if probeCalls < 2 {
		t.Fatalf("expected second probe after cooldown, got %d calls", probeCalls)
	}
}

func TestProviderModelRegistryMarksQueueOverflowWhenUnregisteredModelsExceedLimit(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "true")
	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_LIMIT", "1")
	t.Setenv("AI_BRIDGE_MODEL_QUEUE_LIMIT", "2")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"},{"id":"beta"},{"id":"gamma"},{"id":"omega"}]}`))
		case "/v1/chat/completions":
			var payload struct {
				Model string `json:"model"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatalf("decode chat payload: %v", err)
			}
			if payload.Model != "omega" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"default model should be probed first"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ok","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"openai": {
			Provider:                "openai",
			ProviderID:              "openai",
			BaseURL:                 server.URL + "/v1",
			ModelsEndpoint:          server.URL + "/v1/models",
			ChatCompletionsEndpoint: server.URL + "/v1/chat/completions",
			DefaultModel:            "omega",
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
	omega := findModel(snapshot.Models, "omega")
	alpha := findModel(snapshot.Models, "alpha")
	beta := findModel(snapshot.Models, "beta")
	gamma := findModel(snapshot.Models, "gamma")
	if omega.Status != "ready" || !omega.Available {
		t.Fatalf("unexpected omega status: %#v", omega)
	}
	if alpha.Status != "registration_queued" || alpha.QueueStatus != "queued" || alpha.QueuePosition != 2 {
		t.Fatalf("unexpected alpha queue state: %#v", alpha)
	}
	if beta.Status != "registration_overflow" || beta.QueueStatus != "overflow" {
		t.Fatalf("unexpected beta overflow state: %#v", beta)
	}
	if gamma.Status != "registration_overflow" || gamma.QueueStatus != "overflow" {
		t.Fatalf("unexpected gamma overflow state: %#v", gamma)
	}
}

func TestProviderModelRegistryStartDoesNotBlockOnInitialRefresh(t *testing.T) {
	t.Setenv("GO_CORE_MODEL_CATALOG_PATH", filepath.Join(t.TempDir(), "kernel-models.txt"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		switch r.URL.Path {
		case "/v1/models":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":[{"id":"alpha"}]}`))
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")

	registry := NewProviderModelRegistry(map[string]agents.OpenAICompatibleConfig{
		"ai_kernel": {
			Provider:       "ai_kernel",
			ProviderID:     "ai_kernel",
			BaseURL:        server.URL + "/v1",
			ModelsEndpoint: server.URL + "/v1/models",
			DefaultModel:   "alpha",
			APIKey:         "local",
			RequireKey:     true,
			Timeout:        time.Second,
		},
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startedAt := time.Now()
	registry.Start(ctx)
	if elapsed := time.Since(startedAt); elapsed > 100*time.Millisecond {
		t.Fatalf("Start() blocked for %s", elapsed)
	}
}
