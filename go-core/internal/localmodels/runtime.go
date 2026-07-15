package localmodels

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
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	DefaultLocalModel    = "qwen2.5:32b-instruct-q4_k_m"
	DefaultLocalEndpoint = "http://host.containers.internal:11434"
)

var retryableStatusCodes = []int{408, 409, 425, 429, 500, 502, 503, 504}

type RetryPolicy struct {
	MaxAttempts    int
	BackoffBaseSec float64
}

type Config struct {
	Endpoint             string
	FallbackEndpoints    []string
	ModelName            string
	HealthTimeout        time.Duration
	GenerationTimeout    time.Duration
	ManagementTimeout    time.Duration
	WarmKeepAliveSeconds int
	RetryPolicy          RetryPolicy
}

type Model struct {
	Name       string         `json:"name"`
	Size       int64          `json:"size,omitempty"`
	Digest     string         `json:"digest,omitempty"`
	ModifiedAt string         `json:"modified_at,omitempty"`
	Details    map[string]any `json:"details,omitempty"`
}

type ResidentModel struct {
	Name      string         `json:"name"`
	Size      int64          `json:"size,omitempty"`
	SizeVRAM  int64          `json:"size_vram,omitempty"`
	ExpiresAt string         `json:"expires_at,omitempty"`
	Digest    string         `json:"digest,omitempty"`
	Details   map[string]any `json:"details,omitempty"`
}

type Health struct {
	OK              bool     `json:"ok"`
	Ready           bool     `json:"ready"`
	Status          string   `json:"status"`
	Endpoint        string   `json:"endpoint"`
	ModelName       string   `json:"model_name"`
	AvailableModels []string `json:"available_models"`
	ModelPresent    bool     `json:"model_present"`
	Attempts        int      `json:"attempts"`
	StatusCode      int      `json:"status_code,omitempty"`
	Error           string   `json:"error,omitempty"`
}

type Runtime struct {
	client         *http.Client
	config         Config
	mu             sync.RWMutex
	activeEndpoint string
}

type apiError struct {
	StatusCode int
	Body       string
}

func (e *apiError) Error() string {
	if strings.TrimSpace(e.Body) == "" {
		return fmt.Sprintf("request failed with status %d", e.StatusCode)
	}
	return fmt.Sprintf("request failed with status %d: %s", e.StatusCode, strings.TrimSpace(e.Body))
}

func ConfigFromEnv() Config {
	return Config{
		Endpoint:             normalizeEndpoint(envString("AI_BRIDGE_LOCAL_LLM_ENDPOINT", DefaultLocalEndpoint)),
		FallbackEndpoints:    envList("AI_BRIDGE_LOCAL_LLM_FALLBACK_ENDPOINTS"),
		ModelName:            envString("AI_BRIDGE_LOCAL_LLM_MODEL", DefaultLocalModel),
		HealthTimeout:        envDurationSeconds("AI_BRIDGE_LOCAL_LLM_HEALTH_TIMEOUT_SEC", 1.0, 0.2),
		GenerationTimeout:    envDurationSeconds("AI_BRIDGE_LOCAL_LLM_GENERATE_TIMEOUT_SEC", 60.0, 5.0),
		ManagementTimeout:    envDurationSeconds("AI_BRIDGE_LOCAL_LLM_MANAGEMENT_TIMEOUT_SEC", 600.0, 5.0),
		WarmKeepAliveSeconds: envInt("AI_BRIDGE_LOCAL_LLM_WARM_KEEP_ALIVE_SEC", 900, 1),
		RetryPolicy: RetryPolicy{
			MaxAttempts:    envInt("AI_BRIDGE_LOCAL_LLM_RETRY_ATTEMPTS", 2, 1),
			BackoffBaseSec: envFloat("AI_BRIDGE_LOCAL_LLM_RETRY_BACKOFF_SEC", 0.2, 0),
		},
	}
}

func NewRuntime(config Config) *Runtime {
	return &Runtime{
		client: &http.Client{},
		config: config,
	}
}

func (r *Runtime) Endpoints() []string {
	return candidateEndpoints(r.config.Endpoint, r.config.FallbackEndpoints)
}

func (r *Runtime) ActiveEndpoint() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.activeEndpoint != "" {
		return r.activeEndpoint
	}
	return r.config.Endpoint
}

