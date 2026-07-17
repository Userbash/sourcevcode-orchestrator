package api

import (
	"context"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
)

func (s *Server) providerInventory(providerFilter string, forceProbe ...bool) map[string]any {
	probe := len(forceProbe) > 0 && forceProbe[0]
	healthByProvider := s.orchestrator.ProviderHealth(context.Background(), probe)
	type providerAccumulator struct {
		agents        []domain.AgentInfo
		models        map[string]struct{}
		displayModels map[string]struct{}
		capabilities  map[string]struct{}
	}
	accumulators := make(map[string]*providerAccumulator)
	for _, agent := range s.orchestrator.Agents() {
		provider := strings.ToLower(strings.TrimSpace(agent.Provider))
		if provider == "" {
			provider = "unknown"
		}
		if providerFilter != "" && !strings.EqualFold(providerFilter, provider) {
			continue
		}
		row := accumulators[provider]
		if row == nil {
			row = &providerAccumulator{
				models:        make(map[string]struct{}),
				displayModels: make(map[string]struct{}),
				capabilities:  make(map[string]struct{}),
			}
			accumulators[provider] = row
		}
		row.agents = append(row.agents, agent)
		if agent.ModelName != "" {
			row.models[agent.ModelName] = struct{}{}
			row.displayModels[agent.ModelName] = struct{}{}
		}
		for _, capability := range agent.Capabilities {
			row.capabilities[capability] = struct{}{}
		}
	}

	result := make(map[string]any, len(accumulators))
	for provider := range healthByProvider {
		if providerFilter != "" && !strings.EqualFold(providerFilter, provider) {
			continue
		}
		if _, ok := accumulators[provider]; !ok {
			accumulators[provider] = &providerAccumulator{
				models:        make(map[string]struct{}),
				displayModels: make(map[string]struct{}),
				capabilities:  make(map[string]struct{}),
			}
		}
		if snapshot, ok := s.orchestrator.ProviderCatalogSnapshot(provider); ok {
			for _, model := range snapshot.Models {
				accumulators[provider].models[model.ModelName] = struct{}{}
				accumulators[provider].displayModels[model.ModelName] = struct{}{}
				for _, variant := range displayVariants(model.Metadata) {
					if id := strings.TrimSpace(variant.ID); id != "" {
						accumulators[provider].displayModels[id] = struct{}{}
					}
				}
			}
		}
	}
	for provider, accumulator := range accumulators {
		models := sortedKeys(accumulator.models)
		displayModels := sortedKeys(accumulator.displayModels)
		capabilities := sortedKeys(accumulator.capabilities)
		health := healthByProvider[provider]
		statusReason := health.Error
		if statusReason == "" {
			switch {
			case !probe:
				statusReason = "live probe not requested"
			case health.ProbeQueued:
				statusReason = "live probe queued"
			}
		}
		catalog, _ := s.orchestrator.ProviderCatalogSnapshot(provider)
		row := map[string]any{
			"provider":       provider,
			"status":         health.Status,
			"configured":     health.Configured,
			"available":      health.Available,
			"base_url":       health.BaseURL,
			"runtime":        "go-core",
			"agent_count":    len(accumulator.agents),
			"agents":         accumulator.agents,
			"models":         models,
			"display_models": displayModels,
			"capabilities":   capabilities,
			"observed_at":    health.ObservedAt,
			"status_reason":  statusReason,
			"probe_queued":   health.ProbeQueued,
			"cooldown_until": health.CooldownUntil,
			"refresh_after":  health.RefreshAfter,
			"inventory_mode": "registry",
			"provider_id":    catalog.ProviderID,
			"default_model":  catalog.DefaultModel,
			"resource_pools": buildProviderCatalogResourcePools(provider, catalog.Models),
			"catalog":        catalog,
		}
		if collaboration := providerCollaborationProfile(provider); collaboration != nil {
			for key, value := range collaboration {
				row[key] = value
			}
		}
		result[provider] = row
	}
	return result
}

