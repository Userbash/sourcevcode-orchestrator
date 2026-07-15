package agents

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestOpenAICompatibleAgentExecutesTask(t *testing.T) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer secret" {
			t.Fatalf("unexpected authorization: %q", got)
		}
		var request chatCompletionRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != "requested-model" {
			t.Fatalf("unexpected model: %q", request.Model)
		}
		if len(request.Messages) != 2 {
			t.Fatalf("unexpected message count: %#v", request.Messages)
		}
		content, _ := request.Messages[1].Content.(string)
		if !strings.Contains(content, "Implement feature") {
			t.Fatalf("unexpected messages: %#v", request.Messages)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"completion-1","choices":[{"message":{"role":"assistant","content":"implemented"},"finish_reason":"stop"}],"usage":{"total_tokens":12}}`))
	}))
	defer server.Close()

	agent := NewOpenAICompatibleAgent(AgentDescriptor{
		ID: "coder", Type: "coding", Capabilities: []string{"code"},
	}, OpenAICompatibleConfig{
		Provider: "test", BaseURL: server.URL + "/v1", APIKey: "secret",
		DefaultModel: "default-model", RequireKey: true, Timeout: time.Second,
	})
	result := agent.Execute(context.Background(), domain.Task{
		ID: "task-1", Type: domain.TaskTypeCode, RequiredCapability: "code",
		AssignedModel: "requested-model",
		Input: domain.TaskInput{
			Description: "Implement feature",
			Constraints: []string{"no stubs"},
		},
	})
	if result.Status != domain.TaskStatusDone {
		t.Fatalf("unexpected status: %s errors=%v", result.Status, result.Errors)
	}
	if result.Output.Summary != "implemented" {
		t.Fatalf("unexpected output: %q", result.Output.Summary)
	}
	if result.Provider != "test" || result.AgentID != "coder" {
		t.Fatalf("unexpected identity: %#v", result)
	}
	if enabled, _ := result.Output.Artifacts["runtime_tools_enabled"].(bool); !enabled {
		t.Fatalf("expected runtime tools enabled, got %#v", result.Output.Artifacts)
	}
}

func TestOpenAICompatibleAgentUsesWorkspaceTools(t *testing.T) {
	repo := t.TempDir()
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		requests++
		var request chatCompletionRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		switch requests {
		case 1:
			if len(request.Tools) == 0 || request.ToolChoice != "auto" {
				t.Fatalf("expected workspace tools on first request: %#v", request)
			}
			_, _ = w.Write([]byte(`{
				"id":"completion-tools",
				"choices":[{
					"message":{
						"role":"assistant",
						"tool_calls":[
							{"id":"call-1","type":"function","function":{"name":"write_file","arguments":"{\"path\":\"notes.txt\",\"content\":\"hello from tool runtime\"}"}},
							{"id":"call-2","type":"function","function":{"name":"run_command","arguments":"{\"command\":[\"pwd\"]}"}}
						]
					},
					"finish_reason":"tool_calls"
				}],
				"usage":{"total_tokens":21}
			}`))
		case 2:
			if len(request.Messages) < 5 {
				t.Fatalf("expected tool results in second request, got %#v", request.Messages)
			}
			_, _ = w.Write([]byte(`{
				"id":"completion-final",
				"choices":[{
					"message":{"role":"assistant","content":"workspace updated"},
					"finish_reason":"stop"
				}],
				"usage":{"total_tokens":34}
			}`))
		default:
			t.Fatalf("unexpected extra request %d", requests)
		}
	}))
	defer server.Close()

	agent := NewOpenAICompatibleAgent(AgentDescriptor{
		ID: "coder", Type: "coding", Capabilities: []string{"code"},
	}, OpenAICompatibleConfig{
		Provider: "test", BaseURL: server.URL + "/v1", APIKey: "secret",
		DefaultModel: "default-model", RequireKey: true, Timeout: time.Second,
	})
	result := agent.Execute(context.Background(), domain.Task{
		ID: "task-tools", Type: domain.TaskTypeCode, RequiredCapability: "code",
		AssignedModel: "requested-model",
		Input:         domain.TaskInput{Description: "Create a note and inspect cwd"},
		Context:       domain.TaskContext{RepoPath: repo},
	})
	if result.Status != domain.TaskStatusDone {
		t.Fatalf("unexpected status: %s errors=%v", result.Status, result.Errors)
	}
	if result.Output.Summary != "workspace updated" {
		t.Fatalf("unexpected summary: %q", result.Output.Summary)
	}
	if len(result.Output.FilesChanged) != 1 || result.Output.FilesChanged[0] != "notes.txt" {
		t.Fatalf("unexpected files changed: %#v", result.Output.FilesChanged)
	}
	if len(result.Output.CommandsRun) != 1 || result.Output.CommandsRun[0] != "pwd" {
		t.Fatalf("unexpected commands: %#v", result.Output.CommandsRun)
	}
	body, err := os.ReadFile(filepath.Join(repo, "notes.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "hello from tool runtime" {
		t.Fatalf("unexpected file contents: %q", string(body))
	}
}

func TestOpenAICompatibleAgentReportsProviderFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"message":"bad credential"}}`))
	}))
	defer server.Close()

	agent := NewOpenAICompatibleAgent(AgentDescriptor{
		ID: "coder", Capabilities: []string{"code"},
	}, OpenAICompatibleConfig{
		Provider: "test", BaseURL: server.URL, APIKey: "wrong",
		DefaultModel: "model", RequireKey: true, Timeout: time.Second,
	})
	result := agent.Execute(context.Background(), domain.Task{
		ID: "task-2", Type: domain.TaskTypeCode, RequiredCapability: "code",
		Input: domain.TaskInput{Description: "Do work"},
	})
	if result.Status != domain.TaskStatusFailed {
		t.Fatalf("unexpected status: %s", result.Status)
	}
	if len(result.Errors) != 1 || !strings.Contains(result.Errors[0], "bad credential") {
		t.Fatalf("unexpected errors: %v", result.Errors)
	}
}

