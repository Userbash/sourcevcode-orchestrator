package kernel

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
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
	catalog         *modelCatalog
}

type inventorySource struct {
	name string
	url  string
}

type modelProbeResult struct {
	Validated bool
	Retryable bool
	Reason    string
}

func NewProviderModelRegistry(configs map[string]agents.OpenAICompatibleConfig) *ProviderModelRegistry {
	copyConfigs := make(map[string]agents.OpenAICompatibleConfig, len(configs))
	for key, cfg := range maps.Clone(configs) {
		copyConfigs[strings.ToLower(strings.TrimSpace(key))] = cfg
	}
	catalog, err := loadModelCatalog(firstNonEmptyString(os.Getenv("GO_CORE_MODEL_CATALOG_PATH"), os.Getenv("AI_BRIDGE_MODEL_CATALOG_PATH")))
	if err != nil {
		catalog = nil
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
		catalog:         catalog,
	}
}

func (r *ProviderModelRegistry) Start(ctx context.Context) {
	if r == nil || !r.enabled {
		return
	}
	if ctx == nil {
		ctx = context.Background()
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
				r.Refresh(ctx)
			}
		}
	}()
}

func (r *ProviderModelRegistry) Refresh(ctx context.Context) {
	if r == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
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
	models, sourceErrors := r.fetchProviderModels(ctx, provider, cfg)
	if len(models) == 0 {
		snapshot.Status = "unavailable"
		snapshot.Error = aggregateSourceErrors(sourceErrors, "provider returned an empty model inventory")
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
			Metadata:   annotateProviderModelMetadata(cfg.DefaultModel, nil),
		}}, models...)
	}
	if r.shouldValidateProvider(cfg) && len(models) > 0 {
		models = r.validateDiscoveredModels(ctx, cfg, models)
	}
	if r.catalog != nil {
		models = r.catalog.filter(provider, models)
		_ = r.catalog.syncProvider(provider, models)
	}

	snapshot.Models = models
	availableCount := 0
	probeFailureCount := 0
	unavailableCount := 0
	for _, model := range models {
		if model.Available {
			availableCount++
			if model.Status == "probe_failed" {
				probeFailureCount++
			}
		} else {
			unavailableCount++
		}
	}
	snapshot.Available = availableCount > 0
	switch {
	case availableCount == 0:
		snapshot.Status = "unavailable"
		if snapshot.Error == "" {
			snapshot.Error = aggregateSourceErrors(sourceErrors, aggregateModelError(models, "no discovered models are available"))
		}
	case unavailableCount > 0 || probeFailureCount > 0 || len(sourceErrors) > 0:
		snapshot.Status = "degraded"
		if snapshot.Error == "" {
			snapshot.Error = aggregateSourceErrors(sourceErrors, aggregateModelError(models, "one or more models are degraded"))
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
	if !cfg.Configured() {
		return false
	}
	return strings.TrimSpace(cfg.ChatCompletionsURL()) != ""
}

func (r *ProviderModelRegistry) validateDiscoveredModels(ctx context.Context, cfg agents.OpenAICompatibleConfig, models []domain.ProviderModelStatus) []domain.ProviderModelStatus {
	limit := r.validationLimit
	if limit <= 0 || limit > len(models) {
		limit = len(models)
	}
	for i := 0; i < limit; i++ {
		result := r.validateModelViaChat(ctx, cfg, models[i].ModelName)
		models[i].ObservedAt = time.Now().UTC()
		if models[i].Metadata == nil {
			models[i].Metadata = map[string]any{}
		}
		if result.Validated {
			models[i].Available = true
			models[i].Status = "ready"
			models[i].Reason = ""
			models[i].Metadata["probe_status"] = "validated"
			continue
		}
		models[i].Reason = result.Reason
		if result.Retryable {
			models[i].Available = true
			models[i].Status = "probe_failed"
			models[i].Metadata["probe_status"] = "transient_failure"
			continue
		}
		models[i].Available = false
		models[i].Status = "validation_failed"
		models[i].Metadata["probe_status"] = "validation_failed"
	}
	for i := limit; i < len(models); i++ {
		models[i].ObservedAt = time.Now().UTC()
		if models[i].Metadata == nil {
			models[i].Metadata = map[string]any{}
		}
		if strings.TrimSpace(models[i].Status) == "" || models[i].Status == "ready" {
			models[i].Status = "discovered"
		}
		models[i].Metadata["probe_status"] = "not_probed"
	}
	return models
}

func (r *ProviderModelRegistry) validateModelViaChat(ctx context.Context, cfg agents.OpenAICompatibleConfig, model string) modelProbeResult {
	url := cfg.ChatCompletionsURL()
	if strings.TrimSpace(url) == "" {
		return modelProbeResult{Reason: "chat completions endpoint is not configured"}
	}
	payload := map[string]any{
		"model":      model,
		"messages":   []map[string]string{{"role": "user", "content": "ping"}},
		"max_tokens": 1,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return modelProbeResult{Reason: err.Error()}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return modelProbeResult{Reason: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	if strings.TrimSpace(cfg.APIKey) != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	}
	resp, err := r.httpClient(maxDuration(cfg.Timeout, r.timeout)).Do(req)
	if err != nil {
		return modelProbeResult{Retryable: true, Reason: err.Error()}
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	msg := strings.TrimSpace(string(b))
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		var probe struct {
			Error any    `json:"error"`
			ID    string `json:"id"`
		}
		if len(b) > 0 && json.Unmarshal(b, &probe) == nil && probe.Error != nil {
			return modelProbeResult{Reason: fmt.Sprintf("provider returned error payload: %v", probe.Error)}
		}
		return modelProbeResult{Validated: true}
	}
	if msg == "" {
		msg = resp.Status
	}
	return modelProbeResult{Retryable: isRetryableProbeFailure(resp.StatusCode, msg), Reason: msg}
}

func (r *ProviderModelRegistry) fetchProviderModels(ctx context.Context, provider string, cfg agents.OpenAICompatibleConfig) ([]domain.ProviderModelStatus, []string) {
	sources := []inventorySource{{name: "models", url: cfg.ModelsURL()}}
	if codexURL := strings.TrimSpace(cfg.CodexURL()); codexURL != "" && codexURL != cfg.ModelsURL() {
		sources = append(sources, inventorySource{name: "codex", url: codexURL})
	}
	merged := []domain.ProviderModelStatus{}
	seen := map[string]int{}
	var errs []string
	for _, source := range sources {
		rows, err := r.fetchProviderModelsFromSource(ctx, provider, cfg, source)
		if err != nil {
			errs = append(errs, err.Error())
			continue
		}
		for _, row := range rows {
			key := strings.ToLower(strings.TrimSpace(row.ModelName))
			if idx, ok := seen[key]; ok {
				merged[idx] = mergeProviderModelStatus(merged[idx], row, source.name)
				continue
			}
			if row.Metadata == nil {
				row.Metadata = map[string]any{}
			}
			row.Metadata["inventory_source"] = source.name
			row.Metadata["inventory_sources"] = []string{source.name}
			merged = append(merged, row)
			seen[key] = len(merged) - 1
		}
	}
	sort.Slice(merged, func(i, j int) bool { return merged[i].ModelName < merged[j].ModelName })
	return merged, errs
}

func (r *ProviderModelRegistry) fetchProviderModelsFromSource(ctx context.Context, provider string, cfg agents.OpenAICompatibleConfig, source inventorySource) ([]domain.ProviderModelStatus, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, source.url, nil)
	if err != nil {
		return nil, fmt.Errorf("%s inventory request: %w", source.name, err)
	}
	if key := strings.TrimSpace(cfg.APIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := r.httpClient(maxDuration(cfg.Timeout, r.timeout)).Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s inventory request failed: %w", source.name, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		msg := strings.TrimSpace(string(body))
		if msg == "" {
			msg = resp.Status
		}
		return nil, fmt.Errorf("%s inventory request failed: %s", source.name, msg)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, fmt.Errorf("%s inventory read failed: %w", source.name, err)
	}
	rows, err := parseProviderModels(provider, cfg.DefaultModel, body)
	if err != nil {
		return nil, fmt.Errorf("%s inventory parse failed: %w", source.name, err)
	}
	for i := range rows {
		if rows[i].Metadata == nil {
			rows[i].Metadata = map[string]any{}
		}
		rows[i].Metadata["inventory_source"] = source.name
	}
	return rows, nil
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
		status := "discovered"
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
			Metadata:   annotateProviderModelMetadata(name, raw),
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
			rows = appendDisplayVariants(rows)
			sort.Slice(rows, func(i, j int) bool { return rows[i].ModelName < rows[j].ModelName })
			return rows, nil
		}
	}
	var list []map[string]any
	if err := json.Unmarshal(body, &list); err == nil {
		for _, item := range list {
			rows = appendModel(rows, anyString(item["id"], item["name"], item["model"]), item)
		}
		rows = appendDisplayVariants(rows)
		sort.Slice(rows, func(i, j int) bool { return rows[i].ModelName < rows[j].ModelName })
		return rows, nil
	}
	var raw any
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, err
	}
	rows = collectProviderModels(provider, raw, rows, now)
	rows = appendDisplayVariants(rows)
	if len(rows) > 0 {
		sort.Slice(rows, func(i, j int) bool { return rows[i].ModelName < rows[j].ModelName })
		return rows, nil
	}
	return rows, nil
}

