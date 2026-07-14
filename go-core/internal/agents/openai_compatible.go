package agents

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/workspace"
)

const defaultProviderTimeout = 120 * time.Second

type Agent interface {
	Info() domain.AgentInfo
	CanAccept(domain.Task) bool
	Execute(context.Context, domain.Task) domain.AgentResult
}

type AgentDescriptor struct {
	ID           string
	Type         string
	Provider     string
	ModelName    string
	Capabilities []string
	Status       domain.AgentStatus
}

// OpenAICompatibleConfig describes a provider exposing the de-facto standard
// /v1/chat/completions and /v1/models API.
type OpenAICompatibleConfig struct {
	Provider                    string
	ProviderID                  string
	BaseURL                     string
	APIKey                      string
	DefaultModel                string
	ModelsEndpoint              string
	ChatCompletionsEndpoint     string
	ResponsesEndpoint           string
	MessagesEndpoint            string
	MessagesCountTokensEndpoint string
	CodexEndpoint               string
	Timeout                     time.Duration
	RequireKey                  bool
}

func (c OpenAICompatibleConfig) Configured() bool {
	if strings.TrimSpace(c.BaseURL) == "" || strings.TrimSpace(c.DefaultModel) == "" {
		return false
	}
	return !c.RequireKey || strings.TrimSpace(c.APIKey) != ""
}

func (c OpenAICompatibleConfig) EffectiveProviderID() string {
	return firstNonEmpty(c.ProviderID, c.Provider)
}

func (c OpenAICompatibleConfig) ModelsURL() string {
	return firstNonEmpty(strings.TrimSpace(c.ModelsEndpoint), strings.TrimRight(c.BaseURL, "/")+"/models")
}

func (c OpenAICompatibleConfig) ChatCompletionsURL() string {
	return firstNonEmpty(strings.TrimSpace(c.ChatCompletionsEndpoint), strings.TrimRight(c.BaseURL, "/")+"/chat/completions")
}

func (c OpenAICompatibleConfig) ResponsesURL() string {
	if endpoint := strings.TrimSpace(c.ResponsesEndpoint); endpoint != "" {
		return endpoint
	}
	base := strings.TrimRight(c.BaseURL, "/")
	if base == "" {
		return ""
	}
	return base + "/responses"
}

func (c OpenAICompatibleConfig) MessagesURL() string {
	if endpoint := strings.TrimSpace(c.MessagesEndpoint); endpoint != "" {
		return endpoint
	}
	base := strings.TrimRight(c.BaseURL, "/")
	if base == "" {
		return ""
	}
	return base + "/messages"
}

func (c OpenAICompatibleConfig) MessagesCountTokensURL() string {
	if endpoint := strings.TrimSpace(c.MessagesCountTokensEndpoint); endpoint != "" {
		return endpoint
	}
	base := strings.TrimRight(c.BaseURL, "/")
	if base == "" {
		return ""
	}
	return base + "/messages/count_tokens"
}

func (c OpenAICompatibleConfig) CodexURL() string {
	if endpoint := strings.TrimSpace(c.CodexEndpoint); endpoint != "" {
		return endpoint
	}
	return ""
}

type HealthReporter interface {
	Probe(context.Context) domain.ProviderHealth
}

type OpenAICompatibleAgent struct {
	info   AgentDescriptor
	config OpenAICompatibleConfig
	client *http.Client
}

func NewOpenAICompatibleAgent(descriptor AgentDescriptor, config OpenAICompatibleConfig) *OpenAICompatibleAgent {
	if config.Timeout <= 0 {
		config.Timeout = defaultProviderTimeout
	}
	descriptor.Provider = firstNonEmpty(descriptor.Provider, config.Provider)
	descriptor.ModelName = firstNonEmpty(descriptor.ModelName, config.DefaultModel)
	if descriptor.Status == "" {
		if config.Configured() {
			descriptor.Status = domain.AgentStatusReady
		} else {
			descriptor.Status = domain.AgentStatusOffline
		}
	}
	return &OpenAICompatibleAgent{
		info:   descriptor,
		config: config,
		client: &http.Client{Timeout: config.Timeout},
	}
}

