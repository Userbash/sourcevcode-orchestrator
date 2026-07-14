package kernel

import (
	"context"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

type Router struct {
	registry *Registry
	selector *ModelSelector
	runtime  *RuntimeManager
	memory   *memory.Manager
}

func NewRouter(registry *Registry, selector *ModelSelector) *Router {
	if selector == nil {
		selector = NewModelSelector(nil)
	}
	return &Router{registry: registry, selector: selector}
}

func (r *Router) Route(task domain.Task, plan domain.ExecutionPlan) (domain.TaskAcceptance, agents.Agent, bool) {
	return r.route(task, plan, nil)
}

func (r *Router) RouteExcluding(task domain.Task, plan domain.ExecutionPlan, exclude map[string]struct{}) (domain.TaskAcceptance, agents.Agent, bool) {
	return r.route(task, plan, exclude)
}

func (r *Router) route(task domain.Task, plan domain.ExecutionPlan, exclude map[string]struct{}) (domain.TaskAcceptance, agents.Agent, bool) {
	capability := plan.PrimaryCapability
	if capability == "" {
		capability = resolvedCapability(task)
	}
	complexity := plan.Complexity
	if exclude == nil {
		exclude = map[string]struct{}{}
	}
	preferredAgentID := preferredAgentID(task.RoutingHints)
	if preferredAgentID != "" {
		if _, blocked := exclude[preferredAgentID]; !blocked {
			if preferred, ok := r.agentByID(preferredAgentID); ok {
				info := preferred.Info()
				if r.canRouteAgent(task, preferred, capability) {
					return accepted(task, plan, info, capability, "preferred agent routing"), preferred, true
				}
			}
		}
	}
	if routeModeOrchestrator(task.RoutingHints) {
		if _, blocked := exclude["orchestrator"]; !blocked {
			if agent, ok := r.agentByID("orchestrator"); ok && r.canRouteAgent(task, agent, capability) {
				return accepted(task, plan, agent.Info(), capability, "orchestrator route override"), agent, true
			}
		}
	}
	if isSourcecraftWork(task) {
		if _, blocked := exclude["orchestrator"]; !blocked {
			if agent, ok := r.agentByID("orchestrator"); ok && r.canRouteAgent(task, agent, capability) {
				return accepted(task, plan, agent.Info(), capability, "sourcecraft route"), agent, true
			}
		}
	}
	candidates := make([]agents.Agent, 0)
	for _, agent := range r.registry.Agents() {
		if _, blocked := exclude[agent.Info().ID]; blocked {
			continue
		}
		if !r.canRouteAgent(task, agent, capability) {
			continue
		}
		candidates = append(candidates, agent)
	}
	if len(candidates) == 0 {
		return rejected(task, complexity, capability, "no available agent for capability "+capability), nil, false
	}
	risk := EvaluateRiskContext(taskText(task))
	bestScore := -1e9
	var best agents.Agent
	for _, candidate := range candidates {
		score := r.scoreAgent(context.Background(), candidate.Info(), task, capability, complexity, risk)
		if score > bestScore {
			bestScore = score
			best = candidate
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no healthy agent selected for capability "+capability), nil, false
	}
	return accepted(task, plan, best.Info(), capability, "policy routing"), best, true
}

func (r *Router) RouteWithinProviders(task domain.Task, plan domain.ExecutionPlan, providers []string, exclude map[string]struct{}) (domain.TaskAcceptance, agents.Agent, bool) {
	capability := plan.PrimaryCapability
	if capability == "" {
		capability = resolvedCapability(task)
	}
	complexity := plan.Complexity
	providerRanks := map[string]int{}
	orderedProviders := make([]string, 0, len(providers))
	for _, provider := range providers {
		trimmed := strings.ToLower(strings.TrimSpace(provider))
		if trimmed == "" {
			continue
		}
		if _, ok := providerRanks[trimmed]; ok {
			continue
		}
		providerRanks[trimmed] = len(orderedProviders)
		orderedProviders = append(orderedProviders, trimmed)
	}
	if len(orderedProviders) == 0 {
		return domain.TaskAcceptance{}, nil, false
	}
	if exclude == nil {
		exclude = map[string]struct{}{}
	}
	risk := EvaluateRiskContext(taskText(task))
	bestScore := -1e9
	var best agents.Agent
	for _, candidate := range r.registry.Agents() {
		info := candidate.Info()
		if _, blocked := exclude[info.ID]; blocked {
			continue
		}
		rank, allowed := providerRanks[strings.ToLower(strings.TrimSpace(info.Provider))]
		if !allowed {
			continue
		}
		if !r.canRouteAgent(task, candidate, capability) {
			continue
		}
		score := r.scoreAgent(context.Background(), info, task, capability, complexity, risk) - float64(rank*15)
		if score > bestScore {
			bestScore = score
			best = candidate
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no available agent matched fallback providers"), nil, false
	}
	return accepted(task, plan, best.Info(), capability, "budget fallback routing"), best, true
}

func (r *Router) canRouteAgent(task domain.Task, agent agents.Agent, capability string) bool {
	info := agent.Info()
	if !isAgentRoutable(info.Status) {
		return false
	}
	if r.runtime != nil {
		if !r.runtime.Allows(info) || r.runtime.RoutingWeight(info.ID) <= 0 {
			return false
		}
	}
	if !supportsCapability(info.Capabilities, capability) {
		return false
	}
	return agent.CanAccept(task)
}

func (r *Router) scoreAgent(ctx context.Context, info domain.AgentInfo, task domain.Task, capability string, complexity domain.Complexity, risk RiskEvaluation) float64 {
	policyScore := r.policyScore(info, task, capability, complexity, risk)
	historyScore := r.historyScore(ctx, info, task)
	runtimeScore := r.runtimeScore(info)
	finalScore := 0.50*policyScore + 0.30*historyScore + 0.20*runtimeScore
	return finalScore * 100.0
}

func (r *Router) policyScore(info domain.AgentInfo, task domain.Task, capability string, complexity domain.Complexity, risk RiskEvaluation) float64 {
	score := 100.0
	if info.Status == domain.AgentStatusReady {
		score += 20
	}
	if info.Status == domain.AgentStatusBusy {
		score += 5
	}
	if supportsCapability(info.Capabilities, capability) {
		score += 40
	}
	if task.AssignedProvider != "" && strings.EqualFold(info.Provider, task.AssignedProvider) {
		score += 35
	}
	if task.AssignedModel != "" && strings.EqualFold(info.ModelName, task.AssignedModel) {
		score += 15
	}
	if preferredAgentID(task.RoutingHints) == info.ID {
		score += 120
	}
	if complexity == domain.ComplexityHigh || complexity == domain.ComplexityCritical || risk.HighRisk {
		if strings.EqualFold(info.Provider, "openai") {
			score += 40
		}
		if strings.Contains(strings.ToLower(info.Type), "review") {
			score += 12
		}
	} else {
		switch strings.ToLower(info.Provider) {
		case "local", "ai_kernel":
			score += 25
		case "mistral", "antigravity", "mimo":
			score += 15
		case "openai":
			score -= 20
		}
	}
	if isSourcecraftWork(task) && info.ID == "orchestrator" {
		score += 100
	}
	if strings.Contains(strings.ToLower(info.Type), string(task.Type)) {
		score += 10
	}
	if complexity == domain.ComplexityLow && strings.EqualFold(info.Provider, "local") {
		score += 10
	}
	return clampPolicyScore(score)
}

func (r *Router) historyScore(ctx context.Context, info domain.AgentInfo, task domain.Task) float64 {
	if r == nil || r.memory == nil {
		return 0
	}
	score, _, err := r.memory.RouteHistoryScore(ctx, task, info)
	if err != nil {
		return 0
	}
	return clampRouterScore(score)
}

func (r *Router) runtimeScore(info domain.AgentInfo) float64 {
	if r == nil || r.runtime == nil {
		return 0.5
	}
	weight := clampRouterScore(r.runtime.RoutingWeight(info.ID))
	if state, ok := r.runtime.State(info.ID); ok {
		priority := clampRouterScore(state.PriorityScore)
		errorPenalty := clampRouterScore(1 - state.ErrorRate)
		return clampRouterScore(weight*0.50 + priority*0.30 + errorPenalty*0.20)
	}
	return weight
}

func clampPolicyScore(score float64) float64 {
	return clampRouterScore((score - 80.0) / 160.0)
}

func clampRouterScore(score float64) float64 {
	if score < 0 {
		return 0
	}
	if score > 1 {
		return 1
	}
	return score
}

func accepted(task domain.Task, plan domain.ExecutionPlan, info domain.AgentInfo, capability string, reason string) domain.TaskAcceptance {
	return domain.TaskAcceptance{
		TaskID:                  task.ID,
		Status:                  domain.TaskStatusAccepted,
		AgentID:                 info.ID,
		Complexity:              plan.Complexity,
		Reason:                  reason,
		Capability:              capability,
		Provider:                info.Provider,
		ModelName:               info.ModelName,
		RequiresSecondaryReview: plan.Selection.RequiresSecondaryReview,
		AcceptedAt:              time.Now().UTC(),
	}
}

func rejected(task domain.Task, complexity domain.Complexity, capability string, reason string) domain.TaskAcceptance {
	return domain.TaskAcceptance{
		TaskID:     task.ID,
		Status:     domain.TaskStatusRejected,
		Complexity: complexity,
		Reason:     reason,
		Capability: capability,
		AcceptedAt: time.Now().UTC(),
	}
}

func preferredAgentID(hints map[string]any) string {
	for _, key := range []string{"preferred_agent_id", "batch_forced_agent_id", "forced_agent_id"} {
		value, ok := hints[key]
		if !ok {
			continue
		}
		text := strings.TrimSpace(toString(value))
		if text != "" {
			return text
		}
	}
	return ""
}

func routeModeOrchestrator(hints map[string]any) bool {
	if strings.EqualFold(strings.TrimSpace(toString(hints["route_mode"])), "orchestrator") {
		return true
	}
	flag, ok := hints["force_orchestrator"].(bool)
	return ok && flag
}

func isAgentRoutable(status domain.AgentStatus) bool {
	return status == domain.AgentStatusReady || status == domain.AgentStatusBusy || status == domain.AgentStatusDegraded
}

func (r *Router) agentByID(agentID string) (agents.Agent, bool) {
	return r.registry.AgentByID(agentID)
}

func toString(value any) string {
	if value == nil {
		return ""
	}
	text, ok := value.(string)
	if ok {
		return text
	}
	return ""
}
