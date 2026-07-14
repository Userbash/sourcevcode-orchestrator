package kernel

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"maps"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ProviderModelRegistry struct {
	mu              sync.RWMutex
	configs         map[string]agents.OpenAICompatibleConfig
	snapshots       map[string]domain.ProviderCatalogSnapshot
	clientByTimeout map[time.Duration]*http.Client
	refreshInterval time.Duration
	timeout         time.Duration
	enabled         bool
	validateModels  bool
	validationLimit int
}

func NewProviderModelRegistry(configs map[string]agents.OpenAICompatibleConfig) *ProviderModelRegistry {
	copyConfigs := make(map[string]agents.OpenAICompatibleConfig, len(configs))
	for key, cfg := range maps.Clone(configs) {
		copyConfigs[strings.ToLower(strings.TrimSpace(key))] = cfg
	}
	return &ProviderModelRegistry{
		configs:         copyConfigs,
		snapshots:       make(map[string]domain.ProviderCatalogSnapshot),
		clientByTimeout: make(map[time.Duration]*http.Client),
		refreshInterval: registryEnvDuration("AI_BRIDGE_MODEL_REFRESH_INTERVAL", 5*time.Minute, "GO_CORE_MODEL_REGISTRY_REFRESH_INTERVAL"),
		timeout:         registryEnvDuration("AI_BRIDGE_MODEL_REFRESH_TIMEOUT", 20*time.Second, "GO_CORE_MODEL_REGISTRY_TIMEOUT"),
		enabled:         registryEnvBool("AI_BRIDGE_MODEL_REFRESH_ENABLED", true, "GO_CORE_MODEL_REGISTRY_ENABLED"),
		validateModels:  registryEnvBool("AI_BRIDGE_MODEL_VALIDATE_MODELS", true),
		validationLimit: registryEnvInt("AI_BRIDGE_MODEL_VALIDATE_LIMIT", 12),
	}
}

func (r *ProviderModelRegistry) Start(ctx context.Context) {
	if r == nil || !r.enabled {
		return
	}
	r.Refresh(ctx)
	if r.refreshInterval <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(r.refreshInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				r.Refresh(context.Background())
			}
		}
	}()
}

func (r *ProviderModelRegistry) Refresh(ctx context.Context) {
	if r == nil {
		return
	}
	configs := r.Configs()
	var wg sync.WaitGroup
	results := make(chan domain.ProviderCatalogSnapshot, len(configs))
	for provider, cfg := range configs {
		provider, cfg := provider, cfg
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- r.fetchProviderSnapshot(ctx, provider, cfg)
		}()
	}
	wg.Wait()
	close(results)
	r.mu.Lock()
	defer r.mu.Unlock()
	for snapshot := range results {
		r.snapshots[snapshot.Provider] = snapshot
	}
}

func (r *ProviderModelRegistry) Configs() map[string]agents.OpenAICompatibleConfig {
	r.mu.RLock()
	defer r.mu.RUnlock()
	result := make(map[string]agents.OpenAICompatibleConfig, len(r.configs))
	for key, cfg := range r.configs {
		result[key] = cfg
	}
	return result
}

func (r *ProviderModelRegistry) Snapshot(provider string) (domain.ProviderCatalogSnapshot, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	row, ok := r.snapshots[strings.ToLower(strings.TrimSpace(provider))]
	return row, ok
}

func (r *ProviderModelRegistry) Snapshots() []domain.ProviderCatalogSnapshot {
	r.mu.RLock()
	defer r.mu.RUnlock()
	result := make([]domain.ProviderCatalogSnapshot, 0, len(r.snapshots))
	for _, snapshot := range r.snapshots {
		result = append(result, cloneProviderSnapshot(snapshot))
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Provider < result[j].Provider })
	return result
}

func (r *ProviderModelRegistry) HealthyModels(provider string) []domain.ProviderModelStatus {
	snapshot, ok := r.Snapshot(provider)
	if !ok {
		return nil
	}
	result := make([]domain.ProviderModelStatus, 0, len(snapshot.Models))
	for _, model := range snapshot.Models {
		if model.Available {
			result = append(result, model)
		}
	}
	return result
}

