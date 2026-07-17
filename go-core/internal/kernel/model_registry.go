package kernel

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/providerhttp"
)

type ProviderModelRegistry struct {
	mu                 sync.RWMutex
	configs            map[string]agents.OpenAICompatibleConfig
	snapshots          map[string]domain.ProviderCatalogSnapshot
	clientByTimeout    map[time.Duration]*http.Client
	refreshInterval    time.Duration
	timeout            time.Duration
	enabled            bool
	validateModels     bool
	validationLimit    int
	pendingTTL         time.Duration
	confirmationTTL    time.Duration
	retryCooldown      time.Duration
	unregisteredQLimit int
	catalog            *modelCatalog
	refreshing         bool
}

type inventorySource struct {
	name     string
	url      string
	optional bool
}

type modelProbeResult struct {
	Validated   bool
	Retryable   bool
	Reason      string
	Transport   string
	HTTPStatus  int
	LatencyMS   int64
	RequestID   string
	Error       *domain.ProviderAPIError
	ObservedAt  time.Time
	SucceededAt *time.Time
}

type inventorySourceError struct {
	source     inventorySource
	statusCode int
	message    string
	cause      error
}

func (e *inventorySourceError) Error() string {
	prefix := e.source.name + " inventory request failed"
	if e.cause != nil {
		return fmt.Sprintf("%s: %v", prefix, e.cause)
	}
	if strings.TrimSpace(e.message) != "" {
		return fmt.Sprintf("%s: %s", prefix, e.message)
	}
	return prefix
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
		configs:            copyConfigs,
		snapshots:          make(map[string]domain.ProviderCatalogSnapshot),
		clientByTimeout:    make(map[time.Duration]*http.Client),
		refreshInterval:    registryEnvDuration("AI_BRIDGE_MODEL_REFRESH_INTERVAL", 5*time.Minute, "GO_CORE_MODEL_REGISTRY_REFRESH_INTERVAL"),
		timeout:            registryEnvDuration("AI_BRIDGE_MODEL_REFRESH_TIMEOUT", 20*time.Second, "GO_CORE_MODEL_REGISTRY_TIMEOUT"),
		enabled:            registryEnvBool("AI_BRIDGE_MODEL_REFRESH_ENABLED", true, "GO_CORE_MODEL_REGISTRY_ENABLED"),
		validateModels:     registryEnvBool("AI_BRIDGE_MODEL_VALIDATE_MODELS", true),
		validationLimit:    registryEnvInt("AI_BRIDGE_MODEL_VALIDATE_LIMIT", 12),
		pendingTTL:         registryEnvDuration("AI_BRIDGE_MODEL_PENDING_TTL", 30*time.Minute, "GO_CORE_MODEL_REGISTRY_PENDING_TTL"),
		confirmationTTL:    registryEnvDuration("AI_BRIDGE_MODEL_CONFIRMATION_TTL", 6*time.Hour, "GO_CORE_MODEL_REGISTRY_CONFIRMATION_TTL"),
		retryCooldown:      registryEnvDuration("AI_BRIDGE_MODEL_RETRY_COOLDOWN", 10*time.Minute, "GO_CORE_MODEL_REGISTRY_RETRY_COOLDOWN"),
		unregisteredQLimit: registryEnvInt("AI_BRIDGE_MODEL_QUEUE_LIMIT", 64, "GO_CORE_MODEL_REGISTRY_QUEUE_LIMIT"),
		catalog:            catalog,
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
	if !r.beginRefresh(true, time.Now().UTC()) {
		return
	}
	defer r.finishRefresh()
	r.refreshSnapshots(ctx)
}

func (r *ProviderModelRegistry) RefreshIfStale(ctx context.Context) {
	if r == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	now := time.Now().UTC()
	if !r.beginRefresh(false, now) {
		return
	}
	defer r.finishRefresh()
	r.refreshSnapshots(ctx)
}

func (r *ProviderModelRegistry) refreshSnapshots(ctx context.Context) {
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
	if !providerSnapshotRoutingUsable(snapshot, time.Now().UTC()) {
		return nil
	}
	result := make([]domain.ProviderModelStatus, 0, len(snapshot.Models))
	for _, model := range snapshot.Models {
		if providerModelRoutingReady(model) {
			result = append(result, model)
		}
	}
	return result
}

func providerSnapshotRoutingUsable(snapshot domain.ProviderCatalogSnapshot, now time.Time) bool {
	if snapshot.ObservedAt.IsZero() {
		return false
	}
	ttl := time.Duration(snapshot.RefreshIntervalSec) * time.Second
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	if ttl < time.Minute {
		ttl = time.Minute
	}
	return !snapshot.ObservedAt.Add(2 * ttl).Before(now)
}

func (r *ProviderModelRegistry) beginRefresh(force bool, now time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.refreshing {
		return false
	}
	if !force && !r.needsRefreshLocked(now) {
		return false
	}
	r.refreshing = true
	return true
}

func (r *ProviderModelRegistry) finishRefresh() {
	r.mu.Lock()
	r.refreshing = false
	r.mu.Unlock()
}