func (a *OpenAICompatibleAgent) Info() domain.AgentInfo {
	return domain.AgentInfo{
		ID:           a.info.ID,
		Type:         a.info.Type,
		Provider:     a.info.Provider,
		ModelName:    a.info.ModelName,
		Capabilities: append([]string(nil), a.info.Capabilities...),
		Status:       a.info.Status,
	}
}

func (a *OpenAICompatibleAgent) CanAccept(task domain.Task) bool {
	if !a.config.Configured() {
		return false
	}
	if task.RequiredCapability == "" {
		return true
	}
	for _, capability := range a.info.Capabilities {
		if capability == task.RequiredCapability {
			return true
		}
	}
	return false
}

func (a *OpenAICompatibleAgent) Execute(ctx context.Context, task domain.Task) domain.AgentResult {
	startedAt := time.Now()
	result := domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     a.info.ID,
		Status:      domain.TaskStatusFailed,
		Provider:    a.info.Provider,
		ModelName:   firstNonEmpty(task.AssignedModel, a.info.ModelName, a.config.DefaultModel),
		CompletedAt: time.Now().UTC(),
		Output: domain.ResultOutput{Artifacts: map[string]any{
			"runtime":   "go",
			"transport": "openai_compatible_chat_completions",
		}},
	}
	if !a.config.Configured() {
		result.Errors = []string{"provider is not configured"}
		result.Output.Summary = "provider is not configured"
		return result
	}

	var runtime *workspace.Runtime
	if toolRuntimeEnabled(task) {
		var err error
		runtime, err = workspace.New(task.Context.RepoPath)
		if err != nil {
			return failedResult(result, startedAt, err)
		}
	}

	completion, callCount, err := a.runChatCompletionLoop(ctx, task, result.ModelName, runtime)
	if err != nil {
		return failedResult(result, startedAt, err)
	}
	if len(completion.Choices) == 0 {
		return failedResult(result, startedAt, errors.New("provider returned no choices"))
	}
	summary := strings.TrimSpace(chatMessageText(completion.Choices[0].Message))
	if summary == "" {
		return failedResult(result, startedAt, errors.New("provider returned an empty completion"))
	}

	result.Status = domain.TaskStatusDone
	result.Confidence = 0.85
	result.CompletedAt = time.Now().UTC()
	result.Output.Summary = summary
	result.Output.Artifacts["completion_id"] = completion.ID
	result.Output.Artifacts["finish_reason"] = completion.Choices[0].FinishReason
	result.Output.Artifacts["duration_ms"] = time.Since(startedAt).Milliseconds()
	result.Output.Artifacts["usage"] = completion.Usage
	result.Output.Artifacts["tool_call_count"] = callCount
	if runtime != nil {
		filesChanged, commandsRun, testResults := runtime.Snapshot()
		result.Output.FilesChanged = filesChanged
		result.Output.CommandsRun = append(result.Output.CommandsRun, commandsRun...)
		result.Output.TestResults = testResults
		result.Output.Artifacts["workspace_root"] = runtime.Root()
		result.Output.Artifacts["runtime_tools_enabled"] = true
	} else {
		result.Output.CommandsRun = []string{"POST " + a.config.ChatCompletionsURL()}
		result.Output.Artifacts["runtime_tools_enabled"] = false
	}
	return result
}

func (a *OpenAICompatibleAgent) Probe(ctx context.Context) domain.ProviderHealth {
	health := domain.ProviderHealth{
		Provider:   a.info.Provider,
		Configured: a.config.Configured(),
		Status:     "unavailable",
		ObservedAt: time.Now().UTC(),
		BaseURL:    sanitizedBaseURL(a.config.BaseURL),
	}
	if !health.Configured {
		health.Status = "not_configured"
		health.Error = "provider credentials or endpoint are not configured"
		return health
	}
	endpoint := a.config.ModelsURL()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		health.Error = err.Error()
		return health
	}
	if key := strings.TrimSpace(a.config.APIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	response, err := a.client.Do(req)
	if err != nil {
		health.Error = err.Error()
		return health
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		health.Error = providerHTTPError(response.StatusCode, body).Error()
		return health
	}
	health.Available = true
	health.Status = "ready"
	return health
}

