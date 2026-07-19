package kernel

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

type codingRuntimeSnapshotter interface {
	Snapshot(context.Context) map[string]any
}

type managedCodingRuntimeConfig struct {
	Name             string
	Backend          string
	AllowedProviders []string
	PlannerTimeout   time.Duration
	CoderTimeout     time.Duration
	ReviewerTimeout  time.Duration
	TesterTimeout    time.Duration
	RetrievalTimeout time.Duration
}

type managedCodingRuntime struct {
	orchestrator *Orchestrator
	config       managedCodingRuntimeConfig
	startedAt    time.Time

	mu       sync.RWMutex
	sessions map[string]*managedCodingRuntimeSessionState
}

type managedCodingRuntimeSessionState struct {
	request    domain.CodingRuntimeRequest
	session    domain.CodingRuntimeSession
	events     chan domain.CodingRuntimeEvent
	done       chan struct{}
	cancel     context.CancelFunc
	result     domain.AgentResult
	err        error
	active     bool
	startedAt  time.Time
	finishedAt time.Time
	selected   domain.AgentInfo
	live       domain.LiveSessionState
}

type managedCodingRuntimeLaneResult struct {
	step       domain.PlanStep
	task       domain.Task
	acceptance domain.TaskAcceptance
	result     domain.AgentResult
	err        error
	selected   domain.AgentInfo
}

type managedRuntimeEventState struct {
	sessionID string
	taskID    string
	live      domain.LiveSessionState
}

func newManagedCodingRuntime(orchestrator *Orchestrator, config managedCodingRuntimeConfig) domain.ExternalCodingRuntime {
	name := strings.TrimSpace(config.Name)
	if name == "" {
		name = "managed-realtime"
	}
	backend := strings.TrimSpace(config.Backend)
	if backend == "" {
		backend = "managed"
	}
	return &managedCodingRuntime{
		orchestrator: orchestrator,
		config: managedCodingRuntimeConfig{
			Name:             name,
			Backend:          backend,
			AllowedProviders: normalizeRuntimeProviderList(config.AllowedProviders),
			PlannerTimeout:   defaultRuntimeDuration(config.PlannerTimeout, 45*time.Second),
			CoderTimeout:     defaultRuntimeDuration(config.CoderTimeout, 120*time.Second),
			ReviewerTimeout:  defaultRuntimeDuration(config.ReviewerTimeout, 60*time.Second),
			TesterTimeout:    defaultRuntimeDuration(config.TesterTimeout, 90*time.Second),
			RetrievalTimeout: defaultRuntimeDuration(config.RetrievalTimeout, 30*time.Second),
		},
		startedAt: time.Now().UTC(),
		sessions:  map[string]*managedCodingRuntimeSessionState{},
	}
}

func defaultRuntimeDuration(value, fallback time.Duration) time.Duration {
	if value <= 0 {
		return fallback
	}
	return value
}

func cloneLiveSessionState(live domain.LiveSessionState) domain.LiveSessionState {
	live.ActiveWorkers = append([]string(nil), live.ActiveWorkers...)
	live.ActiveTools = append([]string(nil), live.ActiveTools...)
	live.CompletedWorkers = append([]string(nil), live.CompletedWorkers...)
	live.PendingApprovals = append([]string(nil), live.PendingApprovals...)
	return live
}

func runtimeLanePriority(step domain.PlanStep) int {
	worker := strings.ToLower(strings.TrimSpace(runtimeStepWorkerClass(step)))
	switch worker {
	case "planner", "planning":
		return 0
	case "retrieval", "rag", "context":
		return 1
	case "coder", "coding", "implementer":
		return 2
	case "reviewer", "review":
		return 3
	case "tester", "test", "qa":
		return 4
	default:
		return 5
	}
}

func (r *managedCodingRuntime) laneTimeout(step domain.PlanStep) time.Duration {
	worker := strings.ToLower(strings.TrimSpace(runtimeStepWorkerClass(step)))
	switch worker {
	case "planner", "planning":
		return r.config.PlannerTimeout
	case "retrieval", "rag", "context":
		return r.config.RetrievalTimeout
	case "reviewer", "review":
		return r.config.ReviewerTimeout
	case "tester", "test", "qa":
		return r.config.TesterTimeout
	default:
		return r.config.CoderTimeout
	}
}
func (r *managedCodingRuntime) Name() string {
	if r == nil {
		return ""
	}
	return r.config.Name
}

func (r *managedCodingRuntime) Supports(task domain.Task) bool {
	if r == nil || r.orchestrator == nil {
		return false
	}
	if isSourcecraftWork(task) {
		return true
	}
	switch task.Type {
	case domain.TaskTypePlan, domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeReview, domain.TaskTypeTest:
		return true
	default:
		return len(task.Input.Files) > 0
	}
}

func (r *managedCodingRuntime) StartTask(ctx context.Context, req domain.CodingRuntimeRequest) (domain.CodingRuntimeSession, error) {
	if r == nil || r.orchestrator == nil {
		return domain.CodingRuntimeSession{}, errors.New("managed coding runtime is unavailable")
	}
	if strings.TrimSpace(req.Task.ID) == "" {
		return domain.CodingRuntimeSession{}, errors.New("task id is required")
	}
	if !r.Supports(req.Task) {
		return domain.CodingRuntimeSession{}, fmt.Errorf("task %s is not supported by coding runtime", req.Task.ID)
	}

	sessionID := fmt.Sprintf("%s:%s", r.Name(), req.Task.ID)
	taskCtx, cancel := context.WithCancel(context.Background())
	session := domain.CodingRuntimeSession{
		Runtime:    r.Name(),
		SessionID:  sessionID,
		AcceptedAt: time.Now().UTC(),
		Metadata: map[string]any{
			"backend":           r.config.Backend,
			"allowed_providers": append([]string(nil), r.config.AllowedProviders...),
			"allowed_subagents": append([]string(nil), req.AllowedSubagents...),
			"mode":              string(req.Mode),
		},
	}
	state := &managedCodingRuntimeSessionState{
		request:   req,
		session:   session,
		events:    make(chan domain.CodingRuntimeEvent, 32),
		done:      make(chan struct{}),
		cancel:    cancel,
		active:    true,
		startedAt: session.AcceptedAt,
		live: domain.LiveSessionState{
			SessionID: session.SessionID,
			TaskID:    req.Task.ID,
			Mode:      string(req.Mode),
			Active:    true,
			UpdatedAt: session.AcceptedAt,
		},
	}

	r.mu.Lock()
	r.sessions[sessionID] = state
	r.mu.Unlock()

	go r.runSession(taskCtx, sessionID)
	return session, nil
}