func collectProviderModels(provider string, raw any, rows []domain.ProviderModelStatus, now time.Time) []domain.ProviderModelStatus {
	appendModel := func(name string, item map[string]any) {
		name = strings.TrimSpace(name)
		if name == "" {
			return
		}
		for _, row := range rows {
			if strings.EqualFold(row.ModelName, name) {
				return
			}
		}
		status := "discovered"
		available := true
		reason := "discovered from provider inventory"
		if disabled, ok := item["disabled"].(bool); ok && disabled {
			status = "disabled"
			available = false
			reason = "provider reported model as disabled"
		}
		rows = append(rows, domain.ProviderModelStatus{
			Provider:   provider,
			ModelName:  name,
			Available:  available,
			Status:     status,
			Reason:     reason,
			ObservedAt: now,
			Metadata:   annotateProviderModelMetadata(name, item),
		})
	}
	switch typed := raw.(type) {
	case map[string]any:
		if name := anyString(typed["id"], typed["name"], typed["model"], typed["slug"]); name != "" {
			appendModel(name, typed)
		}
		for _, value := range typed {
			rows = collectProviderModels(provider, value, rows, now)
		}
	case []any:
		for _, value := range typed {
			rows = collectProviderModels(provider, value, rows, now)
		}
	}
	return rows
}

