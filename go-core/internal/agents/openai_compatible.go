package agents

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/providerhttp"
	"sourcevcode-orchestrator/go-core/internal/workspace"
)

const defaultProviderTimeout = 120 * time.Second

type Agent interface {
	Info() domain.AgentInfo
	CanAccept(domain.Task) bool
	Execute(context.Context, domain.Task) domain.AgentResult
}

type StreamingAgent interface {
	Agent
	ExecuteStream(ctx context.Context, task domain.Task) (<-chan domain.AgentDelta, <-chan domain.AgentResult, error)
}

type RuntimeCapabilityReporter interface {
	RuntimeCapabilities() domain.ProviderRuntimeCapabilities
}

type AssignedModelOverrider interface {
	SupportsAssignedModelOverride() bool
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
	NativeStreaming             bool
	AllowPseudoRealtime         bool
}

func (c OpenAICompatibleConfig) Configured() bool {
	if strings.TrimSpace(c.BaseURL) == "" || strings.TrimSpace(c.DefaultModel) == "" {
		return false
	}
	return !c.RequireKey || strings.TrimSpace(c.APIKey) != ""
}

func (c OpenAICompatibleConfig) SupportsNativeStreaming() bool {
	return c.NativeStreaming
}

func (c OpenAICompatibleConfig) SupportsPseudoRealtime() bool {
	return c.AllowPseudoRealtime
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

func looksLikeCodexSaleEndpoint(value string) bool {
	trimmed := strings.TrimSpace(strings.ToLower(value))
	return trimmed != "" && strings.Contains(trimmed, "codex.sale")
}

type realtimeMetricsRecorder struct {
	startedAt       time.Time
	transport       string
	nativeStreaming bool
	pseudoRealtime  bool
	firstTokenAt    time.Time
	firstToolAt     time.Time
	firstPatchAt    time.Time
	firstResultAt   time.Time
	firstTestAt     time.Time
	tokensStreamed  int
	toolsExecuted   int
	patchesApplied  int
	testsExecuted   int
}

func newRealtimeMetricsRecorder(startedAt time.Time, transport string, nativeStreaming bool, pseudoRealtime bool) *realtimeMetricsRecorder {
	return &realtimeMetricsRecorder{startedAt: startedAt, transport: transport, nativeStreaming: nativeStreaming, pseudoRealtime: pseudoRealtime}
}

func openAITransportMode(native bool, pseudo bool) domain.RuntimeTransportMode {
	if native {
		return domain.RuntimeTransportNativeStream
	}
	if pseudo {
		return domain.RuntimeTransportPseudoRealtime
	}
	return domain.RuntimeTransportBuffered
}

func providerRuntimeMaxParallelRequests() int {
	for _, key := range []string{
		"GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL",
		"AI_BRIDGE_PROVIDER_MAX_CONCURRENT_PER_MODEL",
		"GO_CORE_PROVIDER_MAX_CONCURRENT",
		"AI_BRIDGE_PROVIDER_MAX_CONCURRENT",
	} {
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err == nil && parsed > 0 {
			return parsed
		}
	}
	return 1
}

func (r *realtimeMetricsRecorder) markToken(at time.Time) {
	if r == nil {
		return
	}
	if r.firstTokenAt.IsZero() {
		r.firstTokenAt = at
	}
	r.tokensStreamed++
}

func (r *realtimeMetricsRecorder) markTool(at time.Time) {
	if r == nil {
		return
	}
	if r.firstToolAt.IsZero() {
		r.firstToolAt = at
	}
	r.toolsExecuted++
}

func (r *realtimeMetricsRecorder) markPatch(at time.Time) {
	if r == nil {
		return
	}
	if r.firstPatchAt.IsZero() {
		r.firstPatchAt = at
	}
	r.patchesApplied++
}

func (r *realtimeMetricsRecorder) markResult(at time.Time) {
	if r == nil {
		return
	}
	if r.firstResultAt.IsZero() {
		r.firstResultAt = at
	}
}

func (r *realtimeMetricsRecorder) markTest(at time.Time) {
	if r == nil {
		return
	}
	if r.firstTestAt.IsZero() {
		r.firstTestAt = at
	}
	r.testsExecuted++
}

func (r *realtimeMetricsRecorder) snapshot(completedAt time.Time) map[string]any {
	if r == nil {
		return nil
	}
	metrics := map[string]any{
		"transport":           r.transport,
		"native_streaming":    r.nativeStreaming,
		"pseudo_realtime":     r.pseudoRealtime,
		"total_completion_ms": completedAt.Sub(r.startedAt).Milliseconds(),
		"tokens_streamed":     r.tokensStreamed,
		"tools_executed":      r.toolsExecuted,
		"patches_applied":     r.patchesApplied,
		"tests_executed":      r.testsExecuted,
	}
	if !r.firstTokenAt.IsZero() {
		metrics["time_to_first_token_ms"] = r.firstTokenAt.Sub(r.startedAt).Milliseconds()
	}
	if !r.firstToolAt.IsZero() {
		metrics["time_to_first_tool_ms"] = r.firstToolAt.Sub(r.startedAt).Milliseconds()
	}
	if !r.firstPatchAt.IsZero() {
		metrics["time_to_first_patch_ms"] = r.firstPatchAt.Sub(r.startedAt).Milliseconds()
	}
	if !r.firstResultAt.IsZero() {
		metrics["time_to_first_result_ms"] = r.firstResultAt.Sub(r.startedAt).Milliseconds()
	}
	if !r.firstTestAt.IsZero() {
		metrics["time_to_first_test_ms"] = r.firstTestAt.Sub(r.startedAt).Milliseconds()
	}
	return metrics
}

func attachRealtimeMetrics(artifacts map[string]any, recorder *realtimeMetricsRecorder, completedAt time.Time) {
	if artifacts == nil || recorder == nil {
		return
	}
	artifacts["realtime_metrics"] = recorder.snapshot(completedAt)
}

func LooksLikeCodexSaleAlias(cfg OpenAICompatibleConfig) bool {
	if strings.EqualFold(strings.TrimSpace(cfg.ProviderID), "codexsale") {
		return true
	}
	for _, endpoint := range []string{
		cfg.BaseURL,
		cfg.ModelsEndpoint,
		cfg.ChatCompletionsEndpoint,
		cfg.ResponsesEndpoint,
		cfg.MessagesEndpoint,
		cfg.MessagesCountTokensEndpoint,
		cfg.CodexEndpoint,
	} {
		if looksLikeCodexSaleEndpoint(endpoint) {
			return true
		}
	}
	return false
}

func CloudProviderPreference() string {
	preference := strings.ToLower(strings.TrimSpace(firstEnv("AI_BRIDGE_CLOUD_PROVIDER", "GO_CORE_CLOUD_PROVIDER")))
	if preference == "" {
		return "auto"
	}
	return preference
}

func SelectCloudProvider(configs map[string]OpenAICompatibleConfig, preference string) string {
	preference = strings.ToLower(strings.TrimSpace(preference))
	if preference != "" && preference != "auto" {
		if cfg, ok := configs[preference]; ok && cfg.Configured() {
			return preference
		}
	}

	openaiCfg, openaiOK := configs["openai"]
	codexCfg, codexOK := configs["codexsale"]
	openaiReady := openaiOK && openaiCfg.Configured()
	codexReady := codexOK && codexCfg.Configured()
	if codexReady && (!openaiReady || LooksLikeCodexSaleAlias(openaiCfg)) {
		return "codexsale"
	}
	if openaiReady {
		return "openai"
	}
	if codexReady {
		return "codexsale"
	}

	orderedClouds := []string{"mistral", "mimo", "antigravity"}
	for _, provider := range orderedClouds {
		if cfg, ok := configs[provider]; ok && cfg.Configured() {
			return provider
		}
	}
	for provider, cfg := range configs {
		if strings.EqualFold(provider, "local") || strings.EqualFold(provider, "ai_kernel") {
			continue
		}
		if cfg.Configured() {
			return provider
		}
	}
	return "openai"
}

func PreferredCloudProvider(configs map[string]OpenAICompatibleConfig) string {
	return SelectCloudProvider(configs, CloudProviderPreference())
}

type HealthReporter interface {
	Probe(context.Context) domain.ProviderHealth
}

type OpenAICompatibleAgent struct {
	info   AgentDescriptor
	config OpenAICompatibleConfig
	client *http.Client
}

type providerHTTPResponse struct {
	statusCode int
	header     http.Header
	body       []byte
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

func (a *OpenAICompatibleAgent) SupportsAssignedModelOverride() bool {
	return true
}

func (a *OpenAICompatibleAgent) RuntimeCapabilities() domain.ProviderRuntimeCapabilities {
	native := a.config.SupportsNativeStreaming()
	pseudo := !native && a.config.SupportsPseudoRealtime()
	streaming := native || pseudo
	return domain.ProviderRuntimeCapabilities{
		Connected:            false,
		NativeStreaming:      native,
		ToolStreaming:        streaming,
		PatchStreaming:       streaming,
		TestStreaming:        streaming,
		MaxParallelRequests:  providerRuntimeMaxParallelRequests(),
		SupportsCancellation: true,
		TransportMode:        openAITransportMode(native, pseudo),
		ObservedAt:           time.Now().UTC(),
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

func (a *OpenAICompatibleAgent) ExecuteStream(ctx context.Context, task domain.Task) (<-chan domain.AgentDelta, <-chan domain.AgentResult, error) {
	deltas := make(chan domain.AgentDelta, 32)
	results := make(chan domain.AgentResult, 1)

	go func() {
		defer close(deltas)
		defer close(results)

		nativeStreaming := a.shouldUseNativeStreaming(task)
		pseudoRealtime := !nativeStreaming && a.config.SupportsPseudoRealtime()
		transport := string(openAITransportMode(nativeStreaming, pseudoRealtime))
		deltas <- domain.AgentDelta{
			TaskID:    task.ID,
			AgentID:   a.info.ID,
			Provider:  a.info.Provider,
			ModelName: firstNonEmpty(task.AssignedModel, a.info.ModelName, a.config.DefaultModel),
			Kind:      domain.AgentDeltaStarted,
			Metadata: map[string]any{
				"transport":            transport,
				"native_streaming":     nativeStreaming,
				"pseudo_realtime":      pseudoRealtime,
				"workspace_compatible": nativeStreaming,
			},
			Timestamp: time.Now().UTC(),
		}

		if !nativeStreaming {
			a.emitBufferedExecution(ctx, task, deltas, results)
			return
		}

		result, err := a.executeNativeStream(ctx, task, deltas)
		if err != nil {
			if a.config.SupportsPseudoRealtime() {
				a.emitBufferedExecution(ctx, task, deltas, results)
				return
			}
			results <- result
			return
		}
		results <- result
	}()

	return deltas, results, nil
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
			"transport": string(domain.RuntimeTransportBuffered),
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
	durationMS := time.Since(startedAt).Milliseconds()
	result.Output.Artifacts["duration_ms"] = durationMS
	result.Output.Artifacts["realtime_metrics"] = map[string]any{
		"transport":               string(domain.RuntimeTransportBuffered),
		"native_streaming":        false,
		"pseudo_realtime":         false,
		"time_to_first_result_ms": durationMS,
		"total_completion_ms":     durationMS,
	}
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
		caps := a.RuntimeCapabilities()
		caps.Connected = false
		caps.ObservedAt = health.ObservedAt
		health.RuntimeCapabilities = &caps
		return health
	}
	endpoint := a.config.ModelsURL()
	response, err := a.doProviderRequest(ctx, http.MethodGet, endpoint, nil, "", 4096)
	if err != nil {
		health.Error = err.Error()
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
			health.Status = "degraded"
		}
		caps := a.RuntimeCapabilities()
		caps.Connected = false
		caps.ObservedAt = health.ObservedAt
		health.RuntimeCapabilities = &caps
		return health
	}
	if response.statusCode < 200 || response.statusCode >= 300 {
		health.Error = providerHTTPError(response.statusCode, response.body).Error()
		if providerResponseLooksRateLimited(response.statusCode, response.body) {
			health.Status = "rate_limited"
			return health
		}
		switch response.statusCode {
		case http.StatusTooManyRequests:
			health.Status = "rate_limited"
		case http.StatusInternalServerError, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			health.Status = "degraded"
		}
		caps := a.RuntimeCapabilities()
		caps.Connected = false
		caps.ObservedAt = health.ObservedAt
		health.RuntimeCapabilities = &caps
		return health
	}
	health.Available = true
	health.Status = "ready"
	caps := a.RuntimeCapabilities()
	caps.Connected = true
	caps.ObservedAt = health.ObservedAt
	health.RuntimeCapabilities = &caps
	return health
}

func providerResponseLooksRateLimited(statusCode int, body []byte) bool {
	if statusCode == http.StatusTooManyRequests {
		return true
	}
	normalized := strings.ToLower(strings.TrimSpace(string(body)))
	if normalized == "" {
		return false
	}
	for _, token := range []string{"service_busy", "rate_limit_error", "rate limit", "retry after", "too many requests"} {
		if strings.Contains(normalized, token) {
			return true
		}
	}
	return false
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
	Stream      bool          `json:"stream,omitempty"`
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

type chatCompletionStreamToolCallFunctionDelta struct {
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

type chatCompletionStreamToolCallDelta struct {
	Index    int                                       `json:"index,omitempty"`
	ID       string                                    `json:"id,omitempty"`
	Type     string                                    `json:"type,omitempty"`
	Function chatCompletionStreamToolCallFunctionDelta `json:"function,omitempty"`
}

type chatCompletionStreamDelta struct {
	Role      string                              `json:"role,omitempty"`
	Content   string                              `json:"content,omitempty"`
	ToolCalls []chatCompletionStreamToolCallDelta `json:"tool_calls,omitempty"`
}

type chatCompletionStreamChunk struct {
	ID      string `json:"id"`
	Choices []struct {
		Delta        chatCompletionStreamDelta `json:"delta"`
		FinishReason string                    `json:"finish_reason"`
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
	durationMS := time.Since(startedAt).Milliseconds()
	result.Output.Artifacts["duration_ms"] = durationMS
	result.Output.Artifacts["realtime_metrics"] = map[string]any{
		"transport":               string(domain.RuntimeTransportBuffered),
		"native_streaming":        false,
		"pseudo_realtime":         false,
		"time_to_first_result_ms": durationMS,
		"total_completion_ms":     durationMS,
	}
	return result
}

func (a *OpenAICompatibleAgent) shouldUseNativeStreaming(task domain.Task) bool {
	return a.config.SupportsNativeStreaming()
}
func (a *OpenAICompatibleAgent) emitBufferedExecution(ctx context.Context, task domain.Task, deltas chan<- domain.AgentDelta, results chan<- domain.AgentResult) {
	result := a.Execute(ctx, task)
	sequence := int64(1)
	if summary := strings.TrimSpace(result.Output.Summary); summary != "" {
		deltas <- domain.AgentDelta{
			TaskID:    task.ID,
			AgentID:   a.info.ID,
			Provider:  result.Provider,
			ModelName: result.ModelName,
			Kind:      domain.AgentDeltaPartialResult,
			Sequence:  sequence,
			Content:   summary,
			Timestamp: time.Now().UTC(),
		}
		sequence++
		deltas <- domain.AgentDelta{
			TaskID:    task.ID,
			AgentID:   a.info.ID,
			Provider:  result.Provider,
			ModelName: result.ModelName,
			Kind:      domain.AgentDeltaFinalResult,
			Sequence:  sequence,
			Content:   summary,
			Timestamp: time.Now().UTC(),
		}
	}
	results <- result
}

func (a *OpenAICompatibleAgent) executeNativeStream(ctx context.Context, task domain.Task, deltas chan<- domain.AgentDelta) (domain.AgentResult, error) {
	startedAt := time.Now()
	recorder := newRealtimeMetricsRecorder(startedAt, string(domain.RuntimeTransportNativeStream), true, a.config.SupportsPseudoRealtime())
	result := domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     a.info.ID,
		Status:      domain.TaskStatusFailed,
		Provider:    a.info.Provider,
		ModelName:   firstNonEmpty(task.AssignedModel, a.info.ModelName, a.config.DefaultModel),
		CompletedAt: time.Now().UTC(),
		Output: domain.ResultOutput{Artifacts: map[string]any{
			"runtime":   "go",
			"transport": string(domain.RuntimeTransportNativeStream),
		}},
	}
	if !a.config.Configured() {
		return failedResult(result, startedAt, errors.New("provider is not configured")), errors.New("provider is not configured")
	}

	var runtime *workspace.Runtime
	if toolRuntimeEnabled(task) {
		var err error
		runtime, err = workspace.New(task.Context.RepoPath)
		if err != nil {
			return failedResult(result, startedAt, err), err
		}
	}

	var sequence int64 = 1
	var completion chatCompletionResponse
	var callCount int
	var err error
	if runtime != nil {
		completion, callCount, err = a.executeStreamingChatCompletionLoop(ctx, task, result.ModelName, runtime, deltas, &sequence, recorder)
	} else {
		completion, err = a.doStreamingChatCompletionRequest(ctx, chatCompletionRequest{
			Model:       result.ModelName,
			Messages:    []chatMessage{{Role: "system", Content: systemPrompt(task)}, {Role: "user", Content: taskPrompt(task)}},
			Temperature: 0.2,
			Stream:      true,
		}, func(token string) {
			if token == "" {
				return
			}
			recorder.markToken(time.Now().UTC())
			deltas <- domain.AgentDelta{
				TaskID:    task.ID,
				AgentID:   a.info.ID,
				Provider:  a.info.Provider,
				ModelName: result.ModelName,
				Kind:      domain.AgentDeltaToken,
				Sequence:  sequence,
				Content:   token,
				Metadata: map[string]any{
					"transport":        string(domain.RuntimeTransportNativeStream),
					"native_streaming": true,
				},
				Timestamp: time.Now().UTC(),
			}
			sequence++
		})
	}
	if err != nil {
		return failedResult(result, startedAt, err), err
	}
	if len(completion.Choices) == 0 {
		err = errors.New("provider returned no choices")
		return failedResult(result, startedAt, err), err
	}
	summary := strings.TrimSpace(chatMessageText(completion.Choices[0].Message))
	if summary == "" {
		err = errors.New("provider returned an empty completion")
		return failedResult(result, startedAt, err), err
	}
	recorder.markResult(time.Now().UTC())
	deltas <- domain.AgentDelta{
		TaskID:    task.ID,
		AgentID:   a.info.ID,
		Provider:  a.info.Provider,
		ModelName: result.ModelName,
		Kind:      domain.AgentDeltaPartialResult,
		Sequence:  sequence,
		Content:   summary,
		Timestamp: time.Now().UTC(),
	}
	sequence++
	deltas <- domain.AgentDelta{
		TaskID:    task.ID,
		AgentID:   a.info.ID,
		Provider:  a.info.Provider,
		ModelName: result.ModelName,
		Kind:      domain.AgentDeltaFinalResult,
		Sequence:  sequence,
		Content:   summary,
		Timestamp: time.Now().UTC(),
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
		result.Output.Artifacts["streaming_workspace"] = true
	} else {
		result.Output.CommandsRun = []string{"STREAM " + a.config.ChatCompletionsURL()}
		result.Output.Artifacts["runtime_tools_enabled"] = false
		result.Output.Artifacts["streaming_workspace"] = false
	}
	attachRealtimeMetrics(result.Output.Artifacts, recorder, result.CompletedAt)
	return result, nil
}

func (a *OpenAICompatibleAgent) executeStreamingChatCompletionLoop(ctx context.Context, task domain.Task, model string, runtime *workspace.Runtime, deltas chan<- domain.AgentDelta, sequence *int64, recorder *realtimeMetricsRecorder) (chatCompletionResponse, int, error) {
	messages := []chatMessage{
		{Role: "system", Content: systemPrompt(task)},
		{Role: "user", Content: taskPrompt(task)},
	}
	tools := workspaceTools()
	callCount := 0
	for iteration := 0; iteration < 8; iteration++ {
		payload := chatCompletionRequest{
			Model:       model,
			Messages:    messages,
			Temperature: 0.2,
			Tools:       tools,
			ToolChoice:  "auto",
			Stream:      true,
		}
		completion, err := a.doStreamingChatCompletionRequest(ctx, payload, func(token string) {
			if token == "" {
				return
			}
			recorder.markToken(time.Now().UTC())
			deltas <- domain.AgentDelta{
				TaskID:    task.ID,
				AgentID:   a.info.ID,
				Provider:  a.info.Provider,
				ModelName: model,
				Kind:      domain.AgentDeltaToken,
				Sequence:  *sequence,
				Content:   token,
				Timestamp: time.Now().UTC(),
			}
			*sequence = *sequence + 1
		})
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
		callCount += len(message.ToolCalls)
		messages = append(messages, chatMessage{Role: "assistant", Content: message.Content, ToolCalls: message.ToolCalls})
		for _, call := range message.ToolCalls {
			a.emitWorkspaceToolStarted(task, model, call, deltas, sequence, recorder)
			output, err := executeWorkspaceTool(runtime, call)
			if err != nil {
				output = map[string]any{"error": err.Error()}
			}
			a.emitWorkspaceToolFinished(task, model, call, output, deltas, sequence, recorder)
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

func (a *OpenAICompatibleAgent) emitWorkspaceToolStarted(task domain.Task, model string, call chatToolCall, deltas chan<- domain.AgentDelta, sequence *int64, recorder *realtimeMetricsRecorder) {
	metadata := map[string]any{"tool": call.Function.Name, "tool_call_id": call.ID}
	recorder.markTool(time.Now().UTC())
	arguments := parseToolArguments(call.Function.Arguments)
	switch call.Function.Name {
	case "write_file":
		path := stringArg(arguments, "path")
		content := stringArg(arguments, "content")
		if path != "" {
			metadata["path"] = path
		}
		if content != "" {
			recorder.markPatch(time.Now().UTC())
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaPatchPreview, Sequence: *sequence, Content: content, Metadata: map[string]any{"path": path}, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaPatchChunk, Sequence: *sequence, Content: content, Metadata: map[string]any{"path": path}, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
		}
		deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaPatchApplyStart, Sequence: *sequence, Content: path, Metadata: metadata, Timestamp: time.Now().UTC()}
		*sequence = *sequence + 1
	case "run_command":
		command := strings.Join(stringSliceArg(arguments, "command"), " ")
		if command != "" {
			metadata["command"] = command
		}
		deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaCommandStarted, Sequence: *sequence, Content: command, Metadata: metadata, Timestamp: time.Now().UTC()}
		*sequence = *sequence + 1
		testKind := inferTestKind(stringSliceArg(arguments, "command"))
		if testKind != "" {
			recorder.markTest(time.Now().UTC())
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaTestStarted, Sequence: *sequence, Content: testKind, Metadata: metadata, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
		}
	}
	deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaToolStarted, Sequence: *sequence, Content: call.Function.Name, Metadata: metadata, Timestamp: time.Now().UTC()}
	*sequence = *sequence + 1
}

func (a *OpenAICompatibleAgent) emitWorkspaceToolFinished(task domain.Task, model string, call chatToolCall, output map[string]any, deltas chan<- domain.AgentDelta, sequence *int64, recorder *realtimeMetricsRecorder) {
	metadata := map[string]any{"tool": call.Function.Name, "tool_call_id": call.ID}
	for key, value := range output {
		metadata[key] = value
	}
	switch call.Function.Name {
	case "write_file":
		recorder.markPatch(time.Now().UTC())
		path, _ := output["path"].(string)
		deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaFilePatch, Sequence: *sequence, Content: path, Metadata: metadata, Timestamp: time.Now().UTC()}
		*sequence = *sequence + 1
		deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaPatchApplyFinish, Sequence: *sequence, Content: path, Metadata: metadata, Timestamp: time.Now().UTC()}
		*sequence = *sequence + 1
	case "run_command":
		if stdout, _ := output["stdout"].(string); strings.TrimSpace(stdout) != "" {
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaToolStdout, Sequence: *sequence, Content: stdout, Metadata: metadata, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
		}
		if stderr, _ := output["stderr"].(string); strings.TrimSpace(stderr) != "" {
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaToolStderr, Sequence: *sequence, Content: stderr, Metadata: metadata, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
		}
		command, _ := output["command_text"].(string)
		deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaCommandFinished, Sequence: *sequence, Content: command, Metadata: metadata, Timestamp: time.Now().UTC()}
		*sequence = *sequence + 1
		if kind := inferTestKindFromOutput(output); kind != "" {
			recorder.markTest(time.Now().UTC())
			content := kind
			if passed, ok := output["exit_code"].(int); ok {
				content = fmt.Sprintf("%s exit=%d", kind, passed)
			}
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaTestCase, Sequence: *sequence, Content: content, Metadata: metadata, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
			deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaTestFinished, Sequence: *sequence, Content: kind, Metadata: metadata, Timestamp: time.Now().UTC()}
			*sequence = *sequence + 1
		}
	}
	serialized, _ := json.Marshal(output)
	deltas <- domain.AgentDelta{TaskID: task.ID, AgentID: a.info.ID, Provider: a.info.Provider, ModelName: model, Kind: domain.AgentDeltaToolFinished, Sequence: *sequence, Content: string(serialized), Metadata: metadata, Timestamp: time.Now().UTC()}
	*sequence = *sequence + 1
}