func (s *Server) modelIndex() map[string]any {
	result := make(map[string]any)
	for _, snapshot := range s.orchestrator.ProviderCatalogs() {
		for _, model := range snapshot.Models {
			existing, _ := result[model.ModelName].(map[string]any)
			if existing == nil {
				existing = map[string]any{
					"model_name": model.ModelName,
					"providers":  []string{},
					"agents":     []string{},
					"status":     model.Status,
				}
			}
			existing["providers"] = appendUniqueString(existing["providers"], snapshot.Provider)
			existing["available"] = truthy(existing["available"]) || model.Available
			existing["status"] = mergeModelStatus(existing["status"], model.Status)
			mergeModelMetadata(existing, model.Metadata)
			applyCollaborationMetadata(existing, snapshot.Provider, model.ModelName)
			if strings.TrimSpace(model.Reason) != "" {
				existing["status_reason"] = model.Reason
			}
			existing["inventory_status"] = mergeModelStatus(existing["inventory_status"], model.InventoryStatus)
			existing["transport_status"] = mergeModelStatus(existing["transport_status"], model.TransportStatus)
			existing["verification_status"] = mergeModelStatus(existing["verification_status"], model.VerificationStatus)
			if strings.TrimSpace(model.Transport) != "" {
				existing["transport"] = model.Transport
			}
			if model.LastHTTPStatus != 0 {
				existing["last_http_status"] = model.LastHTTPStatus
			}
			if model.LastProbeLatencyMS > 0 {
				existing["last_probe_latency_ms"] = model.LastProbeLatencyMS
			}
			if model.LastSuccessAt != nil {
				existing["last_success_at"] = model.LastSuccessAt
			}
			if model.ConsecutiveFailures > 0 {
				existing["consecutive_failures"] = model.ConsecutiveFailures
			}
			if model.ConsecutiveSuccesses > 0 {
				existing["consecutive_successes"] = model.ConsecutiveSuccesses
			}
			if model.VerificationIntervalSec > 0 {
				existing["verification_interval_sec"] = model.VerificationIntervalSec
			}
			if model.LastError != nil {
				existing["last_error"] = map[string]any{
					"category":      model.LastError.Category,
					"message":       model.LastError.Message,
					"retryable":     model.LastError.Retryable,
					"http_status":   model.LastError.HTTPStatus,
					"endpoint":      model.LastError.Endpoint,
					"endpoint_kind": model.LastError.EndpointKind,
					"request_id":    model.LastError.RequestID,
					"observed_at":   model.LastError.ObservedAt,
					"latency_ms":    model.LastError.LatencyMS,
				}
			}
			if model.IsDefault {
				existing["default_for"] = appendUniqueString(existing["default_for"], snapshot.Provider)
			}
			result[model.ModelName] = existing

			for _, variant := range displayVariants(model.Metadata) {
				if strings.TrimSpace(variant.ID) == "" {
					continue
				}
				variantExisting, _ := result[variant.ID].(map[string]any)
				if variantExisting == nil {
					variantExisting = map[string]any{
						"model_name":      variant.ID,
						"providers":       []string{},
						"agents":          []string{},
						"status":          model.Status,
						"available":       model.Available,
						"variant_only":    true,
						"routing_model":   model.ModelName,
						"display_name":    variant.DisplayName,
						"reasoning_level": variant.ReasoningLevel,
					}
				}
				variantExisting["providers"] = appendUniqueString(variantExisting["providers"], snapshot.Provider)
				variantExisting["available"] = truthy(variantExisting["available"]) || model.Available
				variantExisting["status"] = mergeModelStatus(variantExisting["status"], model.Status)
				mergeModelMetadata(variantExisting, model.Metadata)
				applyCollaborationMetadata(variantExisting, snapshot.Provider, model.ModelName)
				if strings.TrimSpace(model.Reason) != "" {
					variantExisting["status_reason"] = model.Reason
				}
				variantExisting["inventory_status"] = mergeModelStatus(variantExisting["inventory_status"], model.InventoryStatus)
				variantExisting["transport_status"] = mergeModelStatus(variantExisting["transport_status"], model.TransportStatus)
				variantExisting["verification_status"] = mergeModelStatus(variantExisting["verification_status"], model.VerificationStatus)
				if strings.TrimSpace(model.Transport) != "" {
					variantExisting["transport"] = model.Transport
				}
				if model.LastHTTPStatus != 0 {
					variantExisting["last_http_status"] = model.LastHTTPStatus
				}
				if model.LastProbeLatencyMS > 0 {
					variantExisting["last_probe_latency_ms"] = model.LastProbeLatencyMS
				}
				if model.LastSuccessAt != nil {
					variantExisting["last_success_at"] = model.LastSuccessAt
				}
				if model.ConsecutiveFailures > 0 {
					variantExisting["consecutive_failures"] = model.ConsecutiveFailures
				}
				if model.ConsecutiveSuccesses > 0 {
					variantExisting["consecutive_successes"] = model.ConsecutiveSuccesses
				}
				if model.VerificationIntervalSec > 0 {
					variantExisting["verification_interval_sec"] = model.VerificationIntervalSec
				}
				if model.LastError != nil {
					variantExisting["last_error"] = map[string]any{
						"category":      model.LastError.Category,
						"message":       model.LastError.Message,
						"retryable":     model.LastError.Retryable,
						"http_status":   model.LastError.HTTPStatus,
						"endpoint":      model.LastError.Endpoint,
						"endpoint_kind": model.LastError.EndpointKind,
						"request_id":    model.LastError.RequestID,
						"observed_at":   model.LastError.ObservedAt,
						"latency_ms":    model.LastError.LatencyMS,
					}
				}
				result[variant.ID] = variantExisting
			}
		}
	}
	for _, agent := range s.orchestrator.Agents() {
		if strings.TrimSpace(agent.ModelName) == "" {
			continue
		}
		existing, _ := result[agent.ModelName].(map[string]any)
		if existing == nil {
			existing = map[string]any{
				"model_name": agent.ModelName,
				"providers":  []string{},
				"agents":     []string{},
				"status":     "configured",
			}
		}
		existing["providers"] = appendUniqueString(existing["providers"], agent.Provider)
		existing["agents"] = appendUniqueString(existing["agents"], agent.ID)
		existing["status"] = mergeModelStatus(existing["status"], "configured")
		result[agent.ModelName] = existing
	}
	return result
}