func (r *Runtime) ListModels(ctx context.Context) ([]Model, string, error) {
	var payload struct {
		Models []struct {
			Name       string         `json:"name"`
			Model      string         `json:"model"`
			Size       int64          `json:"size"`
			Digest     string         `json:"digest"`
			ModifiedAt string         `json:"modified_at"`
			Details    map[string]any `json:"details"`
		} `json:"models"`
	}
	endpoint, err := r.doJSON(ctx, http.MethodGet, "/api/tags", nil, r.config.HealthTimeout, &payload)
	if err != nil {
		return nil, endpoint, err
	}
	models := make([]Model, 0, len(payload.Models))
	for _, item := range payload.Models {
		name := strings.TrimSpace(item.Name)
		if name == "" {
			name = strings.TrimSpace(item.Model)
		}
		models = append(models, Model{
			Name:       name,
			Size:       item.Size,
			Digest:     item.Digest,
			ModifiedAt: item.ModifiedAt,
			Details:    cloneMap(item.Details),
		})
	}
	return models, endpoint, nil
}

func (r *Runtime) ListResidentModels(ctx context.Context) ([]ResidentModel, string, error) {
	var payload struct {
		Models []struct {
			Name      string         `json:"name"`
			Model     string         `json:"model"`
			Size      int64          `json:"size"`
			SizeVRAM  int64          `json:"size_vram"`
			ExpiresAt string         `json:"expires_at"`
			Digest    string         `json:"digest"`
			Details   map[string]any `json:"details"`
		} `json:"models"`
	}
	endpoint, err := r.doJSON(ctx, http.MethodGet, "/api/ps", nil, r.config.HealthTimeout, &payload)
	if err != nil {
		return nil, endpoint, err
	}
	residents := make([]ResidentModel, 0, len(payload.Models))
	for _, item := range payload.Models {
		name := strings.TrimSpace(item.Name)
		if name == "" {
			name = strings.TrimSpace(item.Model)
		}
		residents = append(residents, ResidentModel{
			Name:      name,
			Size:      item.Size,
			SizeVRAM:  item.SizeVRAM,
			ExpiresAt: item.ExpiresAt,
			Digest:    item.Digest,
			Details:   cloneMap(item.Details),
		})
	}
	return residents, endpoint, nil
}

func (r *Runtime) Health(ctx context.Context, modelName string) Health {
	models, endpoint, err := r.ListModels(ctx)
	health := Health{
		Endpoint:  endpoint,
		ModelName: strings.TrimSpace(modelName),
		Status:    "unavailable",
	}
	if err != nil {
		var apiErr *apiError
		if errors.As(err, &apiErr) {
			health.StatusCode = apiErr.StatusCode
		}
		health.Error = err.Error()
		return health
	}
	health.OK = true
	health.Status = "ok"
	health.Attempts = 1
	for _, model := range models {
		health.AvailableModels = append(health.AvailableModels, model.Name)
		if model.Name == health.ModelName {
			health.ModelPresent = true
		}
	}
	health.Ready = health.ModelName == "" || health.ModelPresent
	if !health.Ready {
		health.Status = "degraded"
		health.Error = "configured model is not available"
	}
	return health
}

func (r *Runtime) PullModel(ctx context.Context, modelName string) (string, error) {
	request := map[string]any{"name": effectiveModel(modelName, r.config.ModelName), "stream": false}
	return r.postMutation(ctx, "/api/pull", request)
}

func (r *Runtime) WarmModel(ctx context.Context, modelName string) (string, error) {
	request := map[string]any{
		"model":      effectiveModel(modelName, r.config.ModelName),
		"prompt":     "",
		"stream":     false,
		"keep_alive": r.config.WarmKeepAliveSeconds,
	}
	return r.postMutation(ctx, "/api/generate", request)
}

func (r *Runtime) UnloadModel(ctx context.Context, modelName string) (string, error) {
	request := map[string]any{
		"model":      effectiveModel(modelName, r.config.ModelName),
		"prompt":     "",
		"stream":     false,
		"keep_alive": 0,
	}
	return r.postMutation(ctx, "/api/generate", request)
}

func (r *Runtime) postMutation(ctx context.Context, path string, payload map[string]any) (string, error) {
	endpoint, err := r.doJSON(ctx, http.MethodPost, path, payload, r.config.ManagementTimeout, nil)
	return endpoint, err
}