func parseToolArguments(raw string) map[string]any {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var arguments map[string]any
	if err := json.Unmarshal([]byte(raw), &arguments); err != nil {
		return nil
	}
	return arguments
}

func inferTestKind(command []string) string {
	return inferTestKindFromOutput(map[string]any{"command": strings.Join(command, " ")})
}

func inferTestKindFromOutput(output map[string]any) string {
	command, _ := output["command"].(string)
	if command == "" {
		command, _ = output["command_text"].(string)
	}
	command = strings.TrimSpace(command)
	if command == "" {
		return ""
	}
	switch {
	case strings.HasPrefix(command, "go test"):
		return "go test"
	case strings.HasPrefix(command, "pytest"):
		return "pytest"
	case strings.HasPrefix(command, "python -m pytest"), strings.HasPrefix(command, "python3 -m pytest"):
		return "pytest"
	case strings.HasPrefix(command, "npm test"):
		return "npm test"
	default:
		return ""
	}
}

func (a *OpenAICompatibleAgent) doStreamingChatCompletionRequest(ctx context.Context, payload chatCompletionRequest, emitToken func(string)) (chatCompletionResponse, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return chatCompletionResponse{}, err
	}
	endpoint := a.config.ChatCompletionsURL()
	if endpoint == "" {
		return chatCompletionResponse{}, fmt.Errorf("chat completions endpoint is not configured")
	}

	var completion chatCompletionResponse
	err = providerhttp.DoStream(ctx, providerhttp.RequestConfig{
		ProviderID:   a.info.Provider,
		BaseURL:      a.config.BaseURL,
		APIKey:       a.config.APIKey,
		ModelName:    payload.Model,
		TrafficClass: "primary",
		Client:       a.client,
	}, http.MethodPost, endpoint, body, "application/json", func(req *http.Request) {
		req.Header.Set("Accept", "text/event-stream")
	}, func(resp *http.Response) error {
		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 0, 16<<10), 1<<20)
		var content strings.Builder
		toolCallsByIndex := map[int]*chatToolCall{}
		completion = chatCompletionResponse{Choices: []struct {
			Message      chatMessage `json:"message"`
			FinishReason string      `json:"finish_reason"`
		}{{Message: chatMessage{Role: "assistant"}}}}
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" || strings.HasPrefix(line, ":") {
				continue
			}
			if !strings.HasPrefix(line, "data:") {
				continue
			}
			payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if payload == "[DONE]" {
				break
			}
			var chunk chatCompletionStreamChunk
			if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
				return fmt.Errorf("decode %s stream response: %w", a.info.Provider, err)
			}
			if completion.ID == "" {
				completion.ID = chunk.ID
			}
			if len(chunk.Choices) == 0 {
				continue
			}
			choice := chunk.Choices[0]
			if choice.Delta.Role != "" {
				completion.Choices[0].Message.Role = choice.Delta.Role
			}
			if choice.Delta.Content != "" {
				content.WriteString(choice.Delta.Content)
				if emitToken != nil {
					emitToken(choice.Delta.Content)
				}
			}
			for _, streamedCall := range choice.Delta.ToolCalls {
				call := toolCallsByIndex[streamedCall.Index]
				if call == nil {
					call = &chatToolCall{}
					toolCallsByIndex[streamedCall.Index] = call
				}
				if streamedCall.ID != "" {
					call.ID = streamedCall.ID
				}
				if streamedCall.Type != "" {
					call.Type = streamedCall.Type
				}
				if streamedCall.Function.Name != "" {
					call.Function.Name = streamedCall.Function.Name
				}
				if streamedCall.Function.Arguments != "" {
					call.Function.Arguments += streamedCall.Function.Arguments
				}
			}
			if choice.FinishReason != "" {
				completion.Choices[0].FinishReason = choice.FinishReason
			}
			if len(chunk.Usage) > 0 {
				completion.Usage = chunk.Usage
			}
		}
		if err := scanner.Err(); err != nil {
			return err
		}
		completion.Choices[0].Message.Content = content.String()
		if len(toolCallsByIndex) > 0 {
			indexes := make([]int, 0, len(toolCallsByIndex))
			for index := range toolCallsByIndex {
				indexes = append(indexes, index)
			}
			slices.Sort(indexes)
			completion.Choices[0].Message.ToolCalls = make([]chatToolCall, 0, len(indexes))
			for _, index := range indexes {
				call := toolCallsByIndex[index]
				completion.Choices[0].Message.ToolCalls = append(completion.Choices[0].Message.ToolCalls, *call)
			}
		}
		return nil
	})
	if err != nil {
		return chatCompletionResponse{}, err
	}
	return completion, nil
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
	response, err := a.doProviderRequest(ctx, http.MethodPost, endpoint, body, "application/json", 8<<20)
	if err != nil {
		return chatCompletionResponse{}, err
	}
	if response.statusCode < 200 || response.statusCode >= 300 {
		return chatCompletionResponse{}, providerHTTPError(response.statusCode, response.body)
	}
	var completion chatCompletionResponse
	if err := json.Unmarshal(response.body, &completion); err != nil {
		return chatCompletionResponse{}, fmt.Errorf("decode %s response: %w", a.info.Provider, err)
	}
	return completion, nil
}