func (r *ProviderModelRegistry) needsRefreshLocked(now time.Time) bool {
	ttl := r.refreshInterval
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	if ttl < time.Minute {
		ttl = time.Minute
	}
	for provider := range r.configs {
		snapshot, ok := r.snapshots[provider]
		if !ok {
			return true
		}
		if snapshot.ObservedAt.IsZero() || now.Sub(snapshot.ObservedAt) >= ttl {
			return true
		}
	}
	return false
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
	if placeholderEndpointConfigured(cfg.BaseURL, cfg.ModelsURL(), cfg.ChatCompletionsURL(), cfg.ResponsesURL(), cfg.MessagesURL(), cfg.CodexURL()) {
		snapshot.Error = "provider endpoint uses a placeholder host and must be replaced with a real upstream URL"
		return snapshot
	}

	ctx, cancel := context.WithTimeout(parent, maxDuration(cfg.Timeout, r.timeout))
	defer cancel()
	models, sourceErrors := r.fetchProviderModels(ctx, provider, cfg)
	if len(models) == 0 && strings.TrimSpace(cfg.DefaultModel) != "" {
		models = append(models, domain.ProviderModelStatus{
			Provider:                provider,
			ModelName:               cfg.DefaultModel,
			Available:               false,
			Status:                  "verification_pending",
			Reason:                  "configured default model requires direct transport verification because provider inventory is unavailable",
			InventoryStatus:         "inventory_unavailable",
			TransportStatus:         "transport_pending",
			VerificationStatus:      "verifying",
			ObservedAt:              now,
			IsDefault:               true,
			VerificationIntervalSec: int(r.refreshInterval.Seconds()),
			Metadata:                annotateProviderModelMetadata(cfg.DefaultModel, map[string]any{"synthetic_inventory": true}),
		})
	}
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
			Provider:                provider,
			ModelName:               cfg.DefaultModel,
			Available:               false,
			Status:                  "missing",
			Reason:                  "configured default model is not present in provider inventory",
			InventoryStatus:         "inventory_missing",
			TransportStatus:         "transport_unavailable",
			VerificationStatus:      "unconfirmed",
			ObservedAt:              now,
			IsDefault:               true,
			VerificationIntervalSec: int(r.refreshInterval.Seconds()),
			Metadata:                annotateProviderModelMetadata(cfg.DefaultModel, nil),
		}}, models...)
	}
	if r.shouldValidateProvider(cfg) && len(models) > 0 {
		previousModels := map[string]domain.ProviderModelStatus{}
		if previousSnapshot, ok := r.Snapshot(provider); ok {
			previousModels = indexProviderModels(previousSnapshot.Models)
		}
		models = r.validateDiscoveredModels(ctx, cfg, models, previousModels, now)
	}
	if r.catalog != nil {
		models = r.catalog.filter(provider, models)
		_ = r.catalog.syncProvider(provider, models)
	}

	snapshot.Models = models
	confirmedCount := 0
	pendingCount := 0
	failedCount := 0
	inapplicableCount := 0
	for _, model := range models {
		switch strings.TrimSpace(model.VerificationStatus) {
		case "confirmed":
			confirmedCount++
		case "verifying":
			pendingCount++
		case "skipped":
			inapplicableCount++
		default:
			failedCount++
		}
	}
	snapshot.Available = confirmedCount > 0
	switch {
	case confirmedCount > 0 && (pendingCount > 0 || failedCount > 0 || len(sourceErrors) > 0):
		snapshot.Status = "degraded"
		if snapshot.Error == "" {
			snapshot.Error = aggregateSourceErrors(sourceErrors, aggregateModelError(models, "one or more models are degraded or still verifying"))
		}
	case confirmedCount > 0:
		snapshot.Status = "ready"
	case pendingCount > 0:
		snapshot.Status = "verifying"
		if snapshot.Error == "" {
			snapshot.Error = aggregateSourceErrors(sourceErrors, aggregateModelError(models, "provider inventory discovered models that are still verifying"))
		}
	case inapplicableCount > 0 && failedCount == 0 && len(sourceErrors) == 0:
		snapshot.Status = "unavailable"
		if snapshot.Error == "" {
			snapshot.Error = aggregateModelError(models, "provider inventory does not contain models compatible with the configured validation transport")
		}
	default:
		snapshot.Status = "unavailable"
		if snapshot.Error == "" {
			snapshot.Error = aggregateSourceErrors(sourceErrors, aggregateModelError(models, "no confirmed models are available"))
		}
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
	return strings.TrimSpace(cfg.ChatCompletionsURL()) != "" || strings.TrimSpace(cfg.ResponsesURL()) != "" || strings.TrimSpace(cfg.MessagesURL()) != ""
}