func (s *Server) localModelHealth() map[string]any {
	providerHealth := s.orchestrator.ProviderHealth(context.Background(), true)
	available := providerHealth["local"].Available || providerHealth["ai_kernel"].Available
	pending := providerHealth["local"].ProbeQueued || providerHealth["ai_kernel"].ProbeQueued
	status := "unavailable"
	reason := "no configured local provider responded to the models probe"
	if available {
		status, reason = "ok", ""
	} else if pending {
		status, reason = "pending", "local provider health probe queued"
	}
	var agents []domain.AgentInfo
	for _, agent := range s.orchestrator.Agents() {
		if agent.Provider == "local" || agent.Provider == "ai_kernel" {
			agents = append(agents, agent)
		}
	}
	residentModels := []any{}
	if manager := s.orchestrator.LocalModelManager(); manager != nil {
		snapshot := manager.Snapshot()
		if snapshotResidents, ok := snapshot["resident_models"].([]map[string]any); ok {
			residentModels = make([]any, 0, len(snapshotResidents))
			for _, row := range snapshotResidents {
				residentModels = append(residentModels, row)
			}
		} else if snapshotResidents, ok := snapshot["resident_models"].([]any); ok {
			residentModels = snapshotResidents
		}
	}
	return map[string]any{
		"status":          status,
		"overall_ok":      available,
		"agents":          agents,
		"resident_models": residentModels,
		"provider_health": providerHealth,
		"reason":          reason,
	}
}

func (s *Server) aiKernelGate() map[string]any {
	health := s.orchestrator.ProviderHealth(context.Background(), true)["ai_kernel"]
	var agents []domain.AgentInfo
	for _, agent := range s.orchestrator.Agents() {
		if agent.Provider == "ai_kernel" {
			agents = append(agents, agent)
		}
	}
	status := "unavailable"
	if health.Available {
		status = "ok"
	} else if health.ProbeQueued {
		status = "pending"
	}
	return map[string]any{
		"status":     status,
		"ready":      health.Available,
		"configured": health.Configured,
		"agents":     agents,
		"health":     health,
		"reason":     firstNonEmpty(health.Error, ternaryString(health.ProbeQueued, "live probe queued", "")),
	}
}

func (s *Server) sourcecraftStatus() map[string]any {
	var agents []domain.AgentInfo
	for _, agent := range s.orchestrator.Agents() {
		for _, capability := range agent.Capabilities {
			if capability == "sourcecraft" {
				agents = append(agents, agent)
				break
			}
		}
	}
	status := "degraded"
	if len(agents) > 0 {
		status = "ok"
	}
	return map[string]any{
		"status":                    status,
		"enabled":                   len(agents) > 0,
		"agent_count":               len(agents),
		"agents":                    agents,
		"runtime":                   "go-core",
		"runtime_mode":              "semantic-routing",
		"planning_supported":        true,
		"delegation_supported":      true,
		"mutation_supported":        false,
		"runtime_bridge_configured": false,
		"task_families":             kernel.SourcecraftTaskFamilies(),
		"safe_actions":              kernel.SourcecraftSafeActions(),
		"limitations": []string{
			"repo mutations are not implemented in go-core",
			"git, gh, and src runtime readiness checks are not available",
			"protected-branch and preview-token workflows are not available",
		},
	}
}