type chatMessage struct {
	Role       string         `json:"role"`
	Content    any            `json:"content,omitempty"`
	Name       string         `json:"name,omitempty"`
	ToolCallID string         `json:"tool_call_id,omitempty"`
	ToolCalls  []chatToolCall `json:"tool_calls,omitempty"`
}

type chatCompletionRequest struct {
	Model       string        `json:"model"`
	Messages    []chatMessage `json:"messages"`
	Temperature float64       `json:"temperature,omitempty"`
	Tools       []chatTool    `json:"tools,omitempty"`
	ToolChoice  string        `json:"tool_choice,omitempty"`
}

type chatTool struct {
	Type     string           `json:"type"`
	Function chatToolFunction `json:"function"`
}

type chatToolFunction struct {
	Name        string         `json:"name"`
	Description string         `json:"description,omitempty"`
	Parameters  map[string]any `json:"parameters,omitempty"`
}

type chatToolCall struct {
	ID       string               `json:"id"`
	Type     string               `json:"type"`
	Function chatToolFunctionCall `json:"function"`
}

type chatToolFunctionCall struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments,omitempty"`
}

type chatCompletionResponse struct {
	ID      string `json:"id"`
	Choices []struct {
		Message      chatMessage `json:"message"`
		FinishReason string      `json:"finish_reason"`
	} `json:"choices"`
	Usage map[string]any `json:"usage,omitempty"`
}

func systemPrompt(task domain.Task) string {
	return "You are an execution agent in SourceVCode Orchestrator. Return a concrete, truthful result for a " +
		string(task.Type) + " task. Respect constraints and acceptance criteria. Use workspace tools when you need to inspect or modify files or run safe repo commands. Do not claim files or commands were changed unless the supplied runtime actually gave you tools to do so."
}

func taskPrompt(task domain.Task) string {
	var builder strings.Builder
	builder.WriteString("Task: ")
	builder.WriteString(task.Input.Description)
	if task.Context.Project != "" {
		builder.WriteString("\nProject: ")
		builder.WriteString(task.Context.Project)
	}
	if task.Context.RepoPath != "" {
		builder.WriteString("\nRepository: ")
		builder.WriteString(task.Context.RepoPath)
	}
	if len(task.Input.Files) > 0 {
		builder.WriteString("\nFiles: ")
		builder.WriteString(strings.Join(task.Input.Files, ", "))
	}
	if len(task.Input.Constraints) > 0 {
		builder.WriteString("\nConstraints:\n- ")
		builder.WriteString(strings.Join(task.Input.Constraints, "\n- "))
	}
	if len(task.Input.AcceptanceCriteria) > 0 {
		builder.WriteString("\nAcceptance criteria:\n- ")
		builder.WriteString(strings.Join(task.Input.AcceptanceCriteria, "\n- "))
	}
	return builder.String()
}

func failedResult(result domain.AgentResult, startedAt time.Time, err error) domain.AgentResult {
	result.Status = domain.TaskStatusFailed
	result.Confidence = 0
	result.CompletedAt = time.Now().UTC()
	result.Errors = []string{err.Error()}
	result.Output.Summary = err.Error()
	result.Output.Artifacts["duration_ms"] = time.Since(startedAt).Milliseconds()
	return result
}