func (r *managedCodingRuntime) WaitTask(ctx context.Context, session domain.CodingRuntimeSession) (domain.AgentResult, error) {
	state, ok := r.sessionState(session.SessionID)
	if !ok {
		return domain.AgentResult{}, fmt.Errorf("coding runtime session %s was not found", session.SessionID)
	}
	select {
	case <-ctx.Done():
		return domain.AgentResult{}, ctx.Err()
	case <-state.done:
	}
	if state.err != nil {
		return domain.AgentResult{}, state.err
	}
	return state.result, nil
}

func (r *managedCodingRuntime) AbortTask(ctx context.Context, sessionID string) error {
	_ = ctx
	state, ok := r.sessionState(sessionID)
	if !ok {
		return fmt.Errorf("coding runtime session %s was not found", sessionID)
	}
	state.cancel()
	return nil
}

func (r *managedCodingRuntime) Events(ctx context.Context, sessionID string) (<-chan domain.CodingRuntimeEvent, error) {
	state, ok := r.sessionState(sessionID)
	if !ok {
		return nil, fmt.Errorf("coding runtime session %s was not found", sessionID)
	}
	forwarded := make(chan domain.CodingRuntimeEvent, 32)
	go func() {
		defer close(forwarded)
		for {
			select {
			case <-ctx.Done():
				return
			case event, ok := <-state.events:
				if !ok {
					return
				}
				select {
				case forwarded <- event:
				case <-ctx.Done():
					return
				}
			}
		}
	}()
	return forwarded, nil
}

