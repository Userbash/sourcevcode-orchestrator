package kernel

import (
	"context"
	"strconv"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

type Router struct {
	registry     *Registry
	selector     *ModelSelector
	runtime      *RuntimeManager
	memory       *memory.Manager
	routeMemory  memory.RouteMemoryAgent
	retriever    memory.RetrieverAgent
	liveRealtime *LiveRealtimeMetricsCollector
}

func NewRouter(registry *Registry, selector *ModelSelector) *Router {
	if selector == nil {
		selector = NewModelSelector(nil)
	}
	return &Router{registry: registry, selector: selector}
}

func (r *Router) AttachMemoryManager(manager *memory.Manager) {
	if r == nil {
		return
	}
	r.memory = manager
	r.routeMemory = manager
	r.retriever = manager
}

func (r *Router) AttachLiveRealtimeMetrics(metrics *LiveRealtimeMetricsCollector) {
	if r == nil {
		return
	}
	r.liveRealtime = metrics
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
				if r.canRouteAgent(task, preferred, capability) {
					return accepted(task, plan, preferred, capability, "preferred agent routing", r.runtime), preferred, true
				}
			}
		}
	}
	if routeModeOrchestrator(task.RoutingHints) {
		if _, blocked := exclude["orchestrator"]; !blocked {
			if agent, ok := r.agentByID("orchestrator"); ok && r.canRouteAgent(task, agent, capability) {
				return accepted(task, plan, agent, capability, "orchestrator route override", r.runtime), agent, true
			}
		}
	}
	if isSourcecraftWork(task) {
		if _, blocked := exclude["orchestrator"]; !blocked {
			if agent, ok := r.agentByID("orchestrator"); ok && r.canRouteAgent(task, agent, capability) {
				return accepted(task, plan, agent, capability, "sourcecraft route", r.runtime), agent, true
			}
		}
	}
	relaxAssignedProvider := len(exclude) > 0
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
	candidates = filterCandidatesByAssignedProvider(task, candidates, relaxAssignedProvider)
	candidates = filterCandidatesByAssignedModel(task, candidates, r.runtime, relaxAssignedProvider || shouldRelaxAssignedProvider(task))
	if len(candidates) == 0 {
		if strings.TrimSpace(task.AssignedModel) != "" {
			return rejected(task, complexity, capability, "no available agent matched assigned model "+task.AssignedModel), nil, false
		}
		if strings.TrimSpace(task.AssignedProvider) != "" {
			return rejected(task, complexity, capability, "no available agent matched assigned provider "+task.AssignedProvider), nil, false
		}
		return rejected(task, complexity, capability, "no available agent for capability "+capability), nil, false
	}
	risk := EvaluateRiskContext(taskText(task))
	bestScore := -1e9
	var best agents.Agent
	for _, candidate := range candidates {
		score := r.scoreAgent(context.Background(), candidate, task, capability, complexity, risk)
		if score > bestScore {
			bestScore = score
			best = candidate
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no healthy agent selected for capability "+capability), nil, false
	}
	return accepted(task, plan, best, capability, "policy routing", r.runtime), best, true
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
		score := r.scoreAgent(context.Background(), candidate, task, capability, complexity, risk) - float64(rank*15)
		if score > bestScore {
			bestScore = score
			best = candidate
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no available agent matched fallback providers"), nil, false
	}
	return accepted(task, plan, best, capability, "budget fallback routing", r.runtime), best, true
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

func filterCandidatesByAssignedProvider(task domain.Task, candidates []agents.Agent, relaxAssignedProvider bool) []agents.Agent {
	assignedProvider := strings.TrimSpace(task.AssignedProvider)
	if assignedProvider == "" || relaxAssignedProvider || shouldRelaxAssignedProvider(task) {
		return candidates
	}
	filtered := make([]agents.Agent, 0, len(candidates))
	for _, candidate := range candidates {
		if strings.EqualFold(strings.TrimSpace(candidate.Info().Provider), assignedProvider) {
			filtered = append(filtered, candidate)
		}
	}
	return filtered
}

func filterCandidatesByAssignedModel(task domain.Task, candidates []agents.Agent, runtime *RuntimeManager, relaxAssignedModel bool) []agents.Agent {
	assignedModel := strings.TrimSpace(task.AssignedModel)
	if assignedModel == "" || relaxAssignedModel {
		return candidates
	}
	filtered := make([]agents.Agent, 0, len(candidates))
	for _, candidate := range candidates {
		if candidateMatchesAssignedModel(candidate, assignedModel, runtime) {
			filtered = append(filtered, candidate)
		}
	}
	return filtered
}

func candidateMatchesAssignedModel(candidate agents.Agent, assignedModel string, runtime *RuntimeManager) bool {
	assignedModel = strings.TrimSpace(assignedModel)
	if candidate == nil || assignedModel == "" {
		return false
	}
	info := candidate.Info()
	if strings.EqualFold(strings.TrimSpace(info.ModelName), assignedModel) {
		if runtime == nil || runtime.providerRegistry == nil {
			return true
		}
		ready, known := runtime.ProviderModelReadyStatus(info.Provider, assignedModel)
		return known && ready
	}
	if runtime == nil || runtime.providerRegistry == nil {
		return false
	}
	return runtime.SupportsAssignedModel(candidate, assignedModel)
}

func shouldRelaxAssignedProvider(task domain.Task) bool {
	if task.RoutingHints == nil {
		return false
	}
	if attempt, ok := task.RoutingHints["p2p_attempt"].(int); ok && attempt > 1 {
		return true
	}
	if attempt, ok := task.RoutingHints["p2p_attempt"].(float64); ok && attempt > 1 {
		return true
	}
	if rawFailures, ok := task.RoutingHints["peer_failures"]; ok {
		switch failures := rawFailures.(type) {
		case []map[string]any:
			return len(failures) > 0
		case []any:
			return len(failures) > 0
		}
	}
	return false
}

func (r *Router) scoreAgent(ctx context.Context, agent agents.Agent, task domain.Task, capability string, complexity domain.Complexity, risk RiskEvaluation) float64 {
	info := agent.Info()
	policyScore := r.policyScore(agent, task, capability, complexity, risk)
	historyScore := r.historyScore(ctx, info, task)
	runtimeScore := r.runtimeScore(info, task, capability)
	retrievalScore := r.retrievalScore(ctx, info, task, complexity)
	realtimeScore := r.realtimeQualityScore(info)
	finalScore := 0.39*policyScore + 0.18*historyScore + 0.20*runtimeScore + 0.11*retrievalScore + 0.12*realtimeScore
	return finalScore * 100.0
}

func (r *Router) realtimeQualityScore(info domain.AgentInfo) float64 {
	if r == nil || r.liveRealtime == nil {
		return 0.5
	}
	summary, ok := r.liveRealtime.ModelSummary(info.Provider, info.ModelName)
	if !ok {
		return 0.5
	}
	transportScore := 0.35
	switch {
	case summary.NativeStreamSessions > 0:
		transportScore = 1.0
	case summary.PseudoRealtimeSessions > 0:
		transportScore = 0.7
	case summary.BufferedSessions > 0:
		transportScore = 0.25
	}
	ttftScore := 1.0
	if summary.AvgTimeToFirstTokenMS > 0 {
		ttftScore = 1.0 / (1.0 + float64(summary.AvgTimeToFirstTokenMS)/600.0)
	}
	completionScore := 1.0
	if summary.AvgTotalCompletionMS > 0 {
		completionScore = 1.0 / (1.0 + float64(summary.AvgTotalCompletionMS)/4000.0)
	}
	failurePenalty := 1.0 - summary.FailureRate
	if failurePenalty < 0 {
		failurePenalty = 0
	}
	return 0.35*transportScore + 0.25*ttftScore + 0.20*completionScore + 0.20*failurePenalty
}

func (r *Router) policyScore(agent agents.Agent, task domain.Task, capability string, complexity domain.Complexity, risk RiskEvaluation) float64 {
	info := agent.Info()
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
	if task.AssignedModel != "" && candidateMatchesAssignedModel(agent, task.AssignedModel, r.runtime) {
		score += 15
	}
	if preferredAgentID(task.RoutingHints) == info.ID {
		score += 120
	}
	if complexity == domain.ComplexityHigh || complexity == domain.ComplexityCritical || risk.HighRisk {
		if strings.EqualFold(info.Provider, "openai") || strings.EqualFold(info.Provider, "codexsale") {
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
		case "openai", "codexsale":
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
	score += clusterPolicyDelta(task, info, capability, complexity, risk)
	return clampPolicyScore(score)
}

func (r *Router) historyScore(ctx context.Context, info domain.AgentInfo, task domain.Task) float64 {
	if r == nil || r.routeMemory == nil {
		return 0
	}
	score, _, err := r.routeMemory.RouteHistoryScore(ctx, task, info)
	if err != nil {
		return 0
	}
	return clampRouterScore(score)
}

func (r *Router) runtimeScore(info domain.AgentInfo, task domain.Task, capability string) float64 {
	if r == nil || r.runtime == nil {
		return 0.5
	}
	weight := clampRouterScore(r.runtime.RoutingWeight(info.ID))
	if state, ok := r.runtime.State(info.ID); ok {
		priority := clampRouterScore(state.PriorityScore)
		errorPenalty := clampRouterScore(1 - state.ErrorRate)
		loadScore := liveLoadScore(state)
		workerClass := taskWorkerClass(task, capability)
		capacityScore := clampRouterScore(1 - r.runtime.CapacityPressure(info, workerClass))
		return clampRouterScore(weight*0.30 + priority*0.20 + errorPenalty*0.18 + loadScore*0.16 + capacityScore*0.16)
	}
	return weight
}

func liveLoadScore(state domain.AgentRuntimeState) float64 {
	pressure := state.AgentSlotUsage
	if state.ModelSlotUsage > pressure {
		pressure = state.ModelSlotUsage
	}
	if state.GlobalSlotUsage > pressure {
		pressure = state.GlobalSlotUsage
	}
	return clampRouterScore(1 - pressure)
}

func (r *Router) retrievalScore(ctx context.Context, info domain.AgentInfo, task domain.Task, complexity domain.Complexity) float64 {
	snapshot := memory.RetrievalSnapshot{}
	if r != nil && r.retriever != nil {
		loaded, err := r.retriever.Retrieve(ctx, task, 4)
		if err == nil {
			snapshot = loaded
		}
	}
	heavyRetrieval := complexity == domain.ComplexityHigh || complexity == domain.ComplexityCritical || task.Type == domain.TaskTypeResearch || task.Type == domain.TaskTypeReview || snapshot.KPI.Tier == "high" || snapshot.KPI.CoverageRatio >= 0.55 || snapshot.KPI.PackedCount >= 3
	if !heavyRetrieval {
		return 0.5
	}
	name := strings.ToLower(info.ModelName)
	provider := strings.ToLower(info.Provider)
	agentType := strings.ToLower(info.Type)
	score := 0.35
	if provider == "openai" || provider == "codexsale" {
		score += 0.25
	}
	if provider == "ai_kernel" || provider == "local" {
		score += 0.08
	}
	if strings.Contains(agentType, "research") || strings.Contains(agentType, "review") || strings.Contains(agentType, "analysis") {
		score += 0.18
	}
	if strings.Contains(name, "gpt") || strings.Contains(name, "claude") || strings.Contains(name, "gemini") || strings.Contains(name, "qwen") || strings.Contains(name, "mistral") {
		score += 0.18
	}
	if strings.Contains(name, "mini") {
		score -= 0.12
	}
	if snapshot.KPI.Tier == "high" {
		score += 0.1
	}
	if snapshot.KPI.CoverageRatio >= 0.6 {
		score += 0.08
	}
	if snapshot.KPI.TruncationRatio >= 0.35 {
		score += 0.06
	}
	return clampRouterScore(score)
}

func clusterPolicyDelta(task domain.Task, info domain.AgentInfo, capability string, complexity domain.Complexity, risk RiskEvaluation) float64 {
	workerClass := taskWorkerClass(task, capability)
	if workerClass == "" {
		return 0
	}
	provider := strings.ToLower(strings.TrimSpace(info.Provider))
	agentType := strings.ToLower(strings.TrimSpace(info.Type))
	contextBudget := taskContextBudget(task)
	weight := taskWeight(task)
	delta := 0.0

	if strings.Contains(agentType, workerClass) {
		delta += 18
	}
	if workerClass == "code" && (strings.Contains(agentType, "coding") || strings.Contains(agentType, "coder")) {
		delta += 16
	}
	if workerClass == "review" && strings.Contains(agentType, "review") {
		delta += 22
	}
	if workerClass == "test" && strings.Contains(agentType, "test") {
		delta += 18
	}
	if workerClass == "retrieval" && (strings.Contains(agentType, "research") || strings.Contains(agentType, "analysis")) {
		delta += 15
	}
	if workerClass == "merge" && (strings.Contains(agentType, "review") || strings.Contains(agentType, "analysis") || strings.Contains(agentType, "orchestrator")) {
		delta += 18
	}
	if workerClass == "planner" && (strings.Contains(agentType, "plan") || strings.Contains(agentType, "analysis")) {
		delta += 16
	}

	smallContext := contextBudget > 0 && contextBudget <= 900
	largeContext := contextBudget >= 1800 || complexity == domain.ComplexityHigh || complexity == domain.ComplexityCritical || risk.HighRisk
	lightWeight := weight > 0 && weight <= 1.5
	heavyWeight := weight >= 3.5

	switch workerClass {
	case "code", "test", "retrieval", "verification":
		switch provider {
		case "local", "ai_kernel":
			delta += 26
		case "mistral", "antigravity", "mimo":
			delta += 12
		case "openai", "codexsale":
			delta -= 10
		}
	case "review", "merge", "planner", "research":
		if largeContext || heavyWeight {
			switch provider {
			case "openai", "codexsale":
				delta += 24
			case "mistral", "antigravity", "mimo":
				delta += 10
			case "local", "ai_kernel":
				delta += 4
			}
		}
	}

	if smallContext {
		switch provider {
		case "local", "ai_kernel":
			delta += 16
		case "openai", "codexsale":
			delta -= 14
		}
	}
	if lightWeight && (provider == "local" || provider == "ai_kernel") {
		delta += 12
	}
	if heavyWeight && largeContext {
		switch provider {
		case "openai", "codexsale":
			delta += 10
		case "local", "ai_kernel":
			delta -= 8
		}
	}

	return delta
}

func taskWorkerClass(task domain.Task, capability string) string {
	if value := strings.ToLower(strings.TrimSpace(kernelString(task.RoutingHints["worker_class"]))); value != "" {
		return value
	}
	if value := strings.ToLower(strings.TrimSpace(kernelString(task.ExecutionContract["worker_class"]))); value != "" {
		return value
	}
	return strings.ToLower(strings.TrimSpace(capability))
}

func taskContextBudget(task domain.Task) int {
	if value := taskHintInt(task.RoutingHints["context_budget"]); value > 0 {
		return value
	}
	return taskHintInt(task.ExecutionContract["context_budget"])
}

func taskWeight(task domain.Task) float64 {
	if value := taskHintFloat(task.RoutingHints["task_weight"]); value > 0 {
		return value
	}
	return taskHintFloat(task.ExecutionContract["task_weight"])
}

func taskHintInt(value any) int {
	switch v := value.(type) {
	case int:
		return v
	case int32:
		return int(v)
	case int64:
		return int(v)
	case float32:
		return int(v)
	case float64:
		return int(v)
	case string:
		parsed, err := strconv.Atoi(strings.TrimSpace(v))
		if err == nil {
			return parsed
		}
	}
	return 0
}

func taskHintFloat(value any) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int32:
		return float64(v)
	case int64:
		return float64(v)
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err == nil {
			return parsed
		}
	}
	return 0
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

func accepted(task domain.Task, plan domain.ExecutionPlan, agent agents.Agent, capability string, reason string, runtime *RuntimeManager) domain.TaskAcceptance {
	info := agent.Info()
	modelName := info.ModelName
	if assignedModel := strings.TrimSpace(task.AssignedModel); assignedModel != "" && candidateMatchesAssignedModel(agent, assignedModel, runtime) {
		modelName = assignedModel
	}
	return domain.TaskAcceptance{
		TaskID:                  task.ID,
		Status:                  domain.TaskStatusAccepted,
		AgentID:                 info.ID,
		Complexity:              plan.Complexity,
		Reason:                  reason,
		Capability:              capability,
		Provider:                info.Provider,
		ModelName:               modelName,
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