func TestOpenAICompatibleAgentRequiresCredentials(t *testing.T) {
	agent := NewOpenAICompatibleAgent(AgentDescriptor{
		ID: "cloud", Capabilities: []string{"code"},
	}, OpenAICompatibleConfig{
		Provider: "cloud", BaseURL: "https://example.invalid/v1",
		DefaultModel: "model", RequireKey: true,
	})
	if agent.Info().Status != domain.AgentStatusOffline {
		t.Fatalf("expected offline, got %s", agent.Info().Status)
	}
	if agent.CanAccept(domain.Task{RequiredCapability: "code"}) {
		t.Fatal("unconfigured agent accepted a task")
	}
	health := agent.Probe(context.Background())
	if health.Configured || health.Status != "not_configured" {
		t.Fatalf("unexpected health: %#v", health)
	}
}

func TestOpenAICompatibleAgentProbe(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	agent := NewOpenAICompatibleAgent(AgentDescriptor{ID: "probe"}, OpenAICompatibleConfig{
		Provider: "test", BaseURL: server.URL + "/v1", DefaultModel: "model",
	})
	health := agent.Probe(context.Background())
	if !health.Configured || !health.Available || health.Status != "ready" {
		t.Fatalf("unexpected health: %#v", health)
	}
}

func TestNormalizeV1Base(t *testing.T) {
	cases := map[string]string{
		"http://localhost:11434":    "http://localhost:11434/v1",
		"http://localhost:11434/v1": "http://localhost:11434/v1",
		"http://localhost:11434/":   "http://localhost:11434/v1",
	}
	for input, expected := range cases {
		if actual := normalizeV1Base(input, ""); actual != expected {
			t.Fatalf("normalizeV1Base(%q)=%q want %q", input, actual, expected)
		}
	}
}