func (a *OpenAICompatibleAgent) runChatCompletionLoop(ctx context.Context, task domain.Task, model string, runtime *workspace.Runtime) (chatCompletionResponse, int, error) {
	messages := []chatMessage{
		{Role: "system", Content: systemPrompt(task)},
		{Role: "user", Content: taskPrompt(task)},
	}
	tools := workspaceTools()
	if runtime == nil {
		tools = nil
	}
	callCount := 0
	for iteration := 0; iteration < 8; iteration++ {
		payload := chatCompletionRequest{
			Model:       model,
			Messages:    messages,
			Temperature: 0.2,
			Tools:       tools,
		}
		if len(tools) > 0 {
			payload.ToolChoice = "auto"
		}
		completion, err := a.doChatCompletionRequest(ctx, payload)
		if err != nil {
			return chatCompletionResponse{}, callCount, err
		}
		if len(completion.Choices) == 0 {
			return chatCompletionResponse{}, callCount, errors.New("provider returned no choices")
		}
		message := completion.Choices[0].Message
		if len(message.ToolCalls) == 0 {
			return completion, callCount, nil
		}
		if runtime == nil {
			return chatCompletionResponse{}, callCount, errors.New("provider requested tools but runtime is disabled")
		}
		callCount += len(message.ToolCalls)
		messages = append(messages, chatMessage{
			Role:      "assistant",
			Content:   message.Content,
			ToolCalls: message.ToolCalls,
		})
		for _, call := range message.ToolCalls {
			output, err := executeWorkspaceTool(runtime, call)
			if err != nil {
				output = map[string]any{"error": err.Error()}
			}
			serialized, err := json.Marshal(output)
			if err != nil {
				return chatCompletionResponse{}, callCount, err
			}
			messages = append(messages, chatMessage{
				Role:       "tool",
				ToolCallID: call.ID,
				Name:       call.Function.Name,
				Content:    string(serialized),
			})
		}
	}
	return chatCompletionResponse{}, callCount, errors.New("tool loop exceeded max iterations")
}

func (a *OpenAICompatibleAgent) doChatCompletionRequest(ctx context.Context, payload chatCompletionRequest) (chatCompletionResponse, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return chatCompletionResponse{}, err
	}
	endpoint := a.config.ChatCompletionsURL()
	if endpoint == "" {
		return chatCompletionResponse{}, fmt.Errorf("chat completions endpoint is not configured")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return chatCompletionResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	if key := strings.TrimSpace(a.config.APIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	response, err := a.client.Do(req)
	if err != nil {
		return chatCompletionResponse{}, fmt.Errorf("%s request failed: %w", a.info.Provider, err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return chatCompletionResponse{}, fmt.Errorf("read %s response: %w", a.info.Provider, err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return chatCompletionResponse{}, providerHTTPError(response.StatusCode, responseBody)
	}
	var completion chatCompletionResponse
	if err := json.Unmarshal(responseBody, &completion); err != nil {
		return chatCompletionResponse{}, fmt.Errorf("decode %s response: %w", a.info.Provider, err)
	}
	return completion, nil
}

func toolRuntimeEnabled(task domain.Task) bool {
	if task.Context.RepoPath != "" {
		return true
	}
	switch task.Type {
	case domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeReview, domain.TaskTypeTest:
		return true
	default:
		return false
	}
}

func workspaceTools() []chatTool {
	return []chatTool{
		{
			Type: "function",
			Function: chatToolFunction{
				Name:        "list_files",
				Description: "List files under the workspace root or a relative subdirectory.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"path":  map[string]any{"type": "string"},
						"limit": map[string]any{"type": "integer"},
					},
				},
			},
		},
		{
			Type: "function",
			Function: chatToolFunction{
				Name:        "read_file",
				Description: "Read a workspace file, optionally by line range.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"path":       map[string]any{"type": "string"},
						"start_line": map[string]any{"type": "integer"},
						"end_line":   map[string]any{"type": "integer"},
					},
					"required": []string{"path"},
				},
			},
		},
		{
			Type: "function",
			Function: chatToolFunction{
				Name:        "write_file",
				Description: "Write a full file under the workspace root.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"path":    map[string]any{"type": "string"},
						"content": map[string]any{"type": "string"},
					},
					"required": []string{"path", "content"},
				},
			},
		},
		{
			Type: "function",
			Function: chatToolFunction{
				Name:        "run_command",
				Description: "Run a safe allowlisted command inside the workspace using argv form, not a shell string.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"command":         map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
						"cwd":             map[string]any{"type": "string"},
						"timeout_seconds": map[string]any{"type": "integer"},
					},
					"required": []string{"command"},
				},
			},
		},
	}
}

