package kernel

import (
	"context"
	"math"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type RuntimeManager struct {
	registry       *Registry
	probeProviders func(context.Context, bool) map[string]domain.ProviderHealth
	mu             sync.RWMutex
	routingWeights map[string]float64
}

func NewRuntimeManager(registry *Registry, probeProviders func(context.Context, bool) map[string]domain.ProviderHealth) *RuntimeManager {
	return &RuntimeManager{
		registry:       registry,
		probeProviders: probeProviders,
		routingWeights: map[string]float64{},
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
			weight = 0.0
		}
		if state.ErrorRate > 0 {
			weight *= math.Max(0.05, 1-state.ErrorRate)
		}
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
	states := make([]domain.AgentRuntimeState, 0)
	for _, info := range m.registry.AgentInfos() {
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

func cloneFloatMap(input map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
