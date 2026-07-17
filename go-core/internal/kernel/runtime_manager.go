package kernel

import (
	"context"
	"math"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

type RuntimeManager struct {
	registry         *Registry
	providerRegistry *ProviderModelRegistry
	probeProviders   func(context.Context, bool) map[string]domain.ProviderHealth
	mu               sync.RWMutex
	routingWeights   map[string]float64
}

type CapacitySnapshot struct {
	InFlight        int
	AgentSlotUsage  float64
	ModelSlotUsage  float64
	GlobalSlotUsage float64
}

func NewRuntimeManager(registry *Registry, providerRegistry *ProviderModelRegistry, probeProviders func(context.Context, bool) map[string]domain.ProviderHealth) *RuntimeManager {
	return &RuntimeManager{
		registry:         registry,
		providerRegistry: providerRegistry,
		probeProviders:   probeProviders,
		routingWeights:   map[string]float64{},
	}
}

func (m *RuntimeManager) State(agentID string) (domain.AgentRuntimeState, bool) {
	state, ok := m.registry.RuntimeState(agentID)
	if !ok {
		agent, found := m.registry.AgentByID(agentID)
		if !found {
			return domain.AgentRuntimeState{}, false
		}
		state = m.defaultState(agent.Info())
		m.registry.SetRuntimeState(state)
		return state, true
	}
	state = m.normalizeState(state)
	m.registry.SetRuntimeState(state)
	return state, true
}

func (m *RuntimeManager) Allows(info domain.AgentInfo) bool {
	state, ok := m.State(info.ID)
	if !ok {
		return false
	}
	if state.Status == domain.AgentStatusOffline || state.Status == domain.AgentStatusMaintenance {
		return false
	}
	if state.SuppressedUntil != nil && state.SuppressedUntil.After(time.Now().UTC()) {
		return false
	}
	return true
}

func (m *RuntimeManager) RoutingWeight(agentID string) float64 {
	m.mu.RLock()
	if weight, ok := m.routingWeights[agentID]; ok {
		m.mu.RUnlock()
		return weight
	}
	m.mu.RUnlock()
	weights := m.RefreshRoutingWeights()
	return weights[agentID]
}

func (m *RuntimeManager) RefreshRoutingWeights() map[string]float64 {
	weights := make(map[string]float64)
	for _, info := range m.registry.AgentInfos() {
		state, _ := m.State(info.ID)
		weight := runtimePriorityWeight(state)
		state.PriorityScore = weight
		state.UpdatedAt = time.Now().UTC()
		m.registry.SetRuntimeState(state)
		weights[info.ID] = weight
	}
	m.mu.Lock()
	m.routingWeights = weights
	m.mu.Unlock()
	return cloneFloatMap(weights)
}

func (m *RuntimeManager) providerPressure(provider string) float64 {
	provider = strings.ToLower(strings.TrimSpace(provider))
	if provider == "" {
		return 0
	}
	states := m.registry.RuntimeStates()
	maxPressure := 0.0
	totalPressure := 0.0
	count := 0.0
	for _, state := range states {
		if !strings.EqualFold(strings.TrimSpace(state.Provider), provider) {
			continue
		}
		pressure := liveLoadPressure(state)
		if pressure > maxPressure {
			maxPressure = pressure
		}
		totalPressure += pressure
		count++
	}
	if count == 0 {
		return 0
	}
	avgPressure := totalPressure / count
	return clampRuntimeLoad(maxRuntimeFloat(maxPressure*0.65, avgPressure))
}

func (m *RuntimeManager) workerClassPressure(workerClass string, provider string) float64 {
	workerClass = strings.ToLower(strings.TrimSpace(workerClass))
	provider = strings.ToLower(strings.TrimSpace(provider))
	if workerClass == "" {
		return 0
	}
	maxPressure := 0.0
	totalPressure := 0.0
	count := 0.0
	for _, agent := range m.registry.Agents() {
		info := agent.Info()
		if provider != "" && !strings.EqualFold(strings.TrimSpace(info.Provider), provider) {
			continue
		}
		if !runtimeAgentMatchesWorkerClass(info, workerClass) {
			continue
		}
		state, ok := m.State(info.ID)
		if !ok {
			continue
		}
		pressure := liveLoadPressure(state)
		if pressure > maxPressure {
			maxPressure = pressure
		}
		totalPressure += pressure
		count++
	}
	if count == 0 {
		return 0
	}
	avgPressure := totalPressure / count
	return clampRuntimeLoad(maxRuntimeFloat(maxPressure*0.60, avgPressure))
}

func runtimePriorityWeight(state domain.AgentRuntimeState) float64 {
	weight := 1.0
	switch state.Status {
	case domain.AgentStatusReady:
		weight = 1.0
	case domain.AgentStatusBusy:
		weight = 0.8
	case domain.AgentStatusDegraded:
		weight = 0.45
	case domain.AgentStatusMaintenance, domain.AgentStatusOffline:
		weight = 0.0
	default:
		weight = 0.5
	}
	if state.SuppressedUntil != nil && state.SuppressedUntil.After(time.Now().UTC()) {
		return 0.0
	}
	if state.ErrorRate > 0 {
		weight *= math.Max(0.05, 1-state.ErrorRate)
	}
	loadPenalty := math.Max(state.AgentSlotUsage, math.Max(state.ModelSlotUsage, state.GlobalSlotUsage))
	if loadPenalty > 0 {
		weight *= math.Max(0.10, 1-loadPenalty*0.65)
	}
	if weight < 0 {
		return 0
	}
	if weight > 1 {
		return 1
	}
	return weight
}

func (m *RuntimeManager) RoutingWeights() map[string]float64 {
	m.mu.RLock()
	if len(m.routingWeights) == 0 {
		m.mu.RUnlock()
		return m.RefreshRoutingWeights()
	}
	weights := cloneFloatMap(m.routingWeights)
	m.mu.RUnlock()
	return weights
}

func (m *RuntimeManager) UpdateCapacitySnapshot(agentID string, snapshot CapacitySnapshot) (domain.AgentRuntimeState, bool) {
	if m == nil {
		return domain.AgentRuntimeState{}, false
	}
	state, ok := m.State(strings.TrimSpace(agentID))
	if !ok {
		return domain.AgentRuntimeState{}, false
	}
	state.InFlight = maxRuntimeInt(snapshot.InFlight, 0)
	state.AgentSlotUsage = clampRuntimeLoad(snapshot.AgentSlotUsage)
	state.ModelSlotUsage = clampRuntimeLoad(snapshot.ModelSlotUsage)
	state.GlobalSlotUsage = clampRuntimeLoad(snapshot.GlobalSlotUsage)
	state.UpdatedAt = time.Now().UTC()
	m.registry.SetRuntimeState(state)
	m.RefreshRoutingWeights()
	return state, true
}

func (m *RuntimeManager) CapacityPressure(info domain.AgentInfo, workerClass string) float64 {
	if m == nil {
		return 0.5
	}
	state, ok := m.State(info.ID)
	if !ok {
		return 0.5
	}
	providerPressure := m.providerPressure(info.Provider)
	classPressure := m.workerClassPressure(workerClass, info.Provider)
	pressure := maxRuntimeFloat(
		liveLoadPressure(state),
		state.ErrorRate,
		providerPressure,
		classPressure,
	)
	return clampRuntimeLoad(pressure)
}

func (m *RuntimeManager) SuppressLane(agentID string, reason string, seconds int) (domain.AgentRuntimeState, bool) {
	state, ok := m.State(agentID)
	if !ok {
		return domain.AgentRuntimeState{}, false
	}
	if seconds <= 0 {
		seconds = 300
	}
	until := time.Now().UTC().Add(time.Duration(seconds) * time.Second)
	state.Status = domain.AgentStatusMaintenance
	state.DisabledReason = firstNonEmptyString(strings.TrimSpace(reason), "suppressed by runtime policy")
	state.SuppressedUntil = &until
	state.UpdatedAt = time.Now().UTC()
	m.registry.SetRuntimeState(state)
	m.RefreshRoutingWeights()
	return state, true
}

func (m *RuntimeManager) RecoverLane(agentID string) (domain.AgentRuntimeState, bool) {
	state, ok := m.State(agentID)
	if !ok {
		return domain.AgentRuntimeState{}, false
	}
	state.SuppressedUntil = nil
	state.DisabledReason = ""
	if agent, found := m.registry.AgentByID(agentID); found {
		state.Status = agent.Info().Status
	}
	if state.ErrorRate >= 0.25 {
		state.Status = domain.AgentStatusDegraded
	}
	if state.ErrorRate < 0.25 && state.Status == domain.AgentStatusMaintenance {
		state.Status = domain.AgentStatusReady
	}
	state.UpdatedAt = time.Now().UTC()
	m.registry.SetRuntimeState(state)
	m.RefreshRoutingWeights()
	return state, true
}

func (m *RuntimeManager) RecordRuntimeFailure(agentID string, detail string) domain.AgentRuntimeState {
	state, _ := m.State(agentID)
	state.ErrorRate = math.Min(1, state.ErrorRate+0.25)
	state.LastError = strings.TrimSpace(detail)
	if state.ErrorRate >= 0.75 {
		state.Status = domain.AgentStatusMaintenance
	} else {
		state.Status = domain.AgentStatusDegraded
	}
	state.UpdatedAt = time.Now().UTC()
	m.registry.SetRuntimeState(state)
	m.RefreshRoutingWeights()
	return state
}

func (m *RuntimeManager) RecordSuccess(agentID string) domain.AgentRuntimeState {
	state, _ := m.State(agentID)
	state.ErrorRate = math.Max(0, state.ErrorRate-0.5)
	if state.SuppressedUntil == nil || !state.SuppressedUntil.After(time.Now().UTC()) {
		if state.ErrorRate < 0.25 {
			state.Status = domain.AgentStatusReady
			state.DisabledReason = ""
		} else {
			state.Status = domain.AgentStatusDegraded
		}
	}
	state.LastError = ""
	state.UpdatedAt = time.Now().UTC()
	m.registry.SetRuntimeState(state)
	m.RefreshRoutingWeights()
	return state
}

func (m *RuntimeManager) QuarantineAgent(agentID string, reason string) domain.AgentRuntimeState {
	state, _ := m.SuppressLane(agentID, firstNonEmptyString(strings.TrimSpace(reason), "quarantined after repeated runtime failures"), 900)
	return state
}

func (m *RuntimeManager) RecoveryActionForFailure(agentID string) string {
	state, ok := m.State(agentID)
	if !ok {
		return "unknown"
	}
	if state.ErrorRate >= 0.75 {
		return "quarantine_agent"
	}
	if state.ErrorRate >= 0.25 {
		return "degrade_lane"
	}
	return "observe"
}

func (m *RuntimeManager) ProbeProviderRuntime(ctx context.Context, provider string) map[string]any {
	provider = strings.TrimSpace(strings.ToLower(provider))
	if provider == "" {
		return map[string]any{"status": "error", "error": "provider is required"}
	}
	if m.probeProviders == nil {
		return map[string]any{"status": "unavailable", "error": "provider probe callback is not configured"}
	}
	healths := m.probeProviders(ctx, true)
	health, ok := healths[provider]
	if !ok {
		return map[string]any{"status": "error", "error": "provider not found", "provider": provider}
	}
	catalogSnapshot, hasCatalogSnapshot := domain.ProviderCatalogSnapshot{}, false
	if m.providerRegistry != nil {
		catalogSnapshot, hasCatalogSnapshot = m.providerRegistry.Snapshot(provider)
	}
	states := make([]domain.AgentRuntimeState, 0)
	for _, agent := range m.registry.Agents() {
		info := agent.Info()
		if strings.ToLower(info.Provider) != provider {
			continue
		}
		state, _ := m.State(info.ID)
		state.Provider = info.Provider
		state.LastProbeStatus = health.Status
		state.LastProbeError = health.Error
		if !health.Configured {
			state.Status = domain.AgentStatusOffline
			state.DisabledReason = "provider not configured"
		} else if !health.Available {
			state.Status = domain.AgentStatusDegraded
			state.DisabledReason = firstNonEmptyString(health.Error, "provider probe failed")
		} else if confirmed, reason := m.confirmedAgentModel(agent, catalogSnapshot, hasCatalogSnapshot); !confirmed {
			state.Status = domain.AgentStatusDegraded
			state.DisabledReason = reason
		} else if state.SuppressedUntil == nil || !state.SuppressedUntil.After(time.Now().UTC()) {
			state.Status = domain.AgentStatusReady
			if state.ErrorRate < 0.25 {
				state.DisabledReason = ""
			}
		}
		state.UpdatedAt = time.Now().UTC()
		m.registry.SetRuntimeState(state)
		states = append(states, state)
	}
	m.RefreshRoutingWeights()
	return map[string]any{"status": "ok", "provider": provider, "health": health, "agents": states}
}

func (m *RuntimeManager) SyncProviderHealth(providerHealth map[string]domain.ProviderHealth) []domain.AgentRuntimeState {
	if m == nil {
		return nil
	}
	now := time.Now().UTC()
	healthByProvider := make(map[string]domain.ProviderHealth, len(providerHealth))
	for provider, health := range providerHealth {
		key := strings.ToLower(strings.TrimSpace(provider))
		if key == "" {
			key = strings.ToLower(strings.TrimSpace(health.Provider))
		}
		if key == "" {
			continue
		}
		healthByProvider[key] = health
	}
	for _, agent := range m.registry.Agents() {
		info := agent.Info()
		state, _ := m.State(info.ID)
		state.Provider = info.Provider
		if state.SuppressedUntil != nil && state.SuppressedUntil.After(now) {
			state.Status = domain.AgentStatusMaintenance
			if state.DisabledReason == "" {
				state.DisabledReason = "suppressed by runtime policy"
			}
			state.UpdatedAt = now
			m.registry.SetRuntimeState(state)
			continue
		}
		if health, ok := healthByProvider[strings.ToLower(strings.TrimSpace(info.Provider))]; ok {
			state.LastProbeStatus = health.Status
			state.LastProbeError = health.Error
			if !health.Configured {
				state.Status = domain.AgentStatusOffline
				state.DisabledReason = "provider not configured"
			} else if !health.Available {
				state.Status = domain.AgentStatusDegraded
				state.DisabledReason = firstNonEmptyString(health.Error, "provider probe failed")
			} else {
				catalogSnapshot, hasCatalogSnapshot := domain.ProviderCatalogSnapshot{}, false
				if m.providerRegistry != nil {
					catalogSnapshot, hasCatalogSnapshot = m.providerRegistry.Snapshot(info.Provider)
				}
				switch confirmed, reason := m.confirmedAgentModel(agent, catalogSnapshot, hasCatalogSnapshot); {
				case !confirmed:
					state.Status = domain.AgentStatusDegraded
					state.DisabledReason = reason
				case state.ErrorRate >= 0.25:
					state.Status = domain.AgentStatusDegraded
					if state.DisabledReason == "" {
						state.DisabledReason = "runtime error budget exceeded"
					}
				case state.Status == domain.AgentStatusBusy:
					state.DisabledReason = ""
				default:
					state.Status = domain.AgentStatusReady
					state.DisabledReason = ""
				}
			}
		}
		state.UpdatedAt = now
		m.registry.SetRuntimeState(state)
	}
	m.RefreshRoutingWeights()
	return m.registry.RuntimeStates()
}

func (m *RuntimeManager) confirmedAgentModel(agent agents.Agent, snapshot domain.ProviderCatalogSnapshot, hasSnapshot bool) (bool, string) {
	info := agent.Info()
	modelName := strings.TrimSpace(info.ModelName)
	if m.providerRegistry == nil {
		return true, ""
	}
	if !hasSnapshot {
		return false, "provider model registry snapshot is unavailable"
	}
	if !providerSnapshotRoutingUsable(snapshot, time.Now().UTC()) {
		return false, "provider model registry snapshot is stale"
	}
	if modelName == "" {
		if supportsAssignedModelOverride(agent) {
			if _, ok := firstReadyProviderModel(snapshot); ok {
				return true, ""
			}
			return false, "provider has no confirmed executable models for dynamic assignment"
		}
		return false, "agent has no configured model"
	}
	if ready, reason, found := providerSnapshotModelStatus(snapshot, modelName); found {
		if ready {
			return true, ""
		}
		if supportsAssignedModelOverride(agent) {
			if _, ok := firstReadyProviderModel(snapshot); ok {
				return true, ""
			}
		}
		return false, "model " + modelName + " is not confirmed: " + reason
	}
	if supportsAssignedModelOverride(agent) {
		if _, ok := firstReadyProviderModel(snapshot); ok {
			return true, ""
		}
		return false, "provider has no confirmed executable models for dynamic assignment"
	}
	if snapshot.Status == "unavailable" && strings.TrimSpace(snapshot.Error) != "" {
		return false, "provider catalog is unavailable: " + snapshot.Error
	}
	return false, "model " + modelName + " is not registered in the provider catalog"
}

func (m *RuntimeManager) ProviderModelReadyStatus(provider string, modelName string) (bool, bool) {
	if m == nil || m.providerRegistry == nil {
		return false, false
	}
	snapshot, ok := m.providerRegistry.Snapshot(provider)
	if !ok {
		return false, false
	}
	if !providerSnapshotRoutingUsable(snapshot, time.Now().UTC()) {
		return false, false
	}
	ready, _, found := providerSnapshotModelStatus(snapshot, modelName)
	return ready, found
}

func (m *RuntimeManager) SupportsAssignedModel(agent agents.Agent, modelName string) bool {
	assignedModel := strings.TrimSpace(modelName)
	if agent == nil || assignedModel == "" {
		return false
	}
	info := agent.Info()
	if strings.EqualFold(strings.TrimSpace(info.ModelName), assignedModel) {
		if m == nil || m.providerRegistry == nil {
			return true
		}
		snapshot, ok := m.providerRegistry.Snapshot(info.Provider)
		if !ok || !providerSnapshotRoutingUsable(snapshot, time.Now().UTC()) {
			return false
		}
		ready, known := m.ProviderModelReadyStatus(info.Provider, assignedModel)
		return known && ready
	}
	if m == nil || m.providerRegistry == nil || !supportsAssignedModelOverride(agent) {
		return false
	}
	ready, known := m.ProviderModelReadyStatus(info.Provider, assignedModel)
	return known && ready
}

func supportsAssignedModelOverride(agent agents.Agent) bool {
	overrider, ok := agent.(agents.AssignedModelOverrider)
	return ok && overrider.SupportsAssignedModelOverride()
}

func providerModelRoutingReady(model domain.ProviderModelStatus) bool {
	return model.Available && strings.EqualFold(strings.TrimSpace(model.Status), "ready") && strings.EqualFold(strings.TrimSpace(model.VerificationStatus), "confirmed")
}

func providerSnapshotModelStatus(snapshot domain.ProviderCatalogSnapshot, modelName string) (bool, string, bool) {
	for _, model := range snapshot.Models {
		if !strings.EqualFold(model.ModelName, modelName) {
			continue
		}
		if providerModelRoutingReady(model) {
			return true, "", true
		}
		reason := strings.TrimSpace(model.Reason)
		if model.LastError != nil && strings.TrimSpace(model.LastError.Message) != "" {
			reason = model.LastError.Message
		}
		if reason == "" {
			switch strings.TrimSpace(model.VerificationStatus) {
			case "verifying":
				reason = "model verification is still in progress"
			case "unconfirmed":
				reason = "model is not confirmed"
			default:
				reason = "model is not ready for runtime routing"
			}
		}
		return false, reason, true
	}
	return false, "", false
}

func firstReadyProviderModel(snapshot domain.ProviderCatalogSnapshot) (domain.ProviderModelStatus, bool) {
	for _, model := range snapshot.Models {
		if providerModelRoutingReady(model) {
			return model, true
		}
	}
	return domain.ProviderModelStatus{}, false
}

func (m *RuntimeManager) ProbeAgentRuntime(ctx context.Context, agentID string) map[string]any {
	agent, ok := m.registry.AgentByID(agentID)
	if !ok {
		return map[string]any{"status": "error", "error": "agent not found", "agent_id": agentID}
	}
	info := agent.Info()
	payload := m.ProbeProviderRuntime(ctx, info.Provider)
	state, _ := m.State(agentID)
	payload["agent_id"] = agentID
	payload["agent"] = info
	payload["runtime_state"] = state
	return payload
}

func (m *RuntimeManager) defaultState(info domain.AgentInfo) domain.AgentRuntimeState {
	return domain.AgentRuntimeState{
		AgentID:       info.ID,
		Provider:      info.Provider,
		Status:        info.Status,
		PriorityScore: 1,
		UpdatedAt:     time.Now().UTC(),
	}
}

func (m *RuntimeManager) normalizeState(state domain.AgentRuntimeState) domain.AgentRuntimeState {
	if state.UpdatedAt.IsZero() {
		state.UpdatedAt = time.Now().UTC()
	}
	if state.SuppressedUntil != nil && !state.SuppressedUntil.After(time.Now().UTC()) {
		state.SuppressedUntil = nil
		if state.Status == domain.AgentStatusMaintenance {
			state.Status = domain.AgentStatusReady
		}
		if state.ErrorRate < 0.25 {
			state.DisabledReason = ""
		}
	}
	return state
}

func runtimeAgentMatchesWorkerClass(info domain.AgentInfo, workerClass string) bool {
	agentType := strings.ToLower(strings.TrimSpace(info.Type))
	if workerClass == "" {
		return false
	}
	if strings.Contains(agentType, workerClass) {
		return true
	}
	switch workerClass {
	case "code":
		return strings.Contains(agentType, "coder") || strings.Contains(agentType, "coding") || strings.Contains(agentType, "implement")
	case "review":
		return strings.Contains(agentType, "review")
	case "test":
		return strings.Contains(agentType, "test") || strings.Contains(agentType, "qa")
	case "retrieval":
		return strings.Contains(agentType, "research") || strings.Contains(agentType, "analysis")
	case "merge":
		return strings.Contains(agentType, "orchestrator") || strings.Contains(agentType, "review") || strings.Contains(agentType, "analysis")
	case "verification":
		return strings.Contains(agentType, "verify") || strings.Contains(agentType, "review") || strings.Contains(agentType, "test")
	case "planner":
		return strings.Contains(agentType, "plan") || strings.Contains(agentType, "orchestrator")
	case "fallback":
		return true
	default:
		return false
	}
}

func liveLoadPressure(state domain.AgentRuntimeState) float64 {
	return clampRuntimeLoad(maxRuntimeFloat(state.AgentSlotUsage, maxRuntimeFloat(state.ModelSlotUsage, state.GlobalSlotUsage)))
}

func clampRuntimeLoad(value float64) float64 {
	if value < 0 {
		return 0
	}
	if value > 1 {
		return 1
	}
	return value
}

func maxRuntimeFloat(values ...float64) float64 {
	maxValue := 0.0
	for _, value := range values {
		if value > maxValue {
			maxValue = value
		}
	}
	return maxValue
}

func maxRuntimeInt(value int, floor int) int {
	if value < floor {
		return floor
	}
	return value
}

func cloneFloatMap(input map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