func (s *Server) transportAudit() map[string]any {
	return map[string]any{
		"status":               "ok",
		"primary_transport":    "websocket",
		"control_ws_endpoint":  "/control/ws",
		"chat_ws_endpoint":     "/chat/ws",
		"sse_compatibility":    []string{"/events/runtime", "/events/inventory"},
		"actions":              s.dispatcher.Actions(),
		"supported_protocols":  []string{"chat.v1", "chat.json"},
		"python_runtime_calls": 0,
		"inbound_ws_audit": map[string]any{
			"capacity": s.wsAudit.capacity(),
			"entries":  s.wsAudit.snapshot(),
		},
	}
}

func (s *Server) diagnosticsSnapshot(ctx context.Context, layers []string, matrixOnly bool) map[string]any {
	snapshot := s.orchestrator.StateSnapshot(ctx)
	diagnosticLayers := s.filterDiagnosticLayers(s.diagnosticLayers(snapshot), layers)
	payload := map[string]any{
		"schema_version":     "diagnostics.v1",
		"generated_at":       time.Now().UTC(),
		"status":             "ok",
		"matrix_only":        matrixOnly,
		"compatibility_gaps": s.compatibilityGaps(),
		"matrix":             s.routeManifest(),
	}
	if !matrixOnly {
		payload["layers"] = diagnosticLayers
	}
	return payload
}

type displayVariant struct {
	ID             string
	DisplayName    string
	ReasoningLevel string
}

func sortedKeys(values map[string]struct{}) []string {
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func providerCollaborationProfile(provider string) map[string]any {
	if !strings.EqualFold(strings.TrimSpace(provider), "ai_kernel") {
		return nil
	}
	return map[string]any{
		"collaboration_roles":    []string{"primary", "helper", "fallback", "parallel"},
		"recommended_task_types": []string{"code", "fix", "test", "docs", "plan", "review", "research"},
		"support_summary":        "Local kernel can offload repo-local drafting, code execution, verification loops, and fallback work for cloud-routed agents.",
	}
}

func applyCollaborationMetadata(row map[string]any, provider string, modelName string) {
	if row == nil {
		return
	}
	profile := providerCollaborationProfile(provider)
	if profile == nil {
		return
	}
	for key, value := range profile {
		row[key] = value
	}
	if strings.TrimSpace(modelName) != "" {
		row["primary_provider"] = provider
	}
}

func appendUniqueString(raw any, value string) []string {
	items, _ := raw.([]string)
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

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func ternaryString(condition bool, yes string, no string) string {
	if condition {
		return yes
	}
	return no
}

func displayVariants(metadata map[string]any) []displayVariant {
	if len(metadata) == 0 {
		return nil
	}
	raw, ok := metadata["display_variants"]
	if !ok {
		return nil
	}
	list, ok := raw.([]any)
	if !ok {
		if typed, ok := raw.([]map[string]any); ok {
			list = make([]any, 0, len(typed))
			for _, item := range typed {
				list = append(list, item)
			}
		} else {
			return nil
		}
	}
	result := make([]displayVariant, 0, len(list))
	for _, item := range list {
		row, ok := item.(map[string]any)
		if !ok {
			continue
		}
		id := metadataString(row, "id")
		if id == "" {
			continue
		}
		result = append(result, displayVariant{
			ID:             id,
			DisplayName:    metadataString(row, "display_name"),
			ReasoningLevel: metadataString(row, "reasoning_level"),
		})
	}
	return result
}

func buildProviderCatalogResourcePools(provider string, models []domain.ProviderModelStatus) []map[string]any {
	normalized := make([]domain.ProviderModelStatus, 0, len(models))
	for _, model := range models {
		copyModel := model
		if copyModel.Status != "missing" && copyModel.Status != "disabled" && copyModel.InventoryStatus != "inventory_missing" {
			if copyModel.InventoryStatus == "inventory_verified" || copyModel.VerificationStatus == "verifying" || copyModel.IsDefault {
				copyModel.Available = true
				copyModel.VerificationStatus = "confirmed"
			}
		}
		normalized = append(normalized, copyModel)
	}
	return buildProviderResourcePools(provider, normalized)
}

func buildProviderResourcePools(provider string, models []domain.ProviderModelStatus) []map[string]any {
	type poolAccumulator struct {
		provider              string
		family                string
		aliases               []string
		models                []string
		eligibleModels        []string
		eligibleDisplayModels []string
		status                string
		eligible              bool
	}
	pools := map[string]*poolAccumulator{}
	for _, model := range models {
		poolsForModel := metadataStringSlice(model.Metadata, "resource_pools")
		if len(poolsForModel) == 0 {
			if family := metadataString(model.Metadata, "model_family"); family != "" {
				poolsForModel = []string{family}
			}
		}
		if len(poolsForModel) == 0 {
			continue
		}
		aliases := metadataStringSlice(model.Metadata, "family_aliases")
		displayModelNames := []string{model.ModelName}
		for _, variant := range displayVariants(model.Metadata) {
			displayModelNames = appendUniqueSorted(displayModelNames, variant.ID)
		}
		for _, poolName := range poolsForModel {
			poolName = strings.TrimSpace(poolName)
			if poolName == "" {
				continue
			}
			pool := pools[poolName]
			if pool == nil {
				pool = &poolAccumulator{provider: provider, family: metadataString(model.Metadata, "model_family"), status: model.Status}
				if pool.family == "" {
					pool.family = poolName
				}
				pools[poolName] = pool
			}
			pool.aliases = appendUniqueSorted(pool.aliases, aliases...)
			pool.models = appendUniqueSorted(pool.models, model.ModelName)
			if model.Available && strings.EqualFold(strings.TrimSpace(model.VerificationStatus), "confirmed") {
				pool.eligible = true
				pool.eligibleModels = appendUniqueSorted(pool.eligibleModels, model.ModelName)
				pool.eligibleDisplayModels = appendUniqueSorted(pool.eligibleDisplayModels, displayModelNames...)
			}
			pool.status = mergeModelStatus(pool.status, model.Status)
		}
	}
	names := make([]string, 0, len(pools))
	for name := range pools {
		names = append(names, name)
	}
	sort.Strings(names)
	result := make([]map[string]any, 0, len(names))
	for _, name := range names {
		pool := pools[name]
		result = append(result, map[string]any{
			"pool":                    name,
			"family":                  pool.family,
			"aliases":                 pool.aliases,
			"provider":                pool.provider,
			"eligible":                pool.eligible,
			"status":                  pool.status,
			"models":                  pool.models,
			"eligible_models":         pool.eligibleModels,
			"eligible_display_models": pool.eligibleDisplayModels,
		})
	}
	return result
}

func mergeModelMetadata(target map[string]any, metadata map[string]any) {
	if len(target) == 0 || len(metadata) == 0 {
		return
	}
	if family := metadataString(metadata, "model_family"); family != "" {
		target["model_family"] = family
	}
	if pools := metadataStringSlice(metadata, "resource_pools"); len(pools) > 0 {
		target["resource_pools"] = appendUniqueSorted(metadataStringSlice(target, "resource_pools"), pools...)
	}
	if aliases := metadataStringSlice(metadata, "family_aliases"); len(aliases) > 0 {
		target["family_aliases"] = appendUniqueSorted(metadataStringSlice(target, "family_aliases"), aliases...)
	}
}

func appendUniqueSorted(items []string, values ...string) []string {
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		found := false
		for _, item := range items {
			if item == value {
				found = true
				break
			}
		}
		if !found {
			items = append(items, value)
		}
	}
	sort.Strings(items)
	return items
}

func metadataStringSlice(metadata map[string]any, key string) []string {
	if len(metadata) == 0 {
		return nil
	}
	raw, ok := metadata[key]
	if !ok {
		return nil
	}
	switch typed := raw.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		result := make([]string, 0, len(typed))
		for _, item := range typed {
			if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
				result = append(result, strings.TrimSpace(text))
			}
		}
		sort.Strings(result)
		return result
	default:
		return nil
	}
}

