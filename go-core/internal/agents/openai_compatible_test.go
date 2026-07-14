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