func TestPreferredCloudProvider(t *testing.T) {
	t.Setenv("AI_BRIDGE_CLOUD_PROVIDER", "")
	t.Setenv("GO_CORE_CLOUD_PROVIDER", "")

	if got := PreferredCloudProvider(map[string]OpenAICompatibleConfig{
		"openai":    {Provider: "openai", BaseURL: "https://api.openai.com/v1", DefaultModel: "gpt-5.5", APIKey: "secret", RequireKey: true},
		"codexsale": {Provider: "codexsale", BaseURL: "https://codex.sale/v1", DefaultModel: "gpt-5.6-sol", APIKey: "secret", RequireKey: true},
	}); got != "openai" {
		t.Fatalf("PreferredCloudProvider()=%q want %q", got, "openai")
	}

	if got := PreferredCloudProvider(map[string]OpenAICompatibleConfig{
		"openai": {Provider: "openai", BaseURL: "https://api.openai.com/v1", DefaultModel: "gpt-5.5", APIKey: "secret", RequireKey: true},
	}); got != "openai" {
		t.Fatalf("PreferredCloudProvider()=%q want %q", got, "openai")
	}
}

func TestPreferredCloudProviderRespectsOverride(t *testing.T) {
	t.Setenv("AI_BRIDGE_CLOUD_PROVIDER", "codexsale")
	t.Setenv("GO_CORE_CLOUD_PROVIDER", "")

	if got := PreferredCloudProvider(map[string]OpenAICompatibleConfig{
		"openai":    {Provider: "openai", BaseURL: "https://api.openai.com/v1", DefaultModel: "gpt-5.5", APIKey: "secret", RequireKey: true},
		"codexsale": {Provider: "codexsale", BaseURL: "https://codex.sale/v1", DefaultModel: "gpt-5.6-sol", APIKey: "secret", RequireKey: true},
	}); got != "codexsale" {
		t.Fatalf("PreferredCloudProvider()=%q want %q", got, "codexsale")
	}
}

func TestSelectCloudProvider(t *testing.T) {
	configs := map[string]OpenAICompatibleConfig{
		"openai":    {Provider: "openai", BaseURL: "https://api.openai.com/v1", DefaultModel: "gpt-5.5", APIKey: "secret", RequireKey: true},
		"codexsale": {Provider: "codexsale", BaseURL: "https://codex.sale/v1", DefaultModel: "gpt-5.6-sol", APIKey: "secret", RequireKey: true},
		"mistral":   {Provider: "mistral", BaseURL: "https://api.mistral.ai/v1", DefaultModel: "mistral-large-latest", APIKey: "secret", RequireKey: true},
	}

	if got := SelectCloudProvider(configs, "auto"); got != "openai" {
		t.Fatalf("SelectCloudProvider(auto)=%q want %q", got, "openai")
	}
	if got := SelectCloudProvider(configs, "codexsale"); got != "codexsale" {
		t.Fatalf("SelectCloudProvider(codexsale)=%q want %q", got, "codexsale")
	}
	if got := SelectCloudProvider(configs, "openai"); got != "openai" {
		t.Fatalf("SelectCloudProvider(openai)=%q want %q", got, "openai")
	}
	if got := SelectCloudProvider(configs, "mistral"); got != "mistral" {
		t.Fatalf("SelectCloudProvider(mistral)=%q want %q", got, "mistral")
	}

	aliasConfigs := map[string]OpenAICompatibleConfig{
		"openai": {
			Provider:     "openai",
			ProviderID:   "codexsale",
			BaseURL:      "https://codex.sale/v1",
			DefaultModel: "gpt-5.6-sol",
			APIKey:       "secret",
			RequireKey:   true,
		},
		"codexsale": {Provider: "codexsale", BaseURL: "https://codex.sale/v1", DefaultModel: "gpt-5.6-sol", APIKey: "secret", RequireKey: true},
	}
	if got := SelectCloudProvider(aliasConfigs, "auto"); got != "codexsale" {
		t.Fatalf("SelectCloudProvider(alias auto)=%q want %q", got, "codexsale")
	}
	if got := SelectCloudProvider(map[string]OpenAICompatibleConfig{
		"codexsale": {Provider: "codexsale", BaseURL: "https://codex.sale/v1", DefaultModel: "gpt-5.6-sol", APIKey: "secret", RequireKey: true},
	}, "openai"); got != "codexsale" {
		t.Fatalf("SelectCloudProvider(openai fallback)=%q want %q", got, "codexsale")
	}
	if got := SelectCloudProvider(map[string]OpenAICompatibleConfig{
		"mistral": {Provider: "mistral", BaseURL: "https://api.mistral.ai/v1", DefaultModel: "mistral-large-latest", APIKey: "secret", RequireKey: true},
	}, "auto"); got != "mistral" {
		t.Fatalf("SelectCloudProvider(auto non-gpt cloud)=%q want %q", got, "mistral")
	}
}

