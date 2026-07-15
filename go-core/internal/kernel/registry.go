package kernel

import (
	"sort"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/modules"
)

type Registry struct {
	mu                 sync.RWMutex
	agents             map[string]agents.Agent
	modules            map[string]modules.Module
	runtimeStates      map[string]domain.AgentRuntimeState
	agentRegistrations []func(agents.Agent)
}

func NewRegistry() *Registry {
	return &Registry{
		agents:        map[string]agents.Agent{},
		modules:       map[string]modules.Module{},
		runtimeStates: map[string]domain.AgentRuntimeState{},
	}
}

func (r *Registry) RegisterAgent(agent agents.Agent) {
	var listeners []func(agents.Agent)
	r.mu.Lock()
	r.agents[agent.Info().ID] = agent
	info := agent.Info()
	state, ok := r.runtimeStates[info.ID]
	if !ok {
		state = domain.AgentRuntimeState{
			AgentID:       info.ID,
			Provider:      info.Provider,
			Status:        info.Status,
			PriorityScore: 1,
			UpdatedAt:     time.Now().UTC(),
		}
	} else {
		state.Provider = info.Provider
		if state.Status == "" {
			state.Status = info.Status
		}
		state.UpdatedAt = time.Now().UTC()
	}
	r.runtimeStates[info.ID] = state
	listeners = append(listeners, r.agentRegistrations...)
	r.mu.Unlock()
	for _, listener := range listeners {
		if listener != nil {
			listener(agent)
		}
	}
}

func (r *Registry) OnAgentRegistered(listener func(agents.Agent)) {
	if listener == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.agentRegistrations = append(r.agentRegistrations, listener)
}

func (r *Registry) RegisterModule(module modules.Module) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.modules[module.Info().Name] = module
}

func (r *Registry) Agents() []agents.Agent {
	r.mu.RLock()
	defer r.mu.RUnlock()
	items := make([]agents.Agent, 0, len(r.agents))
	for _, agent := range r.agents {
		items = append(items, agent)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].Info().ID < items[j].Info().ID
	})
	return items
}

func (r *Registry) AgentByID(agentID string) (agents.Agent, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	agent, ok := r.agents[agentID]
	return agent, ok
}

func (r *Registry) AgentInfos() []domain.AgentInfo {
	r.mu.RLock()
	defer r.mu.RUnlock()
	items := make([]domain.AgentInfo, 0, len(r.agents))
	for _, agent := range r.agents {
		info := agent.Info()
		if state, ok := r.runtimeStates[info.ID]; ok && state.Status != "" {
			info.Status = state.Status
		}
		items = append(items, info)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].ID < items[j].ID
	})
	return items
}

func (r *Registry) SetRuntimeState(state domain.AgentRuntimeState) {
	if state.AgentID == "" {
		return
	}
	if state.UpdatedAt.IsZero() {
		state.UpdatedAt = time.Now().UTC()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.runtimeStates[state.AgentID]; ok {
		if state.Provider == "" {
			state.Provider = existing.Provider
		}
		if state.Status == "" {
			state.Status = existing.Status
		}
	}
	r.runtimeStates[state.AgentID] = state
}

func (r *Registry) RuntimeState(agentID string) (domain.AgentRuntimeState, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	state, ok := r.runtimeStates[agentID]
	return state, ok
}

func (r *Registry) RuntimeStates() []domain.AgentRuntimeState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	items := make([]domain.AgentRuntimeState, 0, len(r.runtimeStates))
	for _, state := range r.runtimeStates {
		items = append(items, state)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].AgentID < items[j].AgentID
	})
	return items
}

func (r *Registry) Modules() []modules.Module {
	r.mu.RLock()
	defer r.mu.RUnlock()
	items := make([]modules.Module, 0, len(r.modules))
	for _, module := range r.modules {
		items = append(items, module)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].Info().Name < items[j].Info().Name
	})
	return items
}

func (r *Registry) ModuleInfos() []domain.ModuleInfo {
	items := r.Modules()
	out := make([]domain.ModuleInfo, 0, len(items))
	for _, module := range items {
		out = append(out, module.Info())
	}
	return out
}
