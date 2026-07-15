package kernel

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

type AdaptiveRuntime struct {
	registry *Registry
	runtime  *RuntimeManager
	memory   *memory.Manager
}

func NewAdaptiveRuntime(registry *Registry, runtime *RuntimeManager, memoryManager *memory.Manager) *AdaptiveRuntime {
	return &AdaptiveRuntime{
		registry: registry,
		runtime:  runtime,
		memory:   memoryManager,
	}
}

func (a *AdaptiveRuntime) Decide(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) domain.AdaptiveDecision {
	_ = ctx
	diagnostics := a.collectDiagnostics()
	mode := domain.AdaptiveExecutionModeBalanced
	reason := "default balanced runtime policy"
	maxParallelism := adaptiveParallelismLimit(task, plan)
	preferredRouteMode := ""
	recoveryActions := make([]string, 0)
	suppressedAgents := make([]string, 0)

	switch {
	case diagnostics.DegradedAgents > 0 || diagnostics.SuppressedAgents > 0 || diagnostics.AverageErrorRate >= 0.30:
		mode = domain.AdaptiveExecutionModeRecovery
		reason = "runtime detected degraded or suppressed execution lanes"
		preferredRouteMode = "orchestrator"
		if maxParallelism > 2 {
			maxParallelism = 2
		}
		for _, signal := range diagnostics.TopSignals {
			if signal.Action != "suppress_lane" {
				continue
			}
			suppressedAgents = append(suppressedAgents, signal.AgentID)
			recoveryActions = append(recoveryActions, fmt.Sprintf("suppress %s", signal.AgentID))
		}
	case task.Priority == domain.PriorityCritical || task.Priority == domain.PriorityHigh:
		mode = domain.AdaptiveExecutionModeLatencyGuarded
		reason = "priority-sensitive task prefers safer low-latency routing"
		if maxParallelism > 3 {
			maxParallelism = 3
		}
	case planWidth(plan) >= 3 && diagnostics.HealthyAgents >= 2:
		mode = domain.AdaptiveExecutionModeThroughput
		reason = "healthy pool allows wider parallel task fan-out"
		if maxParallelism < 3 {
			maxParallelism = 3
		}
	}

	if maxParallelism < 1 {
		maxParallelism = 1
	}

	return domain.AdaptiveDecision{
		Mode:               mode,
		Reason:             reason,
		MaxParallelism:     maxParallelism,
		PreferredRouteMode: preferredRouteMode,
		SuppressedAgents:   suppressedAgents,
		RecoveryActions:    recoveryActions,
		Diagnostics:        diagnostics,
		DecidedAt:          time.Now().UTC(),
	}
}

func (a *AdaptiveRuntime) Apply(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) (domain.Task, domain.AdaptiveDecision) {
	decision := a.Decide(ctx, task, plan)
	hints := cloneMap(task.RoutingHints)
	if hints == nil {
		hints = map[string]any{}
	}
	hints["adaptive_mode"] = string(decision.Mode)
	hints["adaptive_reason"] = decision.Reason
	hints["adaptive_max_parallelism"] = decision.MaxParallelism
	hints["adaptive_recovery_actions"] = append([]string(nil), decision.RecoveryActions...)
	hints["adaptive_suppressed_agents"] = append([]string(nil), decision.SuppressedAgents...)
	hints["adaptive_diagnostics"] = map[string]any{
		"healthy_agents":     decision.Diagnostics.HealthyAgents,
		"degraded_agents":    decision.Diagnostics.DegradedAgents,
		"suppressed_agents":  decision.Diagnostics.SuppressedAgents,
		"offline_agents":     decision.Diagnostics.OfflineAgents,
		"average_error_rate": decision.Diagnostics.AverageErrorRate,
	}
	if strings.TrimSpace(decision.PreferredRouteMode) != "" {
		hints["route_mode"] = decision.PreferredRouteMode
	}
	task.RoutingHints = hints
	if a.memory != nil {
		_ = a.memory.RecordAdaptiveDecision(ctx, task, decision)
	}
	return task, decision
}

func (a *AdaptiveRuntime) collectDiagnostics() domain.AdaptiveDiagnostics {
	diagnostics := domain.AdaptiveDiagnostics{
		TopSignals: make([]domain.AdaptiveAgentSignal, 0),
		ObservedAt: time.Now().UTC(),
	}
	if a == nil || a.registry == nil || a.runtime == nil {
		return diagnostics
	}
	weights := a.runtime.RefreshRoutingWeights()
	states := make([]domain.AdaptiveAgentSignal, 0)
	var totalErrorRate float64
	for _, info := range a.registry.AgentInfos() {
		state, ok := a.runtime.State(info.ID)
		if !ok {
			continue
		}
		totalErrorRate += state.ErrorRate
		signal := domain.AdaptiveAgentSignal{
			AgentID:       info.ID,
			Status:        state.Status,
			ErrorRate:     state.ErrorRate,
			RoutingWeight: weights[info.ID],
		}
		suppressedAlready := false
		switch state.Status {
		case domain.AgentStatusReady, domain.AgentStatusBusy:
			diagnostics.HealthyAgents++
		case domain.AgentStatusDegraded:
			diagnostics.DegradedAgents++
			signal.Issue = "degraded"
		case domain.AgentStatusMaintenance:
			diagnostics.SuppressedAgents++
			signal.Issue = "suppressed"
			suppressedAlready = true
		case domain.AgentStatusOffline:
			diagnostics.OfflineAgents++
			signal.Issue = "offline"
		}
		if state.ErrorRate >= 0.75 && state.Status != domain.AgentStatusOffline {
			if _, ok := a.runtime.SuppressLane(info.ID, "adaptive recovery due to sustained degradation", 300); ok {
				signal.Action = "suppress_lane"
				signal.Issue = firstNonEmptyString(signal.Issue, "high_error_rate")
				if !suppressedAlready {
					diagnostics.SuppressedAgents++
				}
			}
		} else if state.ErrorRate >= 0.25 && signal.Issue == "" {
			signal.Issue = "elevated_error_rate"
		}
		states = append(states, signal)
	}
	if len(states) > 0 {
		diagnostics.AverageErrorRate = totalErrorRate / float64(len(states))
	}
	sort.SliceStable(states, func(i, j int) bool {
		if states[i].ErrorRate == states[j].ErrorRate {
			return states[i].RoutingWeight < states[j].RoutingWeight
		}
		return states[i].ErrorRate > states[j].ErrorRate
	})
	if len(states) > 4 {
		states = states[:4]
	}
	diagnostics.TopSignals = states
	return diagnostics
}

func adaptiveParallelismLimit(task domain.Task, plan domain.ExecutionPlan) int {
	limit := 2
	switch task.Complexity {
	case domain.ComplexityLow:
		limit = 2
	case domain.ComplexityMedium:
		limit = 3
	case domain.ComplexityHigh, domain.ComplexityCritical:
		limit = 4
	}
	if width := planWidth(plan); width > 0 && width < limit {
		limit = width
	}
	return limit
}

func planWidth(plan domain.ExecutionPlan) int {
	dependents := map[string]int{}
	maxWidth := 0
	for _, step := range plan.Steps {
		level := len(step.Dependencies)
		key := fmt.Sprintf("%d", level)
		dependents[key]++
		if dependents[key] > maxWidth {
			maxWidth = dependents[key]
		}
	}
	if maxWidth == 0 && len(plan.Steps) > 0 {
		return 1
	}
	return maxWidth
}