func TestLoadOpenAICompatibleConfigsPreservesOpenAIAndSeedsCodexSaleAlias(t *testing.T) {
	t.Setenv("OPENAI_BASE_URL", "https://codex.sale/v1")
	t.Setenv("OPENAI_API_KEY", "secret")
	t.Setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-sol")
	t.Setenv("CODEX_SALE_BASE_URL", "")
	t.Setenv("CODEX_SALE_API_KEY", "")
	t.Setenv("CODEX_SALE_MODEL", "")

	configs := LoadOpenAICompatibleConfigs()

	if _, ok := configs["openai"]; !ok {
		t.Fatal("expected openai config to be preserved")
	}
	codexCfg, ok := configs["codexsale"]
	if !ok {
		t.Fatal("expected codexsale config to be present")
	}
	if codexCfg.APIKey != "secret" {
		t.Fatalf("codexsale api key=%q want %q", codexCfg.APIKey, "secret")
	}
	if codexCfg.BaseURL != "https://codex.sale/v1" {
		t.Fatalf("codexsale base url=%q want %q", codexCfg.BaseURL, "https://codex.sale/v1")
	}
}

func TestOpenAICompatibleAgentUsesCustomChatEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/custom/chat" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"completion-custom","choices":[{"message":{"role":"assistant","content":"custom endpoint used"},"finish_reason":"stop"}]}`))
	}))
	defer server.Close()

	agent := NewOpenAICompatibleAgent(AgentDescriptor{
		ID: "coder", Type: "coding", Capabilities: []string{"code"},
	}, OpenAICompatibleConfig{
		Provider:                "test",
		BaseURL:                 server.URL + "/v1",
		ChatCompletionsEndpoint: server.URL + "/custom/chat",
		APIKey:                  "secret",
		DefaultModel:            "default-model",
		RequireKey:              true,
		Timeout:                 time.Second,
	})
	result := agent.Execute(context.Background(), domain.Task{
		ID:                 "task-custom-endpoint",
		Type:               domain.TaskTypeDocs,
		RequiredCapability: "code",
		Input:              domain.TaskInput{Description: "Use custom endpoint"},
	})
	if result.Status != domain.TaskStatusDone {
		t.Fatalf("unexpected status: %s errors=%v", result.Status, result.Errors)
	}
	if result.Output.Summary != "custom endpoint used" {
		t.Fatalf("unexpected summary: %q", result.Output.Summary)
	}
}

func TestLoadOpenAICompatibleConfigsPrefersLoopbackOnHost(t *testing.T) {
	t.Setenv("GO_CORE_RUNNING_IN_CONTAINER", "false")
	t.Setenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT", "http://host.containers.internal:11434")
	t.Setenv("AI_KERNEL_BASE_URL", "http://host.containers.internal:8012/v1")

	configs := LoadOpenAICompatibleConfigs()

	if got := configs["local"].BaseURL; got != "http://127.0.0.1:11434/v1" {
		t.Fatalf("local BaseURL=%q want %q", got, "http://127.0.0.1:11434/v1")
	}
	if got := configs["local"].ModelsURL(); got != "http://127.0.0.1:11434/v1/models" {
		t.Fatalf("local ModelsURL=%q want %q", got, "http://127.0.0.1:11434/v1/models")
	}
	if got := configs["ai_kernel"].BaseURL; got != "http://127.0.0.1:8012/v1" {
		t.Fatalf("ai_kernel BaseURL=%q want %q", got, "http://127.0.0.1:8012/v1")
	}
	if got := configs["ai_kernel"].ModelsURL(); got != "http://127.0.0.1:8012/v1/models" {
		t.Fatalf("ai_kernel ModelsURL=%q want %q", got, "http://127.0.0.1:8012/v1/models")
	}
}