func (r *ProviderModelRegistry) validateDiscoveredModels(ctx context.Context, cfg agents.OpenAICompatibleConfig, models []domain.ProviderModelStatus, previous map[string]domain.ProviderModelStatus, now time.Time) []domain.ProviderModelStatus {
	verificationIntervalSec := int(r.refreshInterval.Seconds())
	for i := range models {
		models[i] = mergeProviderLifecycle(models[i], previous[modelLifecycleKey(models[i].ModelName)], now, verificationIntervalSec)
	}

	queueLimit := r.unregisteredQLimit
	if queueLimit <= 0 {
		queueLimit = len(models)
	}

	ordered := prioritizeModelValidation(models, cfg.DefaultModel)
	probeIndexes := make([]int, 0, minInt(r.validationLimit, len(ordered)))
	queuedIndexes := make([]int, 0, len(ordered))

	for _, idx := range ordered {
		if !canParticipateInRegistration(models[idx]) {
			continue
		}
		if keepConfirmedModelFresh(&models[idx], now, r.confirmationTTL) {
			continue
		}
		if reason := inapplicableValidationReason(models[idx], cfg); reason != "" {
			markModelTransportInapplicable(&models[idx], now, reason)
			continue
		}
		prepareModelForRegistration(&models[idx], now, r.pendingTTL, r.confirmationTTL)
		if !registrationDue(models[idx], now) {
			markModelCooldown(&models[idx], now)
			continue
		}
		if len(queuedIndexes) >= queueLimit {
			markModelQueueOverflow(&models[idx], now)
			continue
		}
		queuedIndexes = append(queuedIndexes, idx)
		if r.validationLimit <= 0 || len(probeIndexes) < r.validationLimit {
			probeIndexes = append(probeIndexes, idx)
			continue
		}
		markModelQueued(&models[idx], now, len(queuedIndexes))
	}

	for _, idx := range probeIndexes {
		startModelVerification(&models[idx], now, len(probeIndexes))
		result := r.validateModelViaTransport(ctx, cfg, models[idx].ModelName)
		applyProbeResult(&models[idx], result, now, r.retryCooldown, r.confirmationTTL)
	}

	for _, idx := range queuedIndexes[len(probeIndexes):] {
		markModelQueued(&models[idx], now, indexInQueue(queuedIndexes, idx)+1)
	}

	return models
}

func prioritizeModelValidation(models []domain.ProviderModelStatus, defaultModel string) []int {
	defaultModel = strings.TrimSpace(defaultModel)
	ordered := make([]int, 0, len(models))
	seen := make(map[int]struct{}, len(models))
	if defaultModel != "" {
		for i := range models {
			if strings.EqualFold(models[i].ModelName, defaultModel) {
				ordered = append(ordered, i)
				seen[i] = struct{}{}
				break
			}
		}
	}
	for i := range models {
		if _, ok := seen[i]; ok {
			continue
		}
		ordered = append(ordered, i)
	}
	return ordered
}

func indexProviderModels(models []domain.ProviderModelStatus) map[string]domain.ProviderModelStatus {
	indexed := make(map[string]domain.ProviderModelStatus, len(models))
	for _, model := range models {
		key := modelLifecycleKey(model.ModelName)
		if key == "" {
			continue
		}
		indexed[key] = model
	}
	return indexed
}

func modelLifecycleKey(name string) string {
	return strings.ToLower(strings.TrimSpace(name))
}

func mergeProviderLifecycle(model domain.ProviderModelStatus, previous domain.ProviderModelStatus, now time.Time, verificationIntervalSec int) domain.ProviderModelStatus {
	model.ObservedAt = now
	model.VerificationIntervalSec = verificationIntervalSec
	if model.Metadata == nil {
		model.Metadata = map[string]any{}
	}
	if previous.ModelName == "" {
		firstSeen := now
		model.FirstSeenAt = &firstSeen
		lastState := now
		model.LastStateChangeAt = &lastState
		return model
	}
	model.FirstSeenAt = cloneTimePtr(previous.FirstSeenAt)
	if model.FirstSeenAt == nil {
		firstSeen := now
		model.FirstSeenAt = &firstSeen
	}
	model.VerificationStartedAt = cloneTimePtr(previous.VerificationStartedAt)
	model.LastStateChangeAt = cloneTimePtr(previous.LastStateChangeAt)
	model.LastSuccessAt = cloneTimePtr(previous.LastSuccessAt)
	model.NextVerificationAt = cloneTimePtr(previous.NextVerificationAt)
	model.ExpiresAt = cloneTimePtr(previous.ExpiresAt)
	model.ConsecutiveFailures = previous.ConsecutiveFailures
	model.ConsecutiveSuccesses = previous.ConsecutiveSuccesses
	model.RegistrationAttempts = previous.RegistrationAttempts
	model.PendingCycles = previous.PendingCycles
	model.QueueStatus = strings.TrimSpace(previous.QueueStatus)
	model.QueuePosition = previous.QueuePosition
	if model.Transport == "" {
		model.Transport = previous.Transport
	}
	if model.LastHTTPStatus == 0 {
		model.LastHTTPStatus = previous.LastHTTPStatus
	}
	if model.LastProbeLatencyMS == 0 {
		model.LastProbeLatencyMS = previous.LastProbeLatencyMS
	}
	if model.LastError == nil {
		model.LastError = previous.LastError
	}
	if model.Metadata == nil {
		model.Metadata = map[string]any{}
	}
	for k, v := range previous.Metadata {
		if _, ok := model.Metadata[k]; !ok {
			model.Metadata[k] = v
		}
	}
	return model
}

func canParticipateInRegistration(model domain.ProviderModelStatus) bool {
	switch model.Status {
	case "disabled", "missing", "transport_inapplicable":
		return false
	default:
		return true
	}
}