func (r *ProviderModelRegistry) fetchProviderSnapshot(parent context.Context, provider string, cfg agents.OpenAICompatibleConfig) domain.ProviderCatalogSnapshot {
	now := time.Now().UTC()
	snapshot := domain.ProviderCatalogSnapshot{
		Provider:                    provider,
		ProviderID:                  cfg.EffectiveProviderID(),
		Configured:                  cfg.Configured(),
		Status:                      "not_configured",
		BaseURL:                     sanitizedURL(cfg.BaseURL),
		ModelsEndpoint:              sanitizedURL(cfg.ModelsURL()),
		ChatCompletionsEndpoint:     sanitizedURL(cfg.ChatCompletionsURL()),
		ResponsesEndpoint:           sanitizedURL(cfg.ResponsesURL()),
		MessagesEndpoint:            sanitizedURL(cfg.MessagesURL()),
		MessagesCountTokensEndpoint: sanitizedURL(cfg.MessagesCountTokensURL()),
		CodexEndpoint:               sanitizedURL(cfg.CodexURL()),
		DefaultModel:                cfg.DefaultModel,
		ObservedAt:                  now,
		RefreshIntervalSec:          int(r.refreshInterval.Seconds()),
	}
	if !cfg.Configured() {
		snapshot.Error = "provider credentials or endpoint are not configured"
		return snapshot
	}
	ctx, cancel := context.WithTimeout(parent, maxDuration(cfg.Timeout, r.timeout))
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, cfg.ModelsURL(), nil)
	if err != nil {
		snapshot.Error = err.Error()
		snapshot.Status = "error"
		return snapshot
	}
	if key := strings.TrimSpace(cfg.APIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := r.httpClient(maxDuration(cfg.Timeout, r.timeout)).Do(req)
	if err != nil {
		snapshot.Error = err.Error()
		snapshot.Status = "unavailable"
		return snapshot
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		snapshot.Error = strings.TrimSpace(string(body))
		snapshot.Status = "unavailable"
		if snapshot.Error == "" {
			snapshot.Error = resp.Status
		}
		return snapshot
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		snapshot.Error = err.Error()
		snapshot.Status = "error"
		return snapshot
	}
	models, parseErr := parseProviderModels(provider, cfg.DefaultModel, body)
	if parseErr != nil {
		snapshot.Error = parseErr.Error()
		snapshot.Status = "error"
		return snapshot
	}
	if len(models) == 0 {
		snapshot.Status = "unavailable"
		snapshot.Error = "provider returned an empty model inventory"
		return snapshot
	}
	defaultFound := false
	for i := range models {
		if strings.EqualFold(models[i].ModelName, cfg.DefaultModel) {
			models[i].IsDefault = true
			defaultFound = true
		}
	}
	if !defaultFound && strings.TrimSpace(cfg.DefaultModel) != "" {
		models = append([]domain.ProviderModelStatus{{
			Provider:   provider,
			ModelName:  cfg.DefaultModel,
			Available:  false,
			Status:     "missing",
			Reason:     "configured default model is not present in provider inventory",
			ObservedAt: now,
			IsDefault:  true,
		}}, models...)
	}
	if r.shouldValidateProvider(cfg) && len(models) > 0 {
		models = r.validateDiscoveredModels(ctx, cfg, models)
	}
	snapshot.Models = models
	availableCount := 0
	unavailableCount := 0
	for _, model := range models {
		if model.Available {
			availableCount++
		} else {
			unavailableCount++
		}
	}
	snapshot.Available = availableCount > 0
	switch {
	case availableCount == 0:
		snapshot.Status = "unavailable"
		if snapshot.Error == "" {
			snapshot.Error = aggregateModelError(models, "no validated models are available")
		}
	case unavailableCount > 0:
		snapshot.Status = "degraded"
		if snapshot.Error == "" {
			snapshot.Error = aggregateModelError(models, "one or more models are unavailable")
		}
	default:
		snapshot.Status = "ready"
	}
	return snapshot
}

func (r *ProviderModelRegistry) shouldValidateProvider(cfg agents.OpenAICompatibleConfig) bool {
	if !r.validateModels {
		return false
	}
	host := strings.ToLower(cfg.BaseURL)
	id := strings.ToLower(cfg.EffectiveProviderID())
	return strings.Contains(host, "codex.sale") || id == "codexsale"
}

func (r *ProviderModelRegistry) validateDiscoveredModels(ctx context.Context, cfg agents.OpenAICompatibleConfig, models []domain.ProviderModelStatus) []domain.ProviderModelStatus {
	limit := r.validationLimit
	if limit <= 0 || limit > len(models) {
		limit = len(models)
	}
	for i := 0; i < limit; i++ {
		ok, reason := r.validateModelViaChat(ctx, cfg, models[i].ModelName)
		models[i].ObservedAt = time.Now().UTC()
		if ok {
			models[i].Available = true
			models[i].Status = "ready"
			models[i].Reason = ""
			continue
		}
		models[i].Available = false
		models[i].Status = "validation_failed"
		models[i].Reason = reason
	}
	return models
}

func (r *ProviderModelRegistry) validateModelViaChat(ctx context.Context, cfg agents.OpenAICompatibleConfig, model string) (bool, string) {
	url := cfg.ChatCompletionsURL()
	if strings.TrimSpace(url) == "" {
		return false, "chat completions endpoint is not configured"
	}
	payload := map[string]any{
		"model":      model,
		"messages":   []map[string]string{{"role": "user", "content": "ping"}},
		"max_tokens": 1,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return false, err.Error()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return false, err.Error()
	}
	req.Header.Set("Content-Type", "application/json")
	if strings.TrimSpace(cfg.APIKey) != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	}
	resp, err := r.httpClient(maxDuration(cfg.Timeout, r.timeout)).Do(req)
	if err != nil {
		return false, err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true, ""
	}
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	msg := strings.TrimSpace(string(b))
	if msg == "" {
		msg = resp.Status
	}
	return false, msg
}

