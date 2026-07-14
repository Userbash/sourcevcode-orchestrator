package api

import (
	"context"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func (s *Server) providerInventory(providerFilter string, forceProbe ...bool) map[string]any {
	probe := len(forceProbe) > 0 && forceProbe[0]
	healthByProvider := s.orchestrator.ProviderHealth(context.Background(), probe)
	type providerAccumulator struct {
		agents       []domain.AgentInfo
		models       map[string]struct{}
		capabilities map[string]struct{}
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
			row = &providerAccumulator{models: make(map[string]struct{}), capabilities: make(map[string]struct{})}
			accumulators[provider] = row
		}
		row.agents = append(row.agents, agent)
		if agent.ModelName != "" {
			row.models[agent.ModelName] = struct{}{}
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
			accumulators[provider] = &providerAccumulator{models: make(map[string]struct{}), capabilities: make(map[string]struct{})}
		}
		if snapshot, ok := s.orchestrator.ProviderCatalogSnapshot(provider); ok {
			for _, model := range snapshot.Models {
				accumulators[provider].models[model.ModelName] = struct{}{}
			}
		}
	}
	for provider, accumulator := range accumulators {
		models := sortedKeys(accumulator.models)
		capabilities := sortedKeys(accumulator.capabilities)
		health := healthByProvider[provider]
		statusReason := health.Error
		if statusReason == "" && !probe {
			statusReason = "live probe not requested"
		}
		catalog, _ := s.orchestrator.ProviderCatalogSnapshot(provider)
		result[provider] = map[string]any{
			"provider":       provider,
			"status":         health.Status,
			"configured":     health.Configured,
			"available":      health.Available,
			"base_url":       health.BaseURL,
			"runtime":        "go-core",
			"agent_count":    len(accumulator.agents),
			"agents":         accumulator.agents,
			"models":         models,
			"capabilities":   capabilities,
			"observed_at":    health.ObservedAt,
			"status_reason":  statusReason,
			"inventory_mode": "registry",
			"provider_id":    catalog.ProviderID,
			"default_model":  catalog.DefaultModel,
			"catalog":        catalog,
		}
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
			existing["available"] = model.Available
			if model.IsDefault {
				existing["default_for"] = appendUniqueString(existing["default_for"], snapshot.Provider)
			}
			result[model.ModelName] = existing
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
		result[agent.ModelName] = existing
	}
	return result
}

func (s *Server) localModelHealth() map[string]any {
	providerHealth := s.orchestrator.ProviderHealth(context.Background(), true)
	available := providerHealth["local"].Available || providerHealth["ai_kernel"].Available
	status := "unavailable"
	reason := "no configured local provider responded to the models probe"
	if available {
		status, reason = "ok", ""
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
	}
	return map[string]any{
		"status":     status,
		"ready":      health.Available,
		"configured": health.Configured,
		"agents":     agents,
		"health":     health,
		"reason":     health.Error,
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
	return map[string]any{
		"status":      "ok",
		"enabled":     len(agents) > 0,
		"agent_count": len(agents),
		"agents":      agents,
		"runtime":     "go-core",
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

func sortedKeys(values map[string]struct{}) []string {
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func appendUniqueString(raw any, value string) []string {
	items, _ := raw.([]string)
	for _, item := range items {
		if item == value {
			return items
		}
	}
	items = append(items, value)
	sort.Strings(items)
	return items
}