func keepConfirmedModelFresh(model *domain.ProviderModelStatus, now time.Time, confirmationTTL time.Duration) bool {
	if strings.TrimSpace(model.VerificationStatus) != "confirmed" || !model.Available {
		return false
	}
	if model.ExpiresAt == nil {
		if model.LastSuccessAt == nil {
			return false
		}
		expiresAt := model.LastSuccessAt.Add(confirmationTTL)
		model.ExpiresAt = &expiresAt
	}
	if now.After(*model.ExpiresAt) {
		setModelState(model, "registration_stale", "inventory_verified", "transport_stale", "stale", "confirmed model reached its verification TTL and must be re-registered", now)
		model.Available = false
		return false
	}
	setModelState(model, "ready", "inventory_verified", "transport_verified", "confirmed", "", now)
	model.QueueStatus = "confirmed"
	model.QueuePosition = 0
	model.NextVerificationAt = cloneTimePtr(model.ExpiresAt)
	return true
}

func prepareModelForRegistration(model *domain.ProviderModelStatus, now time.Time, pendingTTL time.Duration, confirmationTTL time.Duration) {
	if model.InventoryStatus == "" {
		model.InventoryStatus = "inventory_verified"
	}
	if model.TransportStatus == "" || model.TransportStatus == "transport_verified" {
		model.TransportStatus = "transport_pending"
	}
	if model.VerificationStartedAt == nil {
		started := now
		model.VerificationStartedAt = &started
	} else if pendingTTL > 0 && now.Sub(*model.VerificationStartedAt) >= pendingTTL {
		setModelState(model, "registration_stale", model.InventoryStatus, "transport_pending", "stale", "model exceeded the pending registration TTL and is waiting for a retry slot", now)
	}
	if model.LastSuccessAt != nil && confirmationTTL > 0 && model.ExpiresAt == nil {
		expiresAt := model.LastSuccessAt.Add(confirmationTTL)
		model.ExpiresAt = &expiresAt
	}
	model.Available = false
	if strings.TrimSpace(model.VerificationStatus) == "confirmed" {
		model.VerificationStatus = "stale"
	}
	if model.Status == "" || model.Status == "ready" {
		setModelState(model, "verification_pending", model.InventoryStatus, model.TransportStatus, "verifying", "model discovered in provider inventory and awaiting transport verification", now)
	}
	model.PendingCycles++
}

func registrationDue(model domain.ProviderModelStatus, now time.Time) bool {
	if model.NextVerificationAt == nil {
		return true
	}
	return !now.Before(*model.NextVerificationAt)
}

func markModelCooldown(model *domain.ProviderModelStatus, now time.Time) {
	reason := "model is cooling down before the next registration attempt"
	if model.VerificationStatus == "stale" {
		reason = "confirmed model expired and is cooling down before re-registration"
	}
	setModelState(model, "verification_cooldown", model.InventoryStatus, model.TransportStatus, verificationStatusOr(*model, "cooldown"), reason, now)
	model.QueueStatus = "cooldown"
	model.QueuePosition = 0
	model.Available = false
	model.Metadata["probe_status"] = "cooldown"
}

func markModelTransportInapplicable(model *domain.ProviderModelStatus, now time.Time, reason string) {
	model.Available = false
	model.NextVerificationAt = nil
	model.LastError = nil
	model.QueueStatus = "skipped"
	model.QueuePosition = 0
	setModelState(model, "transport_inapplicable", "inventory_verified", "transport_inapplicable", "skipped", reason, now)
	model.Metadata["probe_status"] = "inapplicable"
}

func markModelQueued(model *domain.ProviderModelStatus, now time.Time, position int) {
	setModelState(model, "registration_queued", model.InventoryStatus, "transport_pending", verificationStatusOr(*model, "verifying"), "model is queued for registration", now)
	model.QueueStatus = "queued"
	model.QueuePosition = position
	model.Available = false
	model.Metadata["probe_status"] = "queued"
}

func markModelQueueOverflow(model *domain.ProviderModelStatus, now time.Time) {
	setModelState(model, "registration_overflow", model.InventoryStatus, "transport_pending", verificationStatusOr(*model, "verifying"), "model could not enter the registration queue because the queue limit was reached", now)
	model.QueueStatus = "overflow"
	model.QueuePosition = 0
	model.Available = false
	model.Metadata["probe_status"] = "overflow"
}

func startModelVerification(model *domain.ProviderModelStatus, now time.Time, _ int) {
	if model.VerificationStartedAt == nil {
		started := now
		model.VerificationStartedAt = &started
	}
	model.RegistrationAttempts++
	setModelState(model, "verification_pending", model.InventoryStatus, "transport_pending", "verifying", "model registration is in progress", now)
	model.QueueStatus = "probing"
	model.QueuePosition = 0
	model.Metadata["probe_status"] = "probing"
}

func applyProbeResult(model *domain.ProviderModelStatus, result modelProbeResult, now time.Time, retryCooldown time.Duration, confirmationTTL time.Duration) {
	model.ObservedAt = result.ObservedAt
	model.Transport = result.Transport
	model.LastHTTPStatus = result.HTTPStatus
	model.LastProbeLatencyMS = result.LatencyMS
	if result.RequestID != "" {
		model.Metadata["request_id"] = result.RequestID
	}
	if result.Validated {
		markModelConfirmed(model, result, now, confirmationTTL)
		return
	}
	markModelFailure(model, result, now, retryCooldown)
}