func (r *managedCodingRuntime) Snapshot(ctx context.Context) map[string]any {
	_ = ctx
	if r == nil {
		return map[string]any{"enabled": false, "attached": false, "status": "disabled"}
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	active := 0
	sessions := make([]map[string]any, 0, len(r.sessions))
	for _, state := range r.sessions {
		if state.active {
			active++
		}
		sessions = append(sessions, map[string]any{
			"session_id":  state.session.SessionID,
			"task_id":     state.request.Task.ID,
			"mode":        string(state.request.Mode),
			"active":      state.active,
			"agent_id":    state.selected.ID,
			"provider":    state.selected.Provider,
			"model_name":  state.selected.ModelName,
			"started_at":  state.startedAt,
			"finished_at": state.finishedAt,
			"live": map[string]any{
				"progress":          state.live.Progress,
				"last_delta_kind":   state.live.LastDeltaKind,
				"last_message":      state.live.LastMessage,
				"partial_output":    state.live.PartialOutput,
				"patch_preview":     state.live.PatchPreview,
				"patch_draft":       state.live.PatchDraft,
				"active_workers":    append([]string(nil), state.live.ActiveWorkers...),
				"active_tools":      append([]string(nil), state.live.ActiveTools...),
				"completed_workers": append([]string(nil), state.live.CompletedWorkers...),
				"pending_approvals": append([]string(nil), state.live.PendingApprovals...),
				"updated_at":        state.live.UpdatedAt,
				"agent_id":          state.live.AgentID,
				"provider":          state.live.Provider,
				"model_name":        state.live.ModelName,
				"capabilities":      state.live.Capabilities,
				"realtime_metrics":  mergedRealtimeMetrics(state),
			},
		})
	}
	return map[string]any{
		"enabled":              true,
		"attached":             true,
		"status":               "active",
		"name":                 r.config.Name,
		"backend":              r.config.Backend,
		"allowed_providers":    append([]string(nil), r.config.AllowedProviders...),
		"session_count":        len(r.sessions),
		"active_session_count": active,
		"started_at":           r.startedAt,
		"sessions":             sessions,
	}
}

func (r *managedCodingRuntime) runSession(ctx context.Context, sessionID string) {
	state, ok := r.sessionState(sessionID)
	if !ok {
		return
	}
	result, err := r.executeSession(ctx, state)
	r.mu.Lock()
	state.result = result
	state.err = err
	state.active = false
	state.finishedAt = time.Now().UTC()
	r.orchestrator.CompleteLiveRealtimeSession(sessionID, state.live.Provider, state.live.ModelName, state.finishedAt, err != nil)
	close(state.done)
	close(state.events)
	r.mu.Unlock()
}

func (r *managedCodingRuntime) executeSession(ctx context.Context, state *managedCodingRuntimeSessionState) (domain.AgentResult, error) {
	req := state.request
	task := req.Task
	plan := req.Plan
	if plan.TaskID == "" {
		plan.TaskID = task.ID
	}
	if plan.Complexity == "" {
		plan.Complexity = task.Complexity
	}
	capability := resolvedCapability(task)
	r.emit(state, "task.accepted", "coding runtime accepted task", 0.05, map[string]any{
		"task_id":     task.ID,
		"mode":        string(req.Mode),
		"capability":  capability,
		"backend":     r.config.Backend,
		"rag_results": len(req.RAGResults),
	})

	if shouldUsePlanLanes(plan) {
		return r.executePlanLanes(ctx, state, task, plan, req)
	}

	acceptance, agent, ok := r.routeTask(task, plan, req)
	if !ok || agent == nil {
		reason := acceptance.Reason
		if strings.TrimSpace(reason) == "" {
			reason = "no available coding agent matched realtime runtime constraints"
		}
		result := domain.AgentResult{
			TaskID:      task.ID,
			Status:      domain.TaskStatusFailed,
			Errors:      []string{reason},
			CompletedAt: time.Now().UTC(),
			Output:      domain.ResultOutput{Summary: reason},
		}
		r.emit(state, "task.failed", reason, 1, map[string]any{"task_id": task.ID})
		return result, errors.New(reason)
	}

	selected := agent.Info()
	r.setSelected(state, acceptance, selected)

	task = r.decorateTask(task, req, selected, acceptance)
	r.emit(state, "task.routing", "coding runtime selected agent", 0.2, map[string]any{
		"task_id":    task.ID,
		"agent_id":   selected.ID,
		"provider":   selected.Provider,
		"model_name": selected.ModelName,
		"mode":       string(req.Mode),
	})

	task, acceptance, agent, budgetFailure := r.orchestrator.enforceModelBudgetPolicy(ctx, task, plan, acceptance, agent)
	if budgetFailure != nil {
		r.emit(state, "task.failed", budgetFailure.Output.Summary, 1, map[string]any{
			"task_id":    task.ID,
			"agent_id":   selected.ID,
			"provider":   acceptance.Provider,
			"model_name": acceptance.ModelName,
		})
		return *budgetFailure, errors.New(budgetFailure.Output.Summary)
	}

	selected = agent.Info()
	r.setSelected(state, acceptance, selected)
	task = r.orchestrator.attachRuntimeContext(ctx, task, acceptance, selected)
	r.emit(state, "task.running", "coding runtime started task execution", 0.45, map[string]any{
		"task_id":    task.ID,
		"agent_id":   selected.ID,
		"provider":   selected.Provider,
		"model_name": selected.ModelName,
	})

	acceptance, task, result := r.orchestrator.executeTaskP2P(ctx, task, plan, acceptance, agent)
	if result.Provider == "" {
		result.Provider = acceptance.Provider
	}
	if result.ModelName == "" {
		result.ModelName = acceptance.ModelName
	}
	if result.AgentID == "" {
		result.AgentID = acceptance.AgentID
	}
	if result.Status == "" {
		result.Status = acceptance.Status
	}
	message := strings.TrimSpace(result.Output.Summary)
	if message == "" {
		message = string(result.Status)
	}
	kind := "task.completed"
	if result.Status == domain.TaskStatusFailed || result.Status == domain.TaskStatusDeadLettered {
		kind = "task.failed"
	}
	r.emit(state, kind, message, 1, map[string]any{
		"task_id":    task.ID,
		"agent_id":   result.AgentID,
		"provider":   result.Provider,
		"model_name": result.ModelName,
		"status":     result.Status,
	})
	if result.Status == domain.TaskStatusFailed || result.Status == domain.TaskStatusDeadLettered {
		return result, errors.New(message)
	}
	return result, nil
}

func (r *managedCodingRuntime) routeTask(task domain.Task, plan domain.ExecutionPlan, req domain.CodingRuntimeRequest) (domain.TaskAcceptance, agents.Agent, bool) {
	exclude := map[string]struct{}{}
	providers := r.effectiveProviders(task, req)
	if preferred := strings.TrimSpace(preferredAgentID(task.RoutingHints)); preferred != "" {
		if candidate, ok := r.orchestrator.registry.AgentByID(preferred); ok && r.agentAllowed(candidate.Info(), providers, req.AllowedSubagents) {
			acceptance := accepted(task, plan, candidate, resolvedCapability(task), "preferred agent accepted by managed coding runtime", r.orchestrator.runtime)
			return acceptance, candidate, true
		}
		exclude[preferred] = struct{}{}
	}
	return r.routeOnce(task, plan, providers, exclude, req.AllowedSubagents)
}

func (r *managedCodingRuntime) routeOnce(task domain.Task, plan domain.ExecutionPlan, providers []string, exclude map[string]struct{}, allowedSubagents []string) (domain.TaskAcceptance, agents.Agent, bool) {
	capability := resolvedCapability(task)
	complexity := plan.Complexity
	bestScore := -1e9
	var best agents.Agent
	for _, candidate := range r.orchestrator.registry.Agents() {
		info := candidate.Info()
		if _, blocked := exclude[info.ID]; blocked {
			continue
		}
		if !r.agentAllowed(info, providers, allowedSubagents) {
			continue
		}
		if !r.orchestrator.router.canRouteAgent(task, candidate, capability) {
			continue
		}
		score := r.orchestrator.router.scoreAgent(context.Background(), candidate, task, capability, complexity, EvaluateRiskContext(taskText(task)))
		if score > bestScore {
			bestScore = score
			best = candidate
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no available agent matched realtime coding runtime constraints"), nil, false
	}
	return accepted(task, plan, best, capability, "managed coding runtime routing", r.orchestrator.runtime), best, true
}

func (r *managedCodingRuntime) agentAllowed(info domain.AgentInfo, providers []string, allowedSubagents []string) bool {
	if len(providers) > 0 && !containsFold(providers, info.Provider) {
		return false
	}
	if len(allowedSubagents) == 0 {
		return true
	}
	for _, selector := range allowedSubagents {
		selector = strings.TrimSpace(selector)
		if selector == "" {
			continue
		}
		if strings.EqualFold(selector, info.ID) || strings.EqualFold(selector, info.Type) || strings.EqualFold(selector, info.Provider) {
			return true
		}
		if strings.HasPrefix(selector, "provider:") && strings.EqualFold(strings.TrimSpace(strings.TrimPrefix(selector, "provider:")), info.Provider) {
			return true
		}
		if strings.HasPrefix(selector, "agent:") && strings.EqualFold(strings.TrimSpace(strings.TrimPrefix(selector, "agent:")), info.ID) {
			return true
		}
		if strings.HasPrefix(selector, "type:") && strings.EqualFold(strings.TrimSpace(strings.TrimPrefix(selector, "type:")), info.Type) {
			return true
		}
	}
	return false
}

func (r *managedCodingRuntime) effectiveProviders(task domain.Task, req domain.CodingRuntimeRequest) []string {
	providers := []string{}
	if provider := strings.TrimSpace(task.AssignedProvider); provider != "" {
		providers = append(providers, provider)
	}
	providers = append(providers, selectorProviders(req.AllowedSubagents)...)
	providers = append(providers, r.config.AllowedProviders...)
	return normalizeRuntimeProviderList(providers)
}

func (r *managedCodingRuntime) decorateTask(task domain.Task, req domain.CodingRuntimeRequest, selected domain.AgentInfo, acceptance domain.TaskAcceptance) domain.Task {
	task.AssignedProvider = acceptance.Provider
	task.AssignedModel = acceptance.ModelName
	hints := cloneMap(task.RoutingHints)
	hints["coding_runtime"] = true
	hints["coding_runtime_name"] = r.Name()
	hints["coding_runtime_backend"] = r.config.Backend
	hints["coding_runtime_mode"] = string(req.Mode)
	hints["runtime_selected_agent_id"] = selected.ID
	hints["runtime_selected_provider"] = selected.Provider
	hints["runtime_selected_model"] = selected.ModelName
	if len(req.AllowedSubagents) > 0 {
		hints["allowed_subagents"] = append([]string(nil), req.AllowedSubagents...)
	}
	task.RoutingHints = hints
	contract := cloneMap(task.ExecutionContract)
	contract["coding_runtime"] = r.Name()
	contract["coding_runtime_backend"] = r.config.Backend
	contract["coding_runtime_mode"] = string(req.Mode)
	contract["coding_runtime_session_id"] = fmt.Sprintf("%s:%s", r.Name(), task.ID)
	contract["coding_runtime_selected_agent_id"] = selected.ID
	contract["coding_runtime_selected_provider"] = selected.Provider
	contract["coding_runtime_selected_model"] = selected.ModelName
	task.ExecutionContract = contract
	return task
}

func shouldUsePlanLanes(plan domain.ExecutionPlan) bool {
	if len(plan.Steps) == 0 {
		return false
	}
	for _, step := range plan.Steps {
		if strings.TrimSpace(step.WorkerClass) != "" {
			return true
		}
	}
	return false
}

func (r *managedCodingRuntime) executePlanLanes(ctx context.Context, state *managedCodingRuntimeSessionState, rootTask domain.Task, plan domain.ExecutionPlan, req domain.CodingRuntimeRequest) (domain.AgentResult, error) {
	results := make(chan managedCodingRuntimeLaneResult, len(plan.Steps))
	started := make(map[string]struct{}, len(plan.Steps))
	running := make(map[string]domain.PlanStep, len(plan.Steps))
	completed := make(map[string]managedCodingRuntimeLaneResult, len(plan.Steps))
	total := len(plan.Steps)
	completedCount := 0

	for completedCount < total {
		launched := false
		ready := make([]domain.PlanStep, 0, len(plan.Steps))
		for _, step := range plan.Steps {
			if _, ok := started[step.ID]; ok {
				continue
			}
			depsReady := true
			for _, dep := range step.Dependencies {
				if _, ok := completed[dep]; !ok {
					depsReady = false
					break
				}
			}
			if !depsReady {
				continue
			}
			ready = append(ready, step)
		}
		sort.SliceStable(ready, func(i, j int) bool {
			left, right := ready[i], ready[j]
			if runtimeLanePriority(left) != runtimeLanePriority(right) {
				return runtimeLanePriority(left) < runtimeLanePriority(right)
			}
			return left.ID < right.ID
		})
		for _, step := range ready {
			started[step.ID] = struct{}{}
			running[step.ID] = step
			r.setActiveWorkers(state, r.activeWorkerList(running))
			timeout := r.laneTimeout(step)
			r.emit(state, "lane.started", step.Title, 0.1+0.7*(float64(len(completed))/float64(maxInt(total, 1))), map[string]any{
				"lane_id":      step.ID,
				"lane_title":   step.Title,
				"worker_class": runtimeStepWorkerClass(step),
				"capability":   runtimeStepCapability(step, rootTask),
				"dependencies": append([]string(nil), step.Dependencies...),
				"priority":     runtimeLanePriority(step),
				"timeout_ms":   timeout.Milliseconds(),
			})
			go func(step domain.PlanStep, timeout time.Duration) {
				laneCtx, cancel := context.WithTimeout(ctx, timeout)
				defer cancel()
				results <- r.executeLaneStep(laneCtx, state, rootTask, plan, req, step)
			}(step, timeout)
			launched = true
		}

		if len(running) == 0 {
			if !launched {
				result := domain.AgentResult{
					TaskID:      rootTask.ID,
					Status:      domain.TaskStatusFailed,
					CompletedAt: time.Now().UTC(),
					Errors:      []string{"plan lanes are blocked by unresolved dependencies"},
					Output:      domain.ResultOutput{Summary: "plan lanes are blocked by unresolved dependencies"},
				}
				r.emit(state, "task.failed", result.Output.Summary, 1, map[string]any{"task_id": rootTask.ID})
				return result, errors.New(result.Output.Summary)
			}
			continue
		}

		select {
		case <-ctx.Done():
			result := domain.AgentResult{
				TaskID:      rootTask.ID,
				Status:      domain.TaskStatusFailed,
				CompletedAt: time.Now().UTC(),
				Errors:      []string{ctx.Err().Error()},
				Output:      domain.ResultOutput{Summary: ctx.Err().Error()},
			}
			r.emit(state, "task.cancelled", ctx.Err().Error(), r.currentProgress(state), map[string]any{"task_id": rootTask.ID})
			return result, ctx.Err()
		case laneResult := <-results:
			delete(running, laneResult.step.ID)
			completed[laneResult.step.ID] = laneResult
			completedCount++
			r.setActiveWorkers(state, r.activeWorkerList(running))
			if laneResult.err != nil || laneResult.result.Status == domain.TaskStatusFailed || laneResult.result.Status == domain.TaskStatusDeadLettered {
				message := strings.TrimSpace(laneResult.result.Output.Summary)
				if message == "" && laneResult.err != nil {
					message = laneResult.err.Error()
				}
				if message == "" {
					message = "lane execution failed"
				}
				r.emit(state, "lane.failed", message, 0.1+0.7*(float64(completedCount)/float64(maxInt(total, 1))), map[string]any{
					"lane_id":      laneResult.step.ID,
					"lane_title":   laneResult.step.Title,
					"worker_class": runtimeStepWorkerClass(laneResult.step),
					"agent_id":     laneResult.result.AgentID,
					"provider":     laneResult.result.Provider,
					"model_name":   laneResult.result.ModelName,
					"status":       laneResult.result.Status,
				})
				r.emit(state, "task.failed", message, 1, map[string]any{"task_id": rootTask.ID, "lane_id": laneResult.step.ID})
				if laneResult.err != nil {
					return laneResult.result, laneResult.err
				}
				return laneResult.result, errors.New(message)
			}
			message := strings.TrimSpace(laneResult.result.Output.Summary)
			if message == "" {
				message = laneResult.step.Title
			}
			r.markWorkerCompleted(state, runtimeStepWorkerClass(laneResult.step))
			r.emit(state, "lane.completed", message, 0.1+0.7*(float64(completedCount)/float64(maxInt(total, 1))), map[string]any{
				"lane_id":        laneResult.step.ID,
				"lane_title":     laneResult.step.Title,
				"worker_class":   runtimeStepWorkerClass(laneResult.step),
				"agent_id":       laneResult.result.AgentID,
				"provider":       laneResult.result.Provider,
				"model_name":     laneResult.result.ModelName,
				"status":         laneResult.result.Status,
				"partial_output": laneResult.result.Output.Summary,
			})
		}
	}

	aggregated := r.aggregateLaneResults(rootTask, plan, completed)
	r.emit(state, "task.completed", aggregated.Output.Summary, 1, map[string]any{
		"task_id":        rootTask.ID,
		"lane_count":     len(plan.Steps),
		"partial_output": aggregated.Output.Summary,
	})
	return aggregated, nil
}

func (r *managedCodingRuntime) executeLaneStep(ctx context.Context, state *managedCodingRuntimeSessionState, rootTask domain.Task, plan domain.ExecutionPlan, req domain.CodingRuntimeRequest, step domain.PlanStep) managedCodingRuntimeLaneResult {
	laneTask := r.laneTask(rootTask, req, step)
	acceptance, agent, ok := r.routeTaskForStep(laneTask, plan, req, step)
	if !ok || agent == nil {
		reason := acceptance.Reason
		if strings.TrimSpace(reason) == "" {
			reason = "no agent matched lane constraints"
		}
		return managedCodingRuntimeLaneResult{
			step: step,
			task: laneTask,
			result: domain.AgentResult{
				TaskID:      laneTask.ID,
				Status:      domain.TaskStatusFailed,
				CompletedAt: time.Now().UTC(),
				Errors:      []string{reason},
				Output:      domain.ResultOutput{Summary: reason},
			},
			err: errors.New(reason),
		}
	}

	selected := agent.Info()
	r.emit(state, "lane.routing", step.Title, r.currentProgress(state), map[string]any{
		"lane_id":      step.ID,
		"lane_title":   step.Title,
		"worker_class": runtimeStepWorkerClass(step),
		"agent_id":     selected.ID,
		"provider":     selected.Provider,
		"model_name":   selected.ModelName,
	})

	laneTask = r.decorateTask(laneTask, req, selected, acceptance)
	laneTask, acceptance, agent, budgetFailure := r.orchestrator.enforceModelBudgetPolicy(ctx, laneTask, plan, acceptance, agent)
	if budgetFailure != nil {
		return managedCodingRuntimeLaneResult{step: step, task: laneTask, acceptance: acceptance, result: *budgetFailure, err: errors.New(budgetFailure.Output.Summary), selected: selected}
	}
	selected = agent.Info()
	r.setSelected(state, acceptance, selected)
	laneTask = r.orchestrator.attachRuntimeContext(ctx, laneTask, acceptance, selected)
	acceptance, laneTask, result := r.orchestrator.executeTaskP2P(ctx, laneTask, plan, acceptance, agent)
	if result.Provider == "" {
		result.Provider = acceptance.Provider
	}
	if result.ModelName == "" {
		result.ModelName = acceptance.ModelName
	}
	if result.AgentID == "" {
		result.AgentID = acceptance.AgentID
	}
	if result.Status == "" {
		result.Status = acceptance.Status
	}
	return managedCodingRuntimeLaneResult{step: step, task: laneTask, acceptance: acceptance, result: result, selected: selected}
}

func (r *managedCodingRuntime) aggregateLaneResults(rootTask domain.Task, plan domain.ExecutionPlan, completed map[string]managedCodingRuntimeLaneResult) domain.AgentResult {
	summaryParts := make([]string, 0, len(plan.Steps))
	laneArtifacts := make([]map[string]any, 0, len(plan.Steps))
	provider := ""
	modelName := ""
	agentID := ""
	for _, step := range plan.Steps {
		lane := completed[step.ID]
		if provider == "" {
			provider = lane.result.Provider
		}
		if modelName == "" {
			modelName = lane.result.ModelName
		}
		if agentID == "" {
			agentID = lane.result.AgentID
		}
		if summary := strings.TrimSpace(lane.result.Output.Summary); summary != "" {
			summaryParts = append(summaryParts, fmt.Sprintf("[%s] %s", step.ID, summary))
		}
		laneArtifacts = append(laneArtifacts, map[string]any{
			"lane_id":      step.ID,
			"lane_title":   step.Title,
			"worker_class": runtimeStepWorkerClass(step),
			"status":       lane.result.Status,
			"provider":     lane.result.Provider,
			"model_name":   lane.result.ModelName,
			"agent_id":     lane.result.AgentID,
			"summary":      lane.result.Output.Summary,
		})
	}
	summary := strings.Join(summaryParts, "\n")
	if strings.TrimSpace(summary) == "" {
		summary = "realtime coding plan completed"
	}
	return domain.AgentResult{
		TaskID:      rootTask.ID,
		AgentID:     agentID,
		Provider:    provider,
		ModelName:   modelName,
		Status:      domain.TaskStatusCompleted,
		Confidence:  0.9,
		CompletedAt: time.Now().UTC(),
		Output: domain.ResultOutput{
			Summary: summary,
			Artifacts: map[string]any{
				"runtime":      "go",
				"transport":    string(domain.RuntimeTransportNativeStream),
				"lane_results": laneArtifacts,
			},
		},
	}
}

func (r *managedCodingRuntime) routeTaskForStep(task domain.Task, plan domain.ExecutionPlan, req domain.CodingRuntimeRequest, step domain.PlanStep) (domain.TaskAcceptance, agents.Agent, bool) {
	providers := r.effectiveProviders(task, req)
	workerClass := runtimeStepWorkerClass(step)
	capability := runtimeStepCapability(step, task)
	rootCapability := ""
	if task.ExecutionContract != nil {
		rootCapability = strings.TrimSpace(stringValue(task.ExecutionContract["root_capability"]))
	}
	if rootCapability == "" && task.RoutingHints != nil {
		rootCapability = strings.TrimSpace(stringValue(task.RoutingHints["root_capability"]))
	}
	if rootCapability == "" {
		rootCapability = resolvedCapability(task)
	}
	complexity := plan.Complexity
	selectBest := func(enforceWorkerClass bool, routeCapability string) agents.Agent {
		bestScore := -1e9
		var best agents.Agent
		for _, candidate := range r.orchestrator.registry.Agents() {
			info := candidate.Info()
			if !r.agentAllowed(info, providers, req.AllowedSubagents) {
				continue
			}
			if enforceWorkerClass && workerClass != "" && !runtimeAgentMatchesWorkerClass(info, workerClass) {
				continue
			}
			if !r.orchestrator.router.canRouteAgent(task, candidate, routeCapability) {
				continue
			}
			score := r.orchestrator.router.scoreAgent(context.Background(), candidate, task, routeCapability, complexity, EvaluateRiskContext(taskText(task)))
			if score > bestScore {
				bestScore = score
				best = candidate
			}
		}
		return best
	}
	best := selectBest(true, capability)
	if best == nil && workerClass != "" {
		best = selectBest(false, capability)
	}
	if best == nil && rootCapability != "" && rootCapability != capability {
		best = selectBest(true, rootCapability)
		if best == nil && workerClass != "" {
			best = selectBest(false, rootCapability)
		}
	}
	if best == nil {
		return rejected(task, complexity, capability, "no available agent matched realtime lane constraints"), nil, false
	}
	return accepted(task, plan, best, capability, "managed coding runtime lane routing", r.orchestrator.runtime), best, true
}

func (r *managedCodingRuntime) laneTask(rootTask domain.Task, req domain.CodingRuntimeRequest, step domain.PlanStep) domain.Task {
	laneTask := rootTask
	laneTask.ID = rootTask.ID + ":" + step.ID
	laneTask.ParentTaskID = rootTask.ID
	laneTask.Type = runtimeStepTaskType(step, rootTask)
	laneTask.RequiredCapability = runtimeStepCapability(step, rootTask)
	laneTask.Input.Description = strings.TrimSpace(step.Title)
	if laneTask.Input.Description == "" {
		laneTask.Input.Description = rootTask.Input.Description
	}
	if len(step.Files) > 0 {
		laneTask.Input.Files = append([]string(nil), step.Files...)
	}
	hints := cloneMap(laneTask.RoutingHints)
	hints["coding_runtime"] = true
	hints["runtime_lane_id"] = step.ID
	hints["runtime_lane_title"] = step.Title
	hints["worker_class"] = runtimeStepWorkerClass(step)
	hints["root_capability"] = resolvedCapability(rootTask)
	laneTask.RoutingHints = hints
	contract := cloneMap(laneTask.ExecutionContract)
	contract["coding_runtime"] = r.Name()
	contract["runtime_lane_id"] = step.ID
	contract["runtime_lane_title"] = step.Title
	contract["worker_class"] = runtimeStepWorkerClass(step)
	contract["root_capability"] = resolvedCapability(rootTask)
	laneTask.ExecutionContract = contract
	return laneTask
}

func (r *managedCodingRuntime) setActiveWorkers(state *managedCodingRuntimeSessionState, workers []string) {
	r.mu.Lock()
	state.live.ActiveWorkers = append([]string(nil), workers...)
	state.live.UpdatedAt = time.Now().UTC()
	observe := managedRuntimeEventState{
		sessionID: state.session.SessionID,
		live:      cloneLiveSessionState(state.live),
	}
	startedAt := state.startedAt
	r.mu.Unlock()
	r.orchestrator.ObserveLiveRealtimeSession(observe.sessionID, observe.live.Provider, observe.live.ModelName, startedAt)
}

func (r *managedCodingRuntime) setSelected(state *managedCodingRuntimeSessionState, acceptance domain.TaskAcceptance, selected domain.AgentInfo) {
	capabilities := r.orchestrator.LookupModelCapabilities(selected.Provider, selected.ModelName)
	r.mu.Lock()
	defer r.mu.Unlock()
	state.selected = selected
	state.session.Provider = acceptance.Provider
	state.session.Model = acceptance.ModelName
	state.live.AgentID = selected.ID
	state.live.Provider = selected.Provider
	state.live.ModelName = selected.ModelName
	state.live.Capabilities = capabilities
	state.live.UpdatedAt = time.Now().UTC()
}

func (r *managedCodingRuntime) activeWorkerList(running map[string]domain.PlanStep) []string {
	workers := make([]string, 0, len(running))
	for _, step := range running {
		worker := runtimeStepWorkerClass(step)
		if worker == "" {
			worker = step.ID
		}
		workers = append(workers, worker)
	}
	return workers
}

func runtimeStepWorkerClass(step domain.PlanStep) string {
	workerClass := strings.ToLower(strings.TrimSpace(step.WorkerClass))
	if workerClass != "" {
		return workerClass
	}
	capability := strings.ToLower(strings.TrimSpace(step.Capability))
	switch capability {
	case "plan":
		return "planner"
	case "review":
		return "review"
	case "test":
		return "test"
	case "code", "fix":
		return "code"
	default:
		return capability
	}
}

func runtimeStepCapability(step domain.PlanStep, rootTask domain.Task) string {
	if capability := strings.TrimSpace(step.Capability); capability != "" {
		return capability
	}
	if capability := strings.TrimSpace(rootTask.RequiredCapability); capability != "" {
		return capability
	}
	return resolvedCapability(rootTask)
}

func runtimeStepTaskType(step domain.PlanStep, rootTask domain.Task) domain.TaskType {
	switch runtimeStepWorkerClass(step) {
	case "planner":
		return domain.TaskTypePlan
	case "review":
		return domain.TaskTypeReview
	case "test":
		return domain.TaskTypeTest
	default:
		return rootTask.Type
	}
}

func managedRuntimeMaxInt(value int, floor int) int {
	if value < floor {
		return floor
	}
	return value
}

func (r *managedCodingRuntime) markWorkerCompleted(state *managedCodingRuntimeSessionState, worker string) {
	worker = strings.TrimSpace(worker)
	if worker == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	state.live.CompletedWorkers = appendUniqueFold(state.live.CompletedWorkers, worker)
	state.live.ActiveWorkers = removeFold(state.live.ActiveWorkers, worker)
	state.live.UpdatedAt = time.Now().UTC()
}

func (r *managedCodingRuntime) currentProgress(state *managedCodingRuntimeSessionState) float64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return state.live.Progress
}

func (r *managedCodingRuntime) eventStateSnapshotLocked(state *managedCodingRuntimeSessionState) managedRuntimeEventState {
	return managedRuntimeEventState{
		sessionID: state.session.SessionID,
		taskID:    state.request.Task.ID,
		live:      cloneLiveSessionState(state.live),
	}
}

func metricInt64(value any) (int64, bool) {
	switch typed := value.(type) {
	case int64:
		return typed, true
	case int:
		return int64(typed), true
	case float64:
		return int64(typed), true
	case float32:
		return int64(typed), true
	default:
		return 0, false
	}
}

func metricInt(value any) (int, bool) {
	switch typed := value.(type) {
	case int:
		return typed, true
	case int64:
		return int(typed), true
	case float64:
		return int(typed), true
	case float32:
		return int(typed), true
	default:
		return 0, false
	}
}

func mergedRealtimeMetrics(state *managedCodingRuntimeSessionState) domain.RealtimeExecutionMetrics {
	if state == nil {
		return domain.RealtimeExecutionMetrics{}
	}
	metrics := state.live.RealtimeMetrics
	if state.result.Output.Artifacts == nil {
		return metrics
	}
	raw, ok := state.result.Output.Artifacts["realtime_metrics"].(map[string]any)
	if !ok {
		return metrics
	}
	if value := stringValue(raw["transport"]); value != "" {
		metrics.Transport = value
	}
	if value, ok := raw["native_streaming"].(bool); ok {
		metrics.NativeStreaming = value
	}
	if value, ok := raw["pseudo_realtime"].(bool); ok {
		metrics.PseudoRealtime = value
	}
	if value, ok := metricInt64(raw["time_to_first_token_ms"]); ok {
		metrics.TimeToFirstTokenMS = value
	}
	if value, ok := metricInt64(raw["time_to_first_tool_ms"]); ok {
		metrics.TimeToFirstToolMS = value
	}
	if value, ok := metricInt64(raw["time_to_first_patch_ms"]); ok {
		metrics.TimeToFirstPatchMS = value
	}
	if value, ok := metricInt64(raw["time_to_first_result_ms"]); ok {
		metrics.TimeToFirstResultMS = value
	}
	if value, ok := metricInt64(raw["time_to_first_test_ms"]); ok {
		metrics.TimeToFirstTestMS = value
	}
	if value, ok := metricInt64(raw["total_completion_ms"]); ok {
		metrics.TotalCompletionMS = value
	}
	if value, ok := metricInt(raw["tokens_streamed"]); ok {
		metrics.TokensStreamed = value
	}
	if value, ok := metricInt(raw["tools_executed"]); ok {
		metrics.ToolsExecuted = value
	}
	if value, ok := metricInt(raw["patches_applied"]); ok {
		metrics.PatchesApplied = value
	}
	if value, ok := metricInt(raw["tests_executed"]); ok {
		metrics.TestsExecuted = value
	}
	return metrics
}

func (r *managedCodingRuntime) syncLiveProtocolLocked(state *managedCodingRuntimeSessionState, kind string, metadata map[string]any, message string, occurredAt time.Time) {
	elapsedMS := occurredAt.Sub(state.startedAt).Milliseconds()
	metrics := &state.live.RealtimeMetrics
	if metrics.Transport == "" {
		metrics.Transport = stringValue(metadata["transport"])
	}
	if value, ok := metadata["native_streaming"].(bool); ok {
		metrics.NativeStreaming = value
	}
	if value, ok := metadata["pseudo_realtime"].(bool); ok {
		metrics.PseudoRealtime = value
	}
	if preview := stringValue(metadata["patch_preview"]); preview != "" {
		state.live.PatchPreview = preview
	}
	if chunk := stringValue(metadata["patch_chunk"]); chunk != "" {
		state.live.PatchDraft += chunk
	}
	if partial := stringValue(metadata["partial_output"]); partial != "" {
		state.live.PartialOutput = partial
	}
	switch kind {
	case string(domain.AgentDeltaToken):
		if metrics.TimeToFirstTokenMS == 0 {
			metrics.TimeToFirstTokenMS = elapsedMS
		}
		metrics.TokensStreamed++
	case string(domain.AgentDeltaPartialResult), string(domain.AgentDeltaFinalResult):
		if metrics.TimeToFirstResultMS == 0 {
			metrics.TimeToFirstResultMS = elapsedMS
		}
	case string(domain.AgentDeltaPatchApplyStart):
		if metrics.TimeToFirstPatchMS == 0 {
			metrics.TimeToFirstPatchMS = elapsedMS
		}
		state.live.PendingApprovals = appendUniqueFold(state.live.PendingApprovals, "patch_apply")
	case string(domain.AgentDeltaPatchApplyFinish):
		if metrics.TimeToFirstPatchMS == 0 {
			metrics.TimeToFirstPatchMS = elapsedMS
		}
		metrics.PatchesApplied++
		state.live.PendingApprovals = removeFold(state.live.PendingApprovals, "patch_apply")
	case string(domain.AgentDeltaToolStarted):
		if metrics.TimeToFirstToolMS == 0 {
			metrics.TimeToFirstToolMS = elapsedMS
		}
		metrics.ToolsExecuted++
		tool := strings.TrimSpace(stringValue(metadata["tool_name"]))
		if tool == "" {
			tool = strings.TrimSpace(message)
		}
		if tool != "" {
			state.live.ActiveTools = appendUniqueFold(state.live.ActiveTools, tool)
		}
	case string(domain.AgentDeltaToolFinished):
		tool := strings.TrimSpace(stringValue(metadata["tool_name"]))
		if tool == "" {
			tool = strings.TrimSpace(message)
		}
		if tool != "" {
			state.live.ActiveTools = removeFold(state.live.ActiveTools, tool)
		}
	case string(domain.AgentDeltaTestStarted):
		if metrics.TimeToFirstTestMS == 0 {
			metrics.TimeToFirstTestMS = elapsedMS
		}
		metrics.TestsExecuted++
	case "lane.completed":
		worker := strings.TrimSpace(stringValue(metadata["worker_class"]))
		if worker != "" {
			state.live.CompletedWorkers = appendUniqueFold(state.live.CompletedWorkers, worker)
			state.live.ActiveWorkers = removeFold(state.live.ActiveWorkers, worker)
		}
	}
	if metrics.TotalCompletionMS < elapsedMS {
		metrics.TotalCompletionMS = elapsedMS
	}
	state.live.UpdatedAt = occurredAt
}

func appendUniqueFold(items []string, value string) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return items
	}
	for _, item := range items {
		if strings.EqualFold(strings.TrimSpace(item), value) {
			return items
		}
	}
	return append(items, value)
}