func appendDisplayVariants(rows []domain.ProviderModelStatus) []domain.ProviderModelStatus {
	for i := range rows {
		if rows[i].Metadata == nil {
			continue
		}
		levels := stringSliceMetadata(rows[i].Metadata["supported_reasoning_levels"])
		if len(levels) == 0 {
			continue
		}
		variants := make([]map[string]any, 0, len(levels))
		for _, level := range levels {
			level = strings.TrimSpace(level)
			if level == "" {
				continue
			}
			variants = append(variants, map[string]any{
				"id":              rows[i].ModelName + " " + level,
				"display_name":    rows[i].ModelName + " " + level,
				"routing_model":   rows[i].ModelName,
				"reasoning_level": level,
			})
		}
		if len(variants) > 0 {
			rows[i].Metadata["display_variants"] = variants
		}
	}
	return rows
}

func annotateProviderModelMetadata(modelName string, metadata map[string]any) map[string]any {
	if metadata == nil {
		metadata = map[string]any{}
	}
	family := inferModelFamily(modelName)
	if family == "" {
		return metadata
	}
	metadata["model_family"] = family
	metadata["resource_pool"] = family
	aliases := familyAliases(family)
	if len(aliases) > 0 {
		metadata["family_aliases"] = aliases
	}
	pools := []string{family}
	for _, alias := range aliases {
		pools = appendUniqueString(pools, alias)
	}
	metadata["resource_pools"] = pools
	return metadata
}

func inferModelFamily(modelName string) string {
	name := strings.ToLower(strings.TrimSpace(modelName))
	switch {
	case strings.HasPrefix(name, "claude-"):
		return "claude"
	case strings.HasPrefix(name, "gpt-"), strings.HasPrefix(name, "o1"), strings.HasPrefix(name, "o3"), strings.HasPrefix(name, "o4"):
		return "gpt"
	case strings.HasPrefix(name, "gemini-"):
		return "gemini"
	case strings.HasPrefix(name, "deepseek-"):
		return "deepseek"
	case strings.HasPrefix(name, "glm-"):
		return "glm"
	case strings.HasPrefix(name, "kimi-"):
		return "kimi"
	case strings.HasPrefix(name, "qwen-"):
		return "qwen"
	case strings.HasPrefix(name, "mistral-"):
		return "mistral"
	case strings.HasPrefix(name, "llama-"):
		return "llama"
	default:
		return ""
	}
}

func familyAliases(family string) []string {
	switch strings.ToLower(strings.TrimSpace(family)) {
	case "claude":
		return []string{"anthropic"}
	case "gpt":
		return []string{"openai"}
	default:
		return nil
	}
}

func mergeProviderModelStatus(left, right domain.ProviderModelStatus, source string) domain.ProviderModelStatus {
	merged := left
	merged.Available = left.Available || right.Available
	merged.IsDefault = left.IsDefault || right.IsDefault
	if merged.Metadata == nil {
		merged.Metadata = map[string]any{}
	}
	for key, value := range right.Metadata {
		if _, exists := merged.Metadata[key]; !exists {
			merged.Metadata[key] = value
		}
	}
	existing := stringSliceMetadata(merged.Metadata["inventory_sources"])
	merged.Metadata["inventory_sources"] = appendUniqueString(existing, source)
	return merged
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

func stringSliceMetadata(value any) []string {
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		result := make([]string, 0, len(typed))
		for _, item := range typed {
			if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
				result = append(result, strings.TrimSpace(text))
			}
		}
		return result
	}
	return nil
}

func appendUniqueString(items []string, value string) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return items
	}
	for _, item := range items {
		if item == value {
			return items
		}
	}
	items = append(items, value)
	sort.Strings(items)
	return items
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
		if model.Status == "probe_failed" && strings.TrimSpace(model.Reason) != "" {
			return model.ModelName + ": " + model.Reason
		}
		if !model.Available && strings.TrimSpace(model.Reason) != "" {
			return model.ModelName + ": " + model.Reason
		}
	}
	return fallback
}

func aggregateSourceErrors(errors []string, fallback string) string {
	if len(errors) == 0 {
		return fallback
	}
	return strings.Join(errors, "; ")
}

func isRetryableProbeFailure(statusCode int, reason string) bool {
	if statusCode == http.StatusTooManyRequests || statusCode >= 500 {
		return true
	}
	normalized := strings.ToLower(strings.TrimSpace(reason))
	for _, token := range []string{"busy", "overload", "temporar", "timeout", "try again", "unavailable", "upstream"} {
		if strings.Contains(normalized, token) {
			return true
		}
	}
	return false
}