func markModelConfirmed(model *domain.ProviderModelStatus, result modelProbeResult, now time.Time, confirmationTTL time.Duration) {
	model.Available = true
	model.LastError = nil
	model.ConsecutiveFailures = 0
	model.ConsecutiveSuccesses++
	model.PendingCycles = 0
	model.QueueStatus = "confirmed"
	model.QueuePosition = 0
	succeededAt := result.SucceededAtOr(now)
	model.LastSuccessAt = &succeededAt
	if confirmationTTL > 0 {
		expiresAt := succeededAt.Add(confirmationTTL)
		model.ExpiresAt = &expiresAt
		model.NextVerificationAt = &expiresAt
	} else {
		model.ExpiresAt = nil
		model.NextVerificationAt = nil
	}
	setModelState(model, "ready", "inventory_verified", "transport_verified", "confirmed", "", now)
	model.Metadata["probe_status"] = "validated"
}

func markModelFailure(model *domain.ProviderModelStatus, result modelProbeResult, now time.Time, retryCooldown time.Duration) {
	model.Reason = result.Reason
	model.LastError = result.Error
	model.ConsecutiveFailures++
	model.ConsecutiveSuccesses = 0
	model.Available = false
	if result.Retryable {
		if retryCooldown > 0 {
			nextAttempt := now.Add(retryCooldown)
			model.NextVerificationAt = &nextAttempt
		}
		setModelState(model, "verification_cooldown", model.InventoryStatus, "retryable_failure", "verifying", result.Reason, now)
		model.QueueStatus = "cooldown"
		model.QueuePosition = 0
		model.Metadata["probe_status"] = "transient_failure"
		return
	}
	model.NextVerificationAt = nil
	setModelState(model, "validation_failed", model.InventoryStatus, failureTransportStatus(result.Error), "unconfirmed", result.Reason, now)
	model.QueueStatus = "failed"
	model.QueuePosition = 0
	model.Metadata["probe_status"] = "validation_failed"
}

func setModelState(model *domain.ProviderModelStatus, status, inventoryStatus, transportStatus, verificationStatus, reason string, now time.Time) {
	if inventoryStatus != "" {
		model.InventoryStatus = inventoryStatus
	}
	if transportStatus != "" {
		model.TransportStatus = transportStatus
	}
	if verificationStatus != "" {
		model.VerificationStatus = verificationStatus
	}
	if model.Status != status || model.Reason != reason {
		changedAt := now
		model.LastStateChangeAt = &changedAt
	}
	model.Status = status
	model.Reason = reason
}

func cloneTimePtr(value *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}