func removeFold(items []string, value string) []string {
	if len(items) == 0 || strings.TrimSpace(value) == "" {
		return append([]string(nil), items...)
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		if strings.EqualFold(strings.TrimSpace(item), value) {
			continue
		}
		result = append(result, item)
	}
	return result
}

func (r *managedCodingRuntime) emit(state *managedCodingRuntimeSessionState, kind string, message string, progress float64, metadata map[string]any) {
	occurredAt := time.Now().UTC()
	metadataCopy := cloneMap(metadata)
	metadataCopy["message"] = message
	metadataCopy["progress"] = progress

	r.mu.Lock()
	eventState := managedRuntimeEventState{
		sessionID: state.session.SessionID,
		taskID:    state.request.Task.ID,
	}
	event := domain.CodingRuntimeEvent{
		SessionID: eventState.sessionID,
		TaskID:    eventState.taskID,
		Kind:      kind,
		Message:   message,
		Progress:  progress,
		Timestamp: occurredAt,
		Metadata:  metadataCopy,
	}
	state.live.Progress = progress
	state.live.LastDeltaKind = kind
	state.live.LastMessage = message
	state.live.UpdatedAt = occurredAt
	r.syncLiveProtocolLocked(state, kind, metadataCopy, message, occurredAt)
	switch kind {
	case "task.completed", "task.failed", "task.cancelled":
		state.live.Active = false
	default:
		state.live.Active = true
	}
	eventState.live = cloneLiveSessionState(state.live)
	r.mu.Unlock()

	select {
	case state.events <- event:
	default:
	}
	payload := map[string]any{
		"session_id":  eventState.sessionID,
		"task_id":     eventState.taskID,
		"runtime":     r.Name(),
		"kind":        kind,
		"message":     message,
		"progress":    progress,
		"occurred_at": occurredAt,
		"metadata":    metadataCopy,
		"event":       event,
		"live":        eventState.live,
	}
	r.orchestrator.publishRuntimeEvent("runtime_sessions", kind, eventState.taskID, payload)
	r.orchestrator.publishRuntimeEvent("runtime_session:"+eventState.sessionID, kind, eventState.taskID, payload)
}

func (r *managedCodingRuntime) sessionState(sessionID string) (*managedCodingRuntimeSessionState, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	state, ok := r.sessions[sessionID]
	return state, ok
}

func normalizeRuntimeProviderList(values []string) []string {
	out := make([]string, 0, len(values))
	seen := map[string]struct{}{}
	for _, value := range values {
		trimmed := strings.ToLower(strings.TrimSpace(value))
		if trimmed == "" {
			continue
		}
		if _, ok := seen[trimmed]; ok {
			continue
		}
		seen[trimmed] = struct{}{}
		out = append(out, trimmed)
	}
	return out
}

func selectorProviders(selectors []string) []string {
	providers := make([]string, 0, len(selectors))
	for _, selector := range selectors {
		selector = strings.TrimSpace(selector)
		if strings.HasPrefix(strings.ToLower(selector), "provider:") {
			providers = append(providers, strings.TrimSpace(selector[len("provider:"):]))
		}
	}
	return providers
}

func containsFold(values []string, target string) bool {
	target = strings.TrimSpace(target)
	for _, value := range values {
		if strings.EqualFold(strings.TrimSpace(value), target) {
			return true
		}
	}
	return false
}