func metadataString(metadata map[string]any, key string) string {
	if len(metadata) == 0 {
		return ""
	}
	value, _ := metadata[key].(string)
	return strings.TrimSpace(value)
}

func truthy(raw any) bool {
	value, _ := raw.(bool)
	return value
}

func mergeModelStatus(current any, next string) string {
	existing, _ := current.(string)
	next = strings.TrimSpace(next)
	if existing == "" {
		return next
	}
	if next == "" {
		return existing
	}
	rank := map[string]int{
		"validation_failed":      80,
		"registration_overflow":  78,
		"missing":                75,
		"inventory_missing":      74,
		"disabled":               70,
		"transport_failed":       68,
		"endpoint_misconfigured": 67,
		"registration_stale":     64,
		"transport_stale":        63,
		"retryable_failure":      62,
		"verification_cooldown":  60,
		"verification_pending":   58,
		"registration_queued":    56,
		"verifying":              54,
		"stale":                  52,
		"unconfirmed":            50,
		"transport_pending":      48,
		"unavailable":            40,
		"degraded":               35,
		"inventory_verified":     30,
		"configured":             20,
		"transport_verified":     15,
		"confirmed":              12,
		"ready":                  10,
	}
	if rank[next] > rank[existing] {
		return next
	}
	return existing
}