func firstNonNilTime(values ...*time.Time) *time.Time {
	for _, value := range values {
		if value == nil {
			continue
		}
		return cloneTimePtr(value)
	}
	return nil
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func indexInQueue(queue []int, target int) int {
	for i, idx := range queue {
		if idx == target {
			return i
		}
	}
	return -1
}

func verificationStatusOr(model domain.ProviderModelStatus, fallback string) string {
	if strings.TrimSpace(model.VerificationStatus) != "" {
		return model.VerificationStatus
	}
	return fallback
}

func (r modelProbeResult) SucceededAtOr(fallback time.Time) time.Time {
	if r.SucceededAt != nil {
		return *r.SucceededAt
	}
	return fallback
}

func (r *ProviderModelRegistry) validateModelViaTransport(ctx context.Context, cfg agents.OpenAICompatibleConfig, model string) modelProbeResult {
	transport, url := selectValidationTransport(cfg)
	observedAt := time.Now().UTC()
	if strings.TrimSpace(url) == "" {
		err := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", EndpointKind: transport, Message: "no validation transport endpoint is configured", Category: "unsupported_endpoint", ObservedAt: observedAt}
		return modelProbeResult{Reason: err.Message, Transport: transport, Error: err, ObservedAt: observedAt}
	}
	payload, contentType, err := validationPayload(transport, model)
	if err != nil {
		apiErr := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", Endpoint: sanitizedURL(url), EndpointKind: transport, Message: err.Error(), Category: "invalid_request", ObservedAt: observedAt}
		return modelProbeResult{Reason: apiErr.Message, Transport: transport, Error: apiErr, ObservedAt: observedAt}
	}
	started := time.Now()
	response, err := providerhttp.Do(ctx, providerhttp.RequestConfig{
		ProviderID:   cfg.EffectiveProviderID(),
		BaseURL:      cfg.BaseURL,
		APIKey:       cfg.APIKey,
		TrafficClass: "probe",
		Client:       r.httpClient(maxDuration(cfg.Timeout, r.timeout)),
	}, http.MethodPost, url, payload, contentType, 4096)
	latencyMS := time.Since(started).Milliseconds()
	if err != nil {
		apiErr := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", Endpoint: sanitizedURL(url), EndpointKind: transport, Message: err.Error(), Category: "network_error", Retryable: true, ObservedAt: observedAt, LatencyMS: latencyMS}
		return modelProbeResult{Reason: apiErr.Message, Retryable: true, Transport: transport, LatencyMS: latencyMS, Error: apiErr, ObservedAt: observedAt}
	}
	b := response.Body
	requestID := strings.TrimSpace(response.Header.Get("x-request-id"))
	msg := strings.TrimSpace(string(b))
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		var probe struct {
			Error any    `json:"error"`
			ID    string `json:"id"`
		}
		if len(b) == 0 {
			apiErr := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", Endpoint: sanitizedURL(url), EndpointKind: transport, HTTPStatus: response.StatusCode, Message: "provider returned an empty response body", Category: "empty_response", RequestID: requestID, ObservedAt: observedAt, LatencyMS: latencyMS}
			return modelProbeResult{Reason: apiErr.Message, Transport: transport, HTTPStatus: response.StatusCode, LatencyMS: latencyMS, RequestID: requestID, Error: apiErr, ObservedAt: observedAt}
		}
		if err := json.Unmarshal(b, &probe); err != nil {
			apiErr := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", Endpoint: sanitizedURL(url), EndpointKind: transport, HTTPStatus: response.StatusCode, Message: err.Error(), Category: "bad_response_schema", RequestID: requestID, ObservedAt: observedAt, LatencyMS: latencyMS}
			return modelProbeResult{Reason: apiErr.Message, Transport: transport, HTTPStatus: response.StatusCode, LatencyMS: latencyMS, RequestID: requestID, Error: apiErr, ObservedAt: observedAt}
		}
		if probe.Error != nil {
			message := fmt.Sprintf("provider returned error payload: %v", probe.Error)
			apiErr := &domain.ProviderAPIError{Provider: cfg.Provider, Model: model, Operation: "probe", Endpoint: sanitizedURL(url), EndpointKind: transport, HTTPStatus: response.StatusCode, Message: message, Category: "invalid_request", RequestID: requestID, ObservedAt: observedAt, LatencyMS: latencyMS}
			return modelProbeResult{Reason: message, Transport: transport, HTTPStatus: response.StatusCode, LatencyMS: latencyMS, RequestID: requestID, Error: apiErr, ObservedAt: observedAt}
		}
		succeededAt := observedAt
		return modelProbeResult{Validated: true, Transport: transport, HTTPStatus: response.StatusCode, LatencyMS: latencyMS, RequestID: requestID, ObservedAt: observedAt, SucceededAt: &succeededAt}
	}
	if msg == "" {
		msg = http.StatusText(response.StatusCode)
	}
	apiErr := classifyProviderAPIError(cfg.Provider, model, "probe", transport, sanitizedURL(url), response.StatusCode, msg, requestID, observedAt, latencyMS)
	return modelProbeResult{Reason: apiErr.Message, Retryable: apiErr.Retryable, Transport: transport, HTTPStatus: response.StatusCode, LatencyMS: latencyMS, RequestID: requestID, Error: apiErr, ObservedAt: observedAt}
}