func (r *ProviderModelRegistry) httpClient(timeout time.Duration) *http.Client {
	r.mu.Lock()
	defer r.mu.Unlock()
	if client, ok := r.clientByTimeout[timeout]; ok {
		return client
	}
	client := &http.Client{Timeout: timeout}
	r.clientByTimeout[timeout] = client
	return client
}

func parseProviderModels(provider string, defaultModel string, body []byte) ([]domain.ProviderModelStatus, error) {
	_ = defaultModel
	now := time.Now().UTC()
	appendModel := func(rows []domain.ProviderModelStatus, name string, raw map[string]any) []domain.ProviderModelStatus {
		name = strings.TrimSpace(name)
		if name == "" {
			return rows
		}
		for _, row := range rows {
			if strings.EqualFold(row.ModelName, name) {
				return rows
			}
		}
		reason := "discovered from provider inventory"
		status := "ready"
		available := true
		if disabled, ok := raw["disabled"].(bool); ok && disabled {
			status = "disabled"
			available = false
			reason = "provider reported model as disabled"
		}
		return append(rows, domain.ProviderModelStatus{
			Provider:   provider,
			ModelName:  name,
			Available:  available,
			Status:     status,
			Reason:     reason,
			ObservedAt: now,
			Metadata:   raw,
		})
	}
	var wrapped struct {
		Data   []map[string]any `json:"data"`
		Models []map[string]any `json:"models"`
	}
	rows := []domain.ProviderModelStatus{}
	if err := json.Unmarshal(body, &wrapped); err == nil {
		for _, item := range wrapped.Data {
			rows = appendModel(rows, anyString(item["id"], item["name"], item["model"]), item)
		}
		for _, item := range wrapped.Models {
			rows = appendModel(rows, anyString(item["id"], item["name"], item["model"]), item)
		}
		if len(rows) > 0 {
			sort.Slice(rows, func(i, j int) bool { return rows[i].ModelName < rows[j].ModelName })
			return rows, nil
		}
	}
	var list []map[string]any
	if err := json.Unmarshal(body, &list); err == nil {
		for _, item := range list {
			rows = appendModel(rows, anyString(item["id"], item["name"], item["model"]), item)
		}
		sort.Slice(rows, func(i, j int) bool { return rows[i].ModelName < rows[j].ModelName })
		return rows, nil
	}
	var raw any
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, err
	}
	return rows, nil
}

func cloneProviderSnapshot(snapshot domain.ProviderCatalogSnapshot) domain.ProviderCatalogSnapshot {
	cloned := snapshot
	cloned.Models = append([]domain.ProviderModelStatus(nil), snapshot.Models...)
	return cloned
}

func anyString(values ...any) string {
	for _, value := range values {
		switch typed := value.(type) {
		case string:
			if strings.TrimSpace(typed) != "" {
				return strings.TrimSpace(typed)
			}
		}
	}
	return ""
}

func sanitizedURL(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	if idx := strings.Index(trimmed, "?"); idx >= 0 {
		trimmed = trimmed[:idx]
	}
	return trimmed
}

func registryEnvBool(key string, fallback bool, aliases ...string) bool {
	for _, candidate := range append([]string{key}, aliases...) {
		value := strings.TrimSpace(os.Getenv(candidate))
		if value == "" {
			continue
		}
		parsed, err := strconv.ParseBool(value)
		return err == nil && parsed
	}
	return fallback
}

func registryEnvDuration(key string, fallback time.Duration, aliases ...string) time.Duration {
	for _, candidate := range append([]string{key}, aliases...) {
		value := strings.TrimSpace(os.Getenv(candidate))
		if value == "" {
			continue
		}
		parsed, err := time.ParseDuration(value)
		if err == nil && parsed > 0 {
			return parsed
		}
	}
	return fallback
}

func registryEnvInt(key string, fallback int, aliases ...string) int {
	for _, candidate := range append([]string{key}, aliases...) {
		value := strings.TrimSpace(os.Getenv(candidate))
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

func maxDuration(left, right time.Duration) time.Duration {
	if left <= 0 {
		return right
	}
	if right <= 0 || left > right {
		return left
	}
	return right
}

func aggregateModelError(models []domain.ProviderModelStatus, fallback string) string {
	for _, model := range models {
		if !model.Available && strings.TrimSpace(model.Reason) != "" {
			return model.ModelName + ": " + model.Reason
		}
	}
	return fallback
}