func (r *Runtime) doJSON(ctx context.Context, method string, path string, payload any, timeout time.Duration, out any) (string, error) {
	var body []byte
	var err error
	if payload != nil {
		body, err = json.Marshal(payload)
		if err != nil {
			return "", err
		}
	}

	var lastErr error
	lastEndpoint := ""
	for attempt := 1; attempt <= max(1, r.config.RetryPolicy.MaxAttempts); attempt++ {
		for _, endpoint := range r.endpointOrder() {
			lastEndpoint = endpoint
			err = r.doAttempt(ctx, method, endpoint, path, body, timeout, out)
			if err == nil {
				r.setActiveEndpoint(endpoint)
				return endpoint, nil
			}
			lastErr = err
			var apiErr *apiError
			if errors.As(err, &apiErr) && !slices.Contains(retryableStatusCodes, apiErr.StatusCode) {
				return endpoint, err
			}
			if ctx.Err() != nil {
				return endpoint, err
			}
		}
		if attempt < max(1, r.config.RetryPolicy.MaxAttempts) {
			backoff := time.Duration(float64(time.Second) * r.config.RetryPolicy.BackoffBaseSec * float64(attempt))
			if backoff > 0 {
				timer := time.NewTimer(backoff)
				select {
				case <-ctx.Done():
					timer.Stop()
					return lastEndpoint, ctx.Err()
				case <-timer.C:
				}
			}
		}
	}
	return lastEndpoint, lastErr
}

func (r *Runtime) doAttempt(ctx context.Context, method string, endpoint string, path string, body []byte, timeout time.Duration, out any) error {
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	request, err := http.NewRequestWithContext(reqCtx, method, endpoint+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	request.Header.Set("Accept", "application/json")

	response, err := r.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return err
	}
	if response.StatusCode >= http.StatusBadRequest {
		return &apiError{StatusCode: response.StatusCode, Body: string(raw)}
	}
	if out == nil || len(bytes.TrimSpace(raw)) == 0 {
		return nil
	}
	return json.Unmarshal(raw, out)
}

func (r *Runtime) endpointOrder() []string {
	candidates := r.Endpoints()
	active := r.ActiveEndpoint()
	if active == "" || len(candidates) == 0 {
		return candidates
	}
	if candidates[0] == active {
		return candidates
	}
	ordered := []string{active}
	for _, candidate := range candidates {
		if candidate != active {
			ordered = append(ordered, candidate)
		}
	}
	return ordered
}

func (r *Runtime) setActiveEndpoint(endpoint string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.activeEndpoint = endpoint
}

func candidateEndpoints(primary string, extra []string) []string {
	values := []string{normalizeEndpoint(primary)}
	for _, item := range extra {
		if normalized := normalizeEndpoint(item); normalized != "" {
			values = append(values, normalized)
		}
	}
	derived := make([]string, 0, len(values)*2)
	for _, value := range values {
		switch {
		case strings.Contains(value, "host.containers.internal"):
			derived = append(derived,
				strings.ReplaceAll(value, "host.containers.internal", "127.0.0.1"),
				strings.ReplaceAll(value, "host.containers.internal", "localhost"),
			)
		case strings.Contains(value, "127.0.0.1"):
			derived = append(derived, strings.ReplaceAll(value, "127.0.0.1", "localhost"))
		case strings.Contains(value, "localhost"):
			derived = append(derived, strings.ReplaceAll(value, "localhost", "127.0.0.1"))
		}
	}
	ordered := make([]string, 0, len(values)+len(derived))
	for _, value := range append(values, derived...) {
		if value == "" || slices.Contains(ordered, value) {
			continue
		}
		ordered = append(ordered, value)
	}
	return ordered
}

func normalizeEndpoint(raw string) string {
	value := strings.TrimSpace(strings.TrimRight(raw, "/"))
	if value == "" {
		return ""
	}
	if _, err := url.Parse(value); err != nil {
		return ""
	}
	return value
}

func effectiveModel(value string, fallback string) string {
	model := strings.TrimSpace(value)
	if model == "" {
		return strings.TrimSpace(fallback)
	}
	return model
}

func envString(name string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func envList(name string) []string {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		if item := strings.TrimSpace(part); item != "" {
			result = append(result, item)
		}
	}
	return result
}

func envInt(name string, fallback int, minimum int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	if parsed < minimum {
		return minimum
	}
	return parsed
}

func envFloat(name string, fallback float64, minimum float64) float64 {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return fallback
	}
	if parsed < minimum {
		return minimum
	}
	return parsed
}

func envDurationSeconds(name string, fallback float64, minimum float64) time.Duration {
	seconds := envFloat(name, fallback, minimum)
	return time.Duration(seconds * float64(time.Second))
}

func cloneMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