func TestLoadOpenAICompatibleConfigsKeepsContainerAliasInContainer(t *testing.T) {
	t.Setenv("GO_CORE_RUNNING_IN_CONTAINER", "true")
	t.Setenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT", "http://host.containers.internal:11434")
	t.Setenv("AI_KERNEL_BASE_URL", "http://host.containers.internal:8012/v1")

	configs := LoadOpenAICompatibleConfigs()

	if got := configs["local"].BaseURL; got != "http://host.containers.internal:11434/v1" {
		t.Fatalf("local BaseURL=%q want %q", got, "http://host.containers.internal:11434/v1")
	}
	if got := configs["ai_kernel"].BaseURL; got != "http://host.containers.internal:8012/v1" {
		t.Fatalf("ai_kernel BaseURL=%q want %q", got, "http://host.containers.internal:8012/v1")
	}
}

func TestLoadOpenAICompatibleConfigsSkipsOptionalProvidersWithoutEndpoint(t *testing.T) {
	t.Setenv("MIMO_BASE_URL", "")
	t.Setenv("AI_BRIDGE_MIMO_BASE_URL", "")
	t.Setenv("ANTIGRAVITY_BASE_URL", "")
	t.Setenv("AI_BRIDGE_ANTIGRAVITY_BASE_URL", "")
	t.Setenv("MIMO_MODEL", "")
	t.Setenv("AI_BRIDGE_MIMO_MODEL", "")
	t.Setenv("ANTIGRAVITY_MODEL", "")
	t.Setenv("AI_BRIDGE_ANTIGRAVITY_MODEL", "")
	t.Setenv("MIMO_API_KEY", "")
	t.Setenv("AI_BRIDGE_MIMO_API_KEY", "")
	t.Setenv("ANTIGRAVITY_API_KEY", "")
	t.Setenv("AI_BRIDGE_ANTIGRAVITY_API_KEY", "")

	configs := LoadOpenAICompatibleConfigs()

	if _, ok := configs["mimo"]; ok {
		t.Fatalf("mimo should be absent without explicit endpoint: %#v", configs["mimo"])
	}
	if _, ok := configs["antigravity"]; ok {
		t.Fatalf("antigravity should be absent without explicit endpoint: %#v", configs["antigravity"])
	}
}

func TestLoadOpenAICompatibleConfigsIncludesOptionalProvidersWhenConfigured(t *testing.T) {
	t.Setenv("MIMO_BASE_URL", "https://mimo.example.test/api")
	t.Setenv("MIMO_MODEL", "mimo-reasoner")
	t.Setenv("AI_BRIDGE_MIMO_ENABLED", "true")
	t.Setenv("MIMO_API_KEY", "mimo-secret")
	t.Setenv("ANTIGRAVITY_BASE_URL", "https://antigravity.example.test/inference")
	t.Setenv("ANTIGRAVITY_MODEL", "antigravity-coder")
	t.Setenv("ANTIGRAVITY_API_KEY", "anti-secret")

	configs := LoadOpenAICompatibleConfigs()

	mimo, ok := configs["mimo"]
	if !ok {
		t.Fatal("mimo config missing")
	}
	if !mimo.Configured() {
		t.Fatalf("mimo should be configured: %#v", mimo)
	}
	if mimo.BaseURL != "https://mimo.example.test/api/v1" {
		t.Fatalf("mimo BaseURL=%q want %q", mimo.BaseURL, "https://mimo.example.test/api/v1")
	}
	if mimo.ModelsURL() != "https://mimo.example.test/api/v1/models" {
		t.Fatalf("mimo ModelsURL=%q want %q", mimo.ModelsURL(), "https://mimo.example.test/api/v1/models")
	}

	antigravity, ok := configs["antigravity"]
	if !ok {
		t.Fatal("antigravity config missing")
	}
	if !antigravity.Configured() {
		t.Fatalf("antigravity should be configured: %#v", antigravity)
	}
	if antigravity.BaseURL != "https://antigravity.example.test/inference/v1" {
		t.Fatalf("antigravity BaseURL=%q want %q", antigravity.BaseURL, "https://antigravity.example.test/inference/v1")
	}
	if antigravity.ModelsURL() != "https://antigravity.example.test/inference/v1/models" {
		t.Fatalf("antigravity ModelsURL=%q want %q", antigravity.ModelsURL(), "https://antigravity.example.test/inference/v1/models")
	}
}