func (r *ProviderModelRegistry) fetchProviderModels(ctx context.Context, provider string, cfg agents.OpenAICompatibleConfig) ([]domain.ProviderModelStatus, []string) {
	sources := []inventorySource{{name: "models", url: cfg.ModelsURL()}}
	if codexURL := strings.TrimSpace(cfg.CodexURL()); codexURL != "" && codexURL != cfg.ModelsURL() {
		sources = append(sources, inventorySource{name: "codex", url: codexURL, optional: true})
	}
	merged := []domain.ProviderModelStatus{}
	seen := map[string]int{}
	var errs []string
	for _, source := range sources {
		rows, err := r.fetchProviderModelsFromSource(ctx, provider, cfg, source)
		if err != nil {
			if shouldIgnoreInventorySourceError(source, err, len(merged) > 0) {
				continue
			}
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
	response, err := providerhttp.Do(ctx, providerhttp.RequestConfig{
		ProviderID:   cfg.EffectiveProviderID(),
		BaseURL:      cfg.BaseURL,
		APIKey:       cfg.APIKey,
		TrafficClass: "inventory",
		Client:       r.httpClient(maxDuration(cfg.Timeout, r.timeout)),
	}, http.MethodGet, source.url, nil, "", 2<<20)
	if err != nil {
		return nil, &inventorySourceError{source: source, cause: err}
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		msg := strings.TrimSpace(string(response.Body))
		if msg == "" {
			msg = http.StatusText(response.StatusCode)
		}
		return nil, &inventorySourceError{source: source, statusCode: response.StatusCode, message: msg}
	}
	rows, err := parseProviderModels(provider, cfg.DefaultModel, response.Body)
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
		reason := "model discovered in provider inventory and awaiting transport verification"
		status := "verification_pending"
		available := false
		inventoryStatus := "inventory_verified"
		transportStatus := "transport_pending"
		verificationStatus := "verifying"
		if disabled, ok := raw["disabled"].(bool); ok && disabled {
			status = "disabled"
			available = false
			reason = "provider reported model as disabled"
			transportStatus = "transport_unavailable"
			verificationStatus = "unconfirmed"
		}
		return append(rows, domain.ProviderModelStatus{
			Provider:           provider,
			ModelName:          name,
			Available:          available,
			Status:             status,
			Reason:             reason,
			InventoryStatus:    inventoryStatus,
			TransportStatus:    transportStatus,
			VerificationStatus: verificationStatus,
			ObservedAt:         now,
			Metadata:           annotateProviderModelMetadata(name, raw),
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
		status := "verification_pending"
		available := false
		reason := "model discovered in provider inventory and awaiting transport verification"
		inventoryStatus := "inventory_verified"
		transportStatus := "transport_pending"
		verificationStatus := "verifying"
		if disabled, ok := item["disabled"].(bool); ok && disabled {
			status = "disabled"
			available = false
			reason = "provider reported model as disabled"
			transportStatus = "transport_unavailable"
			verificationStatus = "unconfirmed"
		}
		rows = append(rows, domain.ProviderModelStatus{
			Provider:           provider,
			ModelName:          name,
			Available:          available,
			Status:             status,
			Reason:             reason,
			InventoryStatus:    inventoryStatus,
			TransportStatus:    transportStatus,
			VerificationStatus: verificationStatus,
			ObservedAt:         now,
			Metadata:           annotateProviderModelMetadata(name, item),
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
	case strings.HasPrefix(name, "gemma-"), strings.HasPrefix(name, "gemma"), strings.Contains(name, "gemma"):
		return "gemma"
	case strings.HasPrefix(name, "deepseek-"):
		return "deepseek"
	case strings.HasPrefix(name, "glm-"):
		return "glm"
	case strings.HasPrefix(name, "kimi-"):
		return "kimi"
	case strings.HasPrefix(name, "qwen-"), strings.HasPrefix(name, "qwen"), strings.Contains(name, "qwen"):
		return "qwen"
	case strings.HasPrefix(name, "mistral-"), strings.HasPrefix(name, "codestral-"), strings.HasPrefix(name, "devstral-"), strings.HasPrefix(name, "magistral-"), strings.Contains(name, "mistral"):
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

func boolMetadata(value any) (bool, bool) {
	switch typed := value.(type) {
	case bool:
		return typed, true
	case string:
		trimmed := strings.TrimSpace(typed)
		if trimmed == "" {
			return false, false
		}
		parsed, err := strconv.ParseBool(trimmed)
		if err != nil {
			return false, false
		}
		return parsed, true
	default:
		return false, false
	}
}

func metadataMap(value any) map[string]any {
	switch typed := value.(type) {
	case map[string]any:
		return typed
	case map[string]string:
		converted := make(map[string]any, len(typed))
		for key, item := range typed {
			converted[key] = item
		}
		return converted
	default:
		return nil
	}
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

func inapplicableValidationReason(model domain.ProviderModelStatus, cfg agents.OpenAICompatibleConfig) string {
	transport, _ := selectValidationTransport(cfg)
	if transport == "" || transport == "unsupported" {
		return ""
	}
	if modelSupportsTransport(model, transport) {
		return ""
	}
	return fmt.Sprintf("model is present in provider inventory but does not support the configured %s validation transport", transport)
}

func modelSupportsTransport(model domain.ProviderModelStatus, transport string) bool {
	if transport == "" || transport == "unsupported" {
		return true
	}
	if capabilities := metadataMap(model.Metadata["capabilities"]); capabilities != nil {
		switch transport {
		case "chat_completions":
			if supported, ok := boolMetadata(capabilities["completion_chat"]); ok {
				return supported
			}
		case "responses":
			if supported, ok := boolMetadata(capabilities["responses"]); ok {
				return supported
			}
		case "messages":
			if supported, ok := boolMetadata(capabilities["messages"]); ok {
				return supported
			}
		}
	}

	name := strings.ToLower(strings.TrimSpace(model.ModelName))
	if name == "" {
		return true
	}
	nonChatMarkers := []string{"embed", "embedding", "moderation", "ocr", "realtime", "transcribe", "tts"}
	for _, marker := range nonChatMarkers {
		if strings.Contains(name, marker) {
			return false
		}
	}
	return true
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

func placeholderEndpointConfigured(endpoints ...string) bool {
	for _, endpoint := range endpoints {
		if placeholderEndpoint(endpoint) {
			return true
		}
	}
	return false
}

func placeholderEndpoint(raw string) bool {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return false
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return false
	}
	host := strings.ToLower(strings.TrimSpace(parsed.Hostname()))
	if host == "" {
		return false
	}
	return host == "example" || strings.HasSuffix(host, ".example") || strings.Contains(host, ".example.")
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

func selectValidationTransport(cfg agents.OpenAICompatibleConfig) (string, string) {
	switch {
	case strings.TrimSpace(cfg.ChatCompletionsURL()) != "":
		return "chat_completions", cfg.ChatCompletionsURL()
	case strings.TrimSpace(cfg.ResponsesURL()) != "":
		return "responses", cfg.ResponsesURL()
	case strings.TrimSpace(cfg.MessagesURL()) != "":
		return "messages", cfg.MessagesURL()
	default:
		return "unsupported", ""
	}
}

func validationPayload(transport string, model string) ([]byte, string, error) {
	var payload map[string]any
	switch transport {
	case "chat_completions":
		payload = map[string]any{"model": model, "messages": []map[string]string{{"role": "user", "content": "ping"}}, "max_tokens": 1}
	case "responses":
		payload = map[string]any{"model": model, "input": "ping", "max_output_tokens": 1}
	case "messages":
		payload = map[string]any{"model": model, "max_tokens": 1, "messages": []map[string]any{{"role": "user", "content": []map[string]any{{"type": "text", "text": "ping"}}}}}
	default:
		return nil, "", fmt.Errorf("no supported validation payload for transport %q", transport)
	}
	body, err := json.Marshal(payload)
	return body, "application/json", err
}

func classifyProviderAPIError(provider string, model string, operation string, endpointKind string, endpoint string, statusCode int, message string, requestID string, observedAt time.Time, latencyMS int64) *domain.ProviderAPIError {
	category := "invalid_request"
	retryable := isRetryableProbeFailure(statusCode, message)
	normalized := strings.ToLower(strings.TrimSpace(message))
	switch {
	case statusCode >= 100 && statusCode < 200:
		category = "bad_response_schema"
		retryable = false
	case statusCode >= 300 && statusCode < 400:
		category = "redirect_error"
		retryable = false
	case statusCode == http.StatusBadRequest:
		category = "invalid_request"
	case statusCode == http.StatusUnauthorized:
		category = "auth_error"
	case statusCode == http.StatusForbidden:
		category = "permission_error"
	case statusCode == http.StatusNotFound:
		category = categorizeNotFound(endpointKind, normalized)
		retryable = false
	case statusCode == http.StatusRequestTimeout:
		category = "timeout"
		retryable = true
	case statusCode == http.StatusConflict:
		category = "provider_internal_error"
	case statusCode == http.StatusUnprocessableEntity:
		category = "invalid_request"
		retryable = false
	case statusCode == http.StatusTooManyRequests:
		category = "rate_limit"
		retryable = true
	case statusCode >= 500:
		category = "upstream_unavailable"
		retryable = true
	}
	if looksLikeRateLimitMessage(normalized) {
		category = "rate_limit"
		retryable = true
	}
	if strings.Contains(normalized, "model") && strings.Contains(normalized, "not found") {
		category = "model_not_found"
		retryable = false
	}
	if strings.Contains(normalized, "unsupported") {
		category = "unsupported_model"
		retryable = false
	}
	return &domain.ProviderAPIError{Provider: provider, Model: model, Operation: operation, Endpoint: endpoint, EndpointKind: endpointKind, HTTPStatus: statusCode, Message: message, Retryable: retryable, Category: category, RequestID: requestID, ObservedAt: observedAt, LatencyMS: latencyMS}
}

func categorizeNotFound(endpointKind string, message string) string {
	if strings.Contains(message, "model") {
		return "model_not_found"
	}
	switch endpointKind {
	case "chat_completions":
		return "chat_endpoint_not_found"
	case "responses":
		return "provider_route_not_found"
	case "messages":
		return "provider_route_not_found"
	default:
		return "endpoint_misconfigured"
	}
}

func failureTransportStatus(err *domain.ProviderAPIError) string {
	if err == nil {
		return "transport_failed"
	}
	switch err.Category {
	case "chat_endpoint_not_found", "provider_route_not_found", "endpoint_misconfigured", "redirect_error":
		return "endpoint_misconfigured"
	case "unsupported_model", "model_not_found", "invalid_request", "auth_error", "permission_error":
		return "transport_failed"
	default:
		if err.Retryable {
			return "retryable_failure"
		}
		return "transport_failed"
	}
}

func aggregateModelError(models []domain.ProviderModelStatus, fallback string) string {
	for _, model := range models {
		if model.LastError != nil && strings.TrimSpace(model.LastError.Message) != "" {
			return model.ModelName + ": " + model.LastError.Message
		}
		if strings.TrimSpace(model.Reason) != "" {
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

func shouldIgnoreInventorySourceError(source inventorySource, err error, haveModels bool) bool {
	if !source.optional || !haveModels {
		return false
	}
	var sourceErr *inventorySourceError
	if !errors.As(err, &sourceErr) {
		return false
	}
	if sourceErr.statusCode == http.StatusNotFound {
		return true
	}
	return looksLikeNotFoundMessage(sourceErr.message)
}

func looksLikeRateLimitMessage(reason string) bool {
	normalized := strings.ToLower(strings.TrimSpace(reason))
	if normalized == "" {
		return false
	}
	for _, token := range []string{"service_busy", "rate_limit_error", "rate limit", "too many requests", "retry after"} {
		if strings.Contains(normalized, token) {
			return true
		}
	}
	return false
}

func looksLikeNotFoundMessage(reason string) bool {
	normalized := strings.ToLower(strings.TrimSpace(reason))
	if normalized == "" {
		return false
	}
	return strings.Contains(normalized, "\"code\":\"not_found\"") ||
		(strings.Contains(normalized, "\"type\":\"invalid_request_error\"") && strings.Contains(normalized, "not found")) ||
		strings.Contains(normalized, " not found")
}

func isRetryableProbeFailure(statusCode int, reason string) bool {
	if statusCode == http.StatusTooManyRequests || statusCode >= 500 {
		return true
	}
	normalized := strings.ToLower(strings.TrimSpace(reason))
	for _, token := range []string{"busy", "overload", "temporar", "timeout", "try again", "unavailable", "upstream", "rate limit", "retry after"} {
		if strings.Contains(normalized, token) {
			return true
		}
	}
	return false
}