func executeWorkspaceTool(runtime *workspace.Runtime, call chatToolCall) (map[string]any, error) {
	var arguments map[string]any
	if strings.TrimSpace(call.Function.Arguments) != "" {
		if err := json.Unmarshal([]byte(call.Function.Arguments), &arguments); err != nil {
			return nil, fmt.Errorf("invalid tool arguments for %s: %w", call.Function.Name, err)
		}
	}
	switch call.Function.Name {
	case "list_files":
		return runtime.ListFiles(stringArg(arguments, "path"), intArg(arguments, "limit"))
	case "read_file":
		return runtime.ReadFile(stringArg(arguments, "path"), intArg(arguments, "start_line"), intArg(arguments, "end_line"))
	case "write_file":
		return runtime.WriteFile(stringArg(arguments, "path"), stringArg(arguments, "content"))
	case "run_command":
		return runtime.RunCommand(stringSliceArg(arguments, "command"), stringArg(arguments, "cwd"), intArg(arguments, "timeout_seconds"))
	default:
		return nil, fmt.Errorf("unsupported tool call: %s", call.Function.Name)
	}
}

func chatMessageText(message chatMessage) string {
	switch value := message.Content.(type) {
	case string:
		return value
	case []any:
		parts := make([]string, 0, len(value))
		for _, item := range value {
			row, ok := item.(map[string]any)
			if !ok {
				continue
			}
			if rowType, _ := row["type"].(string); rowType == "text" {
				if text, _ := row["text"].(string); strings.TrimSpace(text) != "" {
					parts = append(parts, text)
				}
			}
		}
		return strings.Join(parts, "\n")
	default:
		return ""
	}
}

func stringArg(arguments map[string]any, key string) string {
	if arguments == nil {
		return ""
	}
	value, _ := arguments[key].(string)
	return value
}

func intArg(arguments map[string]any, key string) int {
	if arguments == nil {
		return 0
	}
	switch value := arguments[key].(type) {
	case float64:
		return int(value)
	case int:
		return value
	case json.Number:
		number, _ := value.Int64()
		return int(number)
	default:
		return 0
	}
}

func stringSliceArg(arguments map[string]any, key string) []string {
	if arguments == nil {
		return nil
	}
	raw, ok := arguments[key].([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
			out = append(out, text)
		}
	}
	return out
}

func providerHTTPError(status int, body []byte) error {
	message := strings.TrimSpace(string(body))
	var payload struct {
		Error any `json:"error"`
	}
	if json.Unmarshal(body, &payload) == nil && payload.Error != nil {
		switch value := payload.Error.(type) {
		case string:
			message = value
		case map[string]any:
			if text, ok := value["message"].(string); ok {
				message = text
			}
		}
	}
	if len(message) > 1000 {
		message = message[:1000]
	}
	return fmt.Errorf("provider returned HTTP %d: %s", status, message)
}