func (a *OpenAICompatibleAgent) doProviderRequest(ctx context.Context, method, endpoint string, body []byte, contentType string, limit int64) (providerHTTPResponse, error) {
	response, err := providerhttp.Do(ctx, providerhttp.RequestConfig{
		ProviderID:   a.config.EffectiveProviderID(),
		ModelName:    firstNonEmpty(a.info.ModelName, a.config.DefaultModel),
		BaseURL:      a.config.BaseURL,
		APIKey:       a.config.APIKey,
		TrafficClass: "primary",
		Client:       a.client,
	}, method, endpoint, body, contentType, limit)
	if err != nil {
		return providerHTTPResponse{}, err
	}
	return providerHTTPResponse{
		statusCode: response.StatusCode,
		header:     response.Header,
		body:       response.Body,
	}, nil
}

func taskNeedsBufferedExecution(task domain.Task) bool {
	if strings.TrimSpace(task.Context.RepoPath) != "" {
		return true
	}
	return len(task.Input.Files) > 0
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
	withDefaults := func(provider string, cfg OpenAICompatibleConfig, defaultNativeStreaming bool) OpenAICompatibleConfig {
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
		cfg.NativeStreaming = boolEnvDefault(providerEnvKey(provider, "NATIVE_STREAMING"), defaultNativeStreaming)
		cfg.AllowPseudoRealtime = boolEnvDefault(providerEnvKey(provider, "ALLOW_PSEUDO_REALTIME"), true)
		return cfg
	}
	configs := map[string]OpenAICompatibleConfig{
		"local": withDefaults("local", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_LOCAL_LLM_PROVIDER_ID", "OLLAMA_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("http://127.0.0.1:11434", "AI_BRIDGE_LOCAL_LLM_ENDPOINT", "OLLAMA_BASE_URL", "OLLAMA_HOST"),
			APIKey:                  firstEnv("AI_BRIDGE_LOCAL_LLM_API_KEY", "OLLAMA_API_KEY"),
			DefaultModel:            firstEnvDefault("", "AI_BRIDGE_LOCAL_LLM_MODEL", "OLLAMA_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_LOCAL_LLM_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_LOCAL_LLM_CHAT_COMPLETIONS_ENDPOINT"),
		}, true),
		"ai_kernel": withDefaults("ai_kernel", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_AI_KERNEL_PROVIDER_ID", "AI_KERNEL_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("http://127.0.0.1:8012/v1", "AI_KERNEL_BASE_URL", "AI_BRIDGE_AI_KERNEL_BASE_URL"),
			APIKey:                  firstEnv("AI_KERNEL_API_KEY", "AI_BRIDGE_AI_KERNEL_API_KEY"),
			DefaultModel:            firstEnvDefault("gemma4-12b-agentic-fable5:q4_k_m", "AI_KERNEL_MODEL_ALIAS", "AI_BRIDGE_AI_KERNEL_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_AI_KERNEL_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_AI_KERNEL_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              envBool("AI_KERNEL_REQUIRE_API_KEY", true),
		}, true),
		"openai": withDefaults("openai", OpenAICompatibleConfig{
			ProviderID:                  firstEnv("AI_BRIDGE_OPENAI_PROVIDER_ID"),
			BaseURL:                     firstEnvDefault("https://api.openai.com/v1", "OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL"),
			APIKey:                      firstEnv("OPENAI_API_KEY"),
			DefaultModel:                firstEnvDefault("gpt-5.5", "OPENAI_DEFAULT_MODEL"),
			ModelsEndpoint:              firstEnv("AI_BRIDGE_OPENAI_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint:     firstEnv("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT"),
			ResponsesEndpoint:           firstEnv("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT"),
			MessagesEndpoint:            firstEnv("AI_BRIDGE_OPENAI_MESSAGES_ENDPOINT"),
			MessagesCountTokensEndpoint: firstEnv("AI_BRIDGE_OPENAI_MESSAGES_COUNT_TOKENS_ENDPOINT"),
			CodexEndpoint:               firstEnv("AI_BRIDGE_OPENAI_CODEX_ENDPOINT"),
			RequireKey:                  true,
		}, true),
		"codexsale": withDefaults("codexsale", OpenAICompatibleConfig{
			ProviderID:                  firstEnvDefault("codexsale", "CODEX_SALE_PROVIDER_ID"),
			BaseURL:                     firstEnvDefault("https://codex.sale/v1", "CODEX_SALE_BASE_URL"),
			APIKey:                      firstEnv("CODEX_SALE_API_KEY"),
			DefaultModel:                firstEnvDefault("gpt-5.6-sol", "CODEX_SALE_MODEL", "CODEX_OPENAI_MODEL"),
			ModelsEndpoint:              firstEnvDefault("https://codex.sale/v1/models", "CODEX_SALE_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint:     firstEnvDefault("https://codex.sale/v1/chat/completions", "CODEX_SALE_CHAT_COMPLETIONS_ENDPOINT"),
			ResponsesEndpoint:           firstEnvDefault("https://codex.sale/v1/responses", "CODEX_SALE_RESPONSES_ENDPOINT"),
			MessagesEndpoint:            firstEnvDefault("https://codex.sale/v1/messages", "CODEX_SALE_MESSAGES_ENDPOINT"),
			MessagesCountTokensEndpoint: firstEnvDefault("https://codex.sale/v1/messages/count_tokens", "CODEX_SALE_MESSAGES_COUNT_TOKENS_ENDPOINT"),
			CodexEndpoint:               firstEnvDefault("https://codex.sale/backend-api/codex", "CODEX_SALE_CODEX_ENDPOINT"),
			RequireKey:                  true,
		}, true),
		"mistral": withDefaults("mistral", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_MISTRAL_PROVIDER_ID"),
			BaseURL:                 firstEnvDefault("https://api.mistral.ai/v1", "MISTRAL_BASE_URL", "AI_BRIDGE_MISTRAL_BASE_URL"),
			APIKey:                  firstEnv("MISTRAL_API_KEY", "AI_BRIDGE_MISTRAL_API_KEY"),
			DefaultModel:            firstEnvDefault("mistral-large-latest", "MISTRAL_MODEL", "AI_BRIDGE_MISTRAL_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_MISTRAL_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_MISTRAL_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              true,
		}, true),
		"antigravity": withDefaults("antigravity", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_ANTIGRAVITY_PROVIDER_ID"),
			BaseURL:                 firstEnv("ANTIGRAVITY_BASE_URL", "AI_BRIDGE_ANTIGRAVITY_BASE_URL"),
			APIKey:                  firstEnv("ANTIGRAVITY_API_KEY", "AI_BRIDGE_ANTIGRAVITY_API_KEY"),
			DefaultModel:            firstEnvDefault("antigravity-coder", "ANTIGRAVITY_MODEL", "AI_BRIDGE_ANTIGRAVITY_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_ANTIGRAVITY_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_ANTIGRAVITY_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              envBool("ANTIGRAVITY_REQUIRE_API_KEY", firstEnv("ANTIGRAVITY_API_KEY", "AI_BRIDGE_ANTIGRAVITY_API_KEY") != ""),
		}, true),
		"mimo": withDefaults("mimo", OpenAICompatibleConfig{
			ProviderID:              firstEnv("AI_BRIDGE_MIMO_PROVIDER_ID"),
			BaseURL:                 firstEnv("MIMO_BASE_URL", "AI_BRIDGE_MIMO_BASE_URL"),
			APIKey:                  firstEnv("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY"),
			DefaultModel:            firstEnvDefault("mimo-coder", "MIMO_MODEL", "AI_BRIDGE_MIMO_MODEL"),
			ModelsEndpoint:          firstEnv("AI_BRIDGE_MIMO_MODELS_ENDPOINT"),
			ChatCompletionsEndpoint: firstEnv("AI_BRIDGE_MIMO_CHAT_COMPLETIONS_ENDPOINT"),
			RequireKey:              envBool("MIMO_REQUIRE_API_KEY", envBool("AI_BRIDGE_MIMO_REQUIRE_API_KEY", envBool("AI_BRIDGE_MIMO_ENABLED", false))),
		}, true),
	}
	for key, cfg := range configs {
		if strings.TrimSpace(cfg.BaseURL) == "" {
			delete(configs, key)
		}
	}
	if openaiCfg, ok := configs["openai"]; ok && LooksLikeCodexSaleAlias(openaiCfg) {
		codexCfg, hasCodex := configs["codexsale"]
		if !hasCodex || !codexCfg.Configured() {
			aliasCfg := openaiCfg
			aliasCfg.ProviderID = firstNonEmpty(codexCfg.ProviderID, aliasCfg.ProviderID, "codexsale")
			aliasCfg.DefaultModel = firstNonEmpty(codexCfg.DefaultModel, aliasCfg.DefaultModel, "gpt-5.6-sol")
			aliasCfg.APIKey = firstNonEmpty(codexCfg.APIKey, aliasCfg.APIKey)
			configs["codexsale"] = withDefaults("codexsale", aliasCfg, true)
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

func providerEnvKey(provider, suffix string) string {
	provider = strings.ToUpper(strings.TrimSpace(provider))
	provider = strings.ReplaceAll(provider, "-", "_")
	return provider + "_" + suffix
}

func boolEnvDefault(key string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
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

func durationFromEnvAny(fallback time.Duration, keys ...string) time.Duration {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			parsed, err := time.ParseDuration(value)
			if err == nil && parsed > 0 {
				return parsed
			}
		}
	}
	return fallback
}

func intFromEnv(key string, fallback int, aliases ...string) int {
	keys := append([]string{key}, aliases...)
	for _, envKey := range keys {
		value := strings.TrimSpace(os.Getenv(envKey))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err == nil {
			return parsed
		}
	}
	return fallback
}