func sanitizedBaseURL(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	parsed.User = nil
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

// LoadOpenAICompatibleConfigs ports provider environment aliases from Python.
func LoadOpenAICompatibleConfigs() map[string]OpenAICompatibleConfig {
	timeout := durationFromEnv("GO_CORE_PROVIDER_TIMEOUT", defaultProviderTimeout)
	withDefaults := func(provider string, cfg OpenAICompatibleConfig) OpenAICompatibleConfig {
		cfg.Provider = provider
		cfg.ProviderID = firstNonEmpty(cfg.ProviderID, provider)
		cfg = normalizeLoopbackProviderConfig(provider, cfg)
		cfg.BaseURL = normalizeV1Base(cfg.BaseURL, cfg.BaseURL)
		if cfg.ModelsEndpoint == "" && cfg.BaseURL != "" {
			cfg.ModelsEndpoint = strings.TrimRight(cfg.BaseURL, "/") + "/models"
		}
		if cfg.ChatCompletionsEndpoint == "" && cfg.BaseURL != "" {
			cfg.ChatCompletionsEndpoint = strings.TrimRight(cfg.BaseURL, "/") + "/chat/completions"
		}
		if cfg.ResponsesEndpoint == "" && cfg.BaseURL != "" {
			cfg.ResponsesEndpoint = strings.TrimRight(cfg.BaseURL, "/") + "/responses"
		}
		if cfg.MessagesEndpoint == "" && cfg.BaseURL != "" {
			cfg.MessagesEndpoint = strings.TrimRight(cfg.BaseURL, "/") + "/messages"
		}
		if cfg.MessagesCountTokensEndpoint == "" && cfg.BaseURL != "" {
			cfg.MessagesCountTokensEndpoint = strings.TrimRight(cfg.BaseURL, "/") + "/messages/count_tokens"
		}
		cfg.Timeout = timeout
		return cfg
	}
	configs := map[string]OpenAICompatibleConfig{
		"local": withDefaults("local", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_LOCAL_LLM_PROVIDER_ID", "OLLAMA_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("http://127.0.0.1:11434", "AI_BRIDGE_LOCAL_LLM_ENDPOINT", "OLLAMA_BASE_URL", "OLLAMA_HOST"),
			APIKey:                  firstEnv("AI_BRIDGE_LOCAL_LLM_API_KEY", "OLLAMA_API_KEY"),
			DefaultModel:            firstEnvDefault("qwen2.5:32b-instruct-q4_k_m", "AI_BRIDGE_LOCAL_LLM_MODEL", "OLLAMA_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_LOCAL_LLM_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_LOCAL_LLM_CHAT_COMPLETIONS_ENDPOINT"),
		}),
		"ai_kernel": withDefaults("ai_kernel", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_AI_KERNEL_PROVIDER_ID", "AI_KERNEL_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("http://127.0.0.1:8012/v1", "AI_KERNEL_BASE_URL", "AI_BRIDGE_AI_KERNEL_BASE_URL"),
			APIKey:                  firstEnv("AI_KERNEL_API_KEY", "AI_BRIDGE_AI_KERNEL_API_KEY"),
			DefaultModel:            firstEnvDefault("hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m", "AI_KERNEL_MODEL_ALIAS", "AI_BRIDGE_AI_KERNEL_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_AI_KERNEL_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_AI_KERNEL_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              envBool("AI_KERNEL_REQUIRE_API_KEY", true),
		}),
		"openai": withDefaults("openai", OpenAICompatibleConfig{
			ProviderID:                  firstEnv("AI_BRIDGE_OPENAI_PROVIDER_ID", "CODEX_SALE_PROVIDER_ID"),
			BaseURL:                     firstEnvDefault("https://api.openai.com/v1", "OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL", "CODEX_SALE_BASE_URL"),
			APIKey:                      firstEnv("OPENAI_API_KEY", "CODEX_SALE_API_KEY"),
			DefaultModel:                firstEnvDefault("gpt-5.5", "CODEX_OPENAI_MODEL", "OPENAI_DEFAULT_MODEL"),
			ModelsEndpoint:              firstEnv("AI_BRIDGE_OPENAI_MODELS_ENDPOINT", "CODEX_SALE_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint:     firstEnv("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT", "CODEX_SALE_CHAT_COMPLETIONS_ENDPOINT"),
			ResponsesEndpoint:           firstEnv("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT", "CODEX_SALE_RESPONSES_ENDPOINT"),
			MessagesEndpoint:            firstEnv("AI_BRIDGE_OPENAI_MESSAGES_ENDPOINT", "CODEX_SALE_MESSAGES_ENDPOINT"),
			MessagesCountTokensEndpoint: firstEnv("AI_BRIDGE_OPENAI_MESSAGES_COUNT_TOKENS_ENDPOINT", "CODEX_SALE_MESSAGES_COUNT_TOKENS_ENDPOINT"),
			CodexEndpoint:               firstEnv("AI_BRIDGE_OPENAI_CODEX_ENDPOINT", "CODEX_SALE_CODEX_ENDPOINT"),
			RequireKey:                  true,
		}),
		"mistral": withDefaults("mistral", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_MISTRAL_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("https://api.mistral.ai/v1", "MISTRAL_BASE_URL", "AI_BRIDGE_MISTRAL_BASE_URL"),
			APIKey:                  firstEnv("MISTRAL_API_KEY", "AI_BRIDGE_MISTRAL_API_KEY"),
			DefaultModel:            firstEnvDefault("mistral-large-latest", "MISTRAL_MODEL", "AI_BRIDGE_MISTRAL_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_MISTRAL_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_MISTRAL_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              true,
		}),
		"antigravity": withDefaults("antigravity", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_ANTIGRAVITY_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("https://api.antigravity.example/v1", "ANTIGRAVITY_BASE_URL", "AI_BRIDGE_ANTIGRAVITY_BASE_URL"),
			APIKey:                  firstEnv("ANTIGRAVITY_API_KEY", "AI_BRIDGE_ANTIGRAVITY_API_KEY"),
			DefaultModel:            firstEnvDefault("antigravity-coder", "ANTIGRAVITY_MODEL", "AI_BRIDGE_ANTIGRAVITY_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_ANTIGRAVITY_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_ANTIGRAVITY_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              firstEnv("ANTIGRAVITY_API_KEY", "AI_BRIDGE_ANTIGRAVITY_API_KEY") != "",
		}),
		"mimo": withDefaults("mimo", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_MIMO_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("https://api.mimo.example/v1", "MIMO_BASE_URL", "AI_BRIDGE_MIMO_BASE_URL"),
			APIKey:                  firstEnv("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY"),
			DefaultModel:            firstEnvDefault("mimo-coder", "MIMO_MODEL", "AI_BRIDGE_MIMO_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_MIMO_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_MIMO_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              envBool("AI_BRIDGE_MIMO_ENABLED", false),
		}),
	}
	for key, cfg := range configs {
		if strings.TrimSpace(cfg.BaseURL) == "" {
			delete(configs, key)
		}
	}
	return configs
}

func normalizeLoopbackProviderConfig(provider string, cfg OpenAICompatibleConfig) OpenAICompatibleConfig {
	if runsInContainer() || !usesLocalLoopbackProvider(provider) {
		return cfg
	}
	cfg.BaseURL = rewriteHostContainersInternal(cfg.BaseURL)
	cfg.ModelsEndpoint = rewriteHostContainersInternal(cfg.ModelsEndpoint)
	cfg.ChatCompletionsEndpoint = rewriteHostContainersInternal(cfg.ChatCompletionsEndpoint)
	cfg.ResponsesEndpoint = rewriteHostContainersInternal(cfg.ResponsesEndpoint)
	cfg.MessagesEndpoint = rewriteHostContainersInternal(cfg.MessagesEndpoint)
	cfg.MessagesCountTokensEndpoint = rewriteHostContainersInternal(cfg.MessagesCountTokensEndpoint)
	cfg.CodexEndpoint = rewriteHostContainersInternal(cfg.CodexEndpoint)
	return cfg
}

func usesLocalLoopbackProvider(provider string) bool {
	switch provider {
	case "local", "ai_kernel":
		return true
	default:
		return false
	}
}

func runsInContainer() bool {
	if value := strings.TrimSpace(os.Getenv("GO_CORE_RUNNING_IN_CONTAINER")); value != "" {
		parsed, err := strconv.ParseBool(value)
		return err == nil && parsed
	}
	if strings.TrimSpace(os.Getenv("container")) != "" {
		return true
	}
	if _, err := os.Stat("/run/.containerenv"); err == nil {
		return true
	}
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return true
	}
	return false
}

func rewriteHostContainersInternal(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	return strings.ReplaceAll(value, "host.containers.internal", "127.0.0.1")
}

func normalizeV1Base(value, fallback string) string {
	value = strings.TrimRight(strings.TrimSpace(firstNonEmpty(value, fallback)), "/")
	if value == "" || strings.HasSuffix(value, "/v1") {
		return value
	}
	return value + "/v1"
}

func firstEnv(keys ...string) string {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}

func firstEnvDefault(fallback string, keys ...string) string {
	return firstNonEmpty(firstEnv(keys...), fallback)
}

func envBool(key string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	return err == nil && parsed
}

func durationFromEnv(key string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}
