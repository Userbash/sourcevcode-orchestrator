package kernel

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/delivery"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/localmodels"
	"sourcevcode-orchestrator/go-core/internal/memory"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
	"sourcevcode-orchestrator/go-core/internal/vfs"
)

type Orchestrator struct {
	registry         *Registry
	planner          *Planner
	router           *Router
	store            state.Store
	runtimeHub       *realtime.Hub
	inventoryHub     *realtime.Hub
	localModels      *localmodels.Manager
	messageBus       delivery.Bus
	delivery         *delivery.Supervisor
	memory           *memory.Manager
	runtime          *RuntimeManager
	vfs              *vfs.Manager
	providerRegistry *ProviderModelRegistry
	globalSlots      chan struct{}
	slotMu           sync.Mutex
	agentSlots       map[string]chan struct{}
	modelSlots       map[string]chan struct{}
	perAgentMax      int
	perModelMax      int
}

func NewOrchestrator(
	registry *Registry,
	planner *Planner,
	router *Router,
	store state.Store,
	runtimeHub *realtime.Hub,
	inventoryHub *realtime.Hub,
	providerRegistry *ProviderModelRegistry,
) *Orchestrator {
	if runtimeHub == nil {
		runtimeHub = realtime.NewHub("runtime", 128)
	}
	if inventoryHub == nil {
		inventoryHub = realtime.NewHub("inventory", 64)
	}
	o := &Orchestrator{
		registry:         registry,
		planner:          planner,
		router:           router,
		store:            store,
		runtimeHub:       runtimeHub,
		inventoryHub:     inventoryHub,
		messageBus:       delivery.OpenBusFromEnv(),
		memory:           memory.NewManager(store),
		providerRegistry: providerRegistry,
		agentSlots:       map[string]chan struct{}{},
		modelSlots:       map[string]chan struct{}{},
		perAgentMax:      envInt("GO_CORE_MAX_CONCURRENT_PER_AGENT", 2),
		perModelMax:      envInt("GO_CORE_MAX_CONCURRENT_PER_MODEL", 2),
	}
	if vfsManager, err := vfs.NewManager(store); err == nil {
		o.vfs = vfsManager
	}
	if limit := envInt("GO_CORE_MAX_CONCURRENT_TASKS", 8); limit > 0 {
		o.globalSlots = make(chan struct{}, limit)
	}
	o.delivery = delivery.NewSupervisor(o.messageBus, 30*time.Second)
	o.runtime = NewRuntimeManager(registry, func(ctx context.Context, probe bool) map[string]domain.ProviderHealth {
		return o.ProviderHealth(ctx, probe)
	})
	if o.providerRegistry != nil {
		o.providerRegistry.Start(context.Background())
	}
	if o.router != nil {
		o.router.runtime = o.runtime
		o.router.memory = o.memory
	}
	o.publishInventorySnapshot(context.Background())
	return o
}

func (o *Orchestrator) SubmitTask(ctx context.Context, task domain.Task) (domain.WorkflowRecord, error) {
	if task.ID == "" {
		task.ID = newTaskID()
	}
	if task.SessionID == "" {
		task.SessionID = task.ID
	}
	if task.CreatedAt.IsZero() {
		task.CreatedAt = time.Now().UTC()
	}
	if task.Priority == "" {
		task.Priority = domain.PriorityNormal
	}
	if task.MemoryScope == "" {
		task.MemoryScope = "session"
	}
	if task.CachePolicy == "" {
		task.CachePolicy = "read_write"
	}
	if task.Type == "" {
		acceptance := domain.TaskAcceptance{
			TaskID:     task.ID,
			Status:     domain.TaskStatusRejected,
			Complexity: domain.ComplexityLow,
			Reason:     "task type is required",
			AcceptedAt: time.Now().UTC(),
		}
		o.publishRuntimeEvent("tasks", "task.rejected", task.ID, map[string]any{
			"task":       task,
			"acceptance": acceptance,
		})
		return domain.WorkflowRecord{}, errors.New("task type is required")
	}

	if decision := preflightPolicy(task); decisionBlocks(decision) {
		acceptance := domain.TaskAcceptance{
			TaskID:     task.ID,
			Status:     domain.TaskStatusRejected,
			Complexity: domain.ComplexityLow,
			Reason:     firstNonEmptyString(strings.Join(decision.Reasons, "; "), "task rejected by policy"),
			AcceptedAt: time.Now().UTC(),
		}
		record := domain.WorkflowRecord{Task: task, Acceptance: acceptance, UpdatedAt: time.Now().UTC()}
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.publishRuntimeEvent("tasks", "task.rejected", task.ID, map[string]any{
			"task":       task,
			"acceptance": acceptance,
			"policy":     decision,
		})
		o.publishInventorySnapshot(ctx)
		return record, nil
	}

	guard := o.memory.CacheGuardSnapshot(ctx, task.SessionID, task.Context.Branch)
	if blocked, _ := guard["blocked"].(bool); blocked {
		reason := firstNonEmptyString(kernelString(guard["reason"]), "cache guard blocked task execution")
		action := firstNonEmptyString(kernelString(guard["action"]), "cache_guard_blocked")
		_ = o.memory.RecordHardStop(ctx, task, action, reason)
		acceptance := domain.TaskAcceptance{
			TaskID:     task.ID,
			Status:     domain.TaskStatusRejected,
			Complexity: domain.ComplexityLow,
			Reason:     reason,
			AcceptedAt: time.Now().UTC(),
		}
		result := &domain.AgentResult{
			TaskID:      task.ID,
			Status:      domain.TaskStatusFailed,
			Errors:      []string{reason},
			CompletedAt: time.Now().UTC(),
			Output: domain.ResultOutput{
				Summary: fmt.Sprintf("Task blocked by cache guard for session %s", task.SessionID),
				Artifacts: map[string]any{
					"cache_guard":        guard,
					"validation_context": guard,
				},
			},
		}
		record := domain.WorkflowRecord{Task: task, Acceptance: acceptance, Result: result, UpdatedAt: time.Now().UTC()}
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.publishRuntimeEvent("tasks", "task.failed", task.ID, map[string]any{
			"task":       task,
			"acceptance": acceptance,
			"result":     result,
			"validation": guard,
		})
		o.publishInventorySnapshot(ctx)
		return record, nil
	}

	task, plan := o.planner.Prepare(task)
	acceptance, agent, ok := o.router.Route(task, plan)
	record := domain.WorkflowRecord{
		Task:       task,
		Plan:       plan,
		Acceptance: acceptance,
		UpdatedAt:  time.Now().UTC(),
	}
	if !ok {
		record.Acceptance.Status = domain.TaskStatusRejected
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.publishRuntimeEvent("tasks", "task.rejected", task.ID, map[string]any{
			"task":       task,
			"plan":       plan,
			"acceptance": record.Acceptance,
		})
		o.publishInventorySnapshot(ctx)
		return record, nil
	}

	if o.runtime != nil {
		state, _ := o.runtime.State(agent.Info().ID)
		decision := assignmentPolicy(task, acceptance, agent.Info(), state)
		if decisionBlocks(decision) {
			record.Acceptance.Status = domain.TaskStatusRejected
			record.Acceptance.Reason = firstNonEmptyString(strings.Join(decision.Reasons, "; "), "assignment rejected by runtime policy")
			record.UpdatedAt = time.Now().UTC()
			if err := o.store.SaveWorkflow(ctx, record); err != nil {
				return domain.WorkflowRecord{}, err
			}
			o.publishRuntimeEvent("tasks", "task.rejected", task.ID, map[string]any{
				"task":       task,
				"plan":       plan,
				"acceptance": record.Acceptance,
				"policy":     decision,
			})
			o.publishInventorySnapshot(ctx)
			return record, nil
		}
	}

	task, acceptance, agent, budgetFailure := o.enforceModelBudgetPolicy(ctx, task, plan, acceptance, agent)
	record.Task = task
	record.Acceptance = acceptance
	if budgetFailure != nil {
		record.Result = budgetFailure
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.publishRuntimeEvent("tasks", "task.failed", task.ID, map[string]any{
			"task":       task,
			"plan":       plan,
			"acceptance": acceptance,
			"result":     budgetFailure,
		})
		o.publishInventorySnapshot(ctx)
		return record, nil
	}

	task = o.attachRuntimeContext(ctx, task, acceptance, agent.Info())
	record.Task = task

	o.publishRuntimeEvent("tasks", "task.accepted", task.ID, map[string]any{
		"task":       task,
		"plan":       plan,
		"acceptance": acceptance,
	})
	o.publishRuntimeEvent("tasks", "task.running", task.ID, map[string]any{
		"task":     task,
		"agent_id": agent.Info().ID,
		"plan":     plan,
	})
	releaseSlots, acquireErr := o.acquireExecutionSlots(ctx, agent.Info(), acceptance.ModelName)
	if acquireErr != nil {
		result := &domain.AgentResult{
			TaskID:      task.ID,
			AgentID:     agent.Info().ID,
			Status:      domain.TaskStatusFailed,
			Errors:      []string{acquireErr.Error()},
			Provider:    acceptance.Provider,
			ModelName:   acceptance.ModelName,
			CompletedAt: time.Now().UTC(),
			Output:      domain.ResultOutput{Summary: acquireErr.Error()},
		}
		record.Result = result
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.publishRuntimeEvent("tasks", "task.failed", task.ID, map[string]any{"task": task, "plan": plan, "result": result})
		return record, nil
	}
	defer releaseSlots()
	startedAt := time.Now().UTC()
	result := agent.Execute(ctx, task)
	latency := time.Since(startedAt)
	_ = o.memory.RecordModelUsage(ctx, task, acceptance.ModelName, result)
	if result.Status == "" {
		result.Status = domain.TaskStatusDone
	}
	if result.Status == domain.TaskStatusFailed {
		_ = o.memory.RecordHardStop(ctx, task, "agent_execute", result.Output.Summary)
		if o.runtime != nil {
			state := o.runtime.RecordRuntimeFailure(agent.Info().ID, result.Output.Summary)
			action := o.runtime.RecoveryActionForFailure(agent.Info().ID)
			if action == "quarantine_agent" {
				state = o.runtime.QuarantineAgent(agent.Info().ID, result.Output.Summary)
			}
			o.publishRuntimeEvent("agents", "agent.runtime_failure", agent.Info().ID, map[string]any{
				"agent_id": agent.Info().ID,
				"task_id":  task.ID,
				"action":   action,
				"state":    state,
			})
		}
	} else if o.runtime != nil {
		o.runtime.RecordSuccess(agent.Info().ID)
	}
	o.persistExecutionState(ctx, task, acceptance, agent.Info(), result, latency)
	record.Result = &result
	record.UpdatedAt = time.Now().UTC()
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		return domain.WorkflowRecord{}, err
	}
	kind := "task.completed"
	if result.Status == domain.TaskStatusFailed {
		kind = "task.failed"
	}
	o.publishRuntimeEvent("tasks", kind, task.ID, map[string]any{
		"task":   task,
		"plan":   plan,
		"result": result,
	})
	o.publishInventorySnapshot(ctx)
	return record, nil
}

func (o *Orchestrator) Workflow(ctx context.Context, workflowID string) (domain.WorkflowRecord, bool, error) {
	return o.store.GetWorkflow(ctx, workflowID)
}

func (o *Orchestrator) Workflows(ctx context.Context) ([]domain.WorkflowRecord, error) {
	return o.store.ListWorkflows(ctx)
}

func (o *Orchestrator) Agents() []domain.AgentInfo {
	return o.registry.AgentInfos()
}

func (o *Orchestrator) Modules() []domain.ModuleInfo {
	return o.registry.ModuleInfos()
}

func (o *Orchestrator) ProviderHealth(ctx context.Context, probe bool) map[string]domain.ProviderHealth {
	reporters := make(map[string]agents.HealthReporter)
	result := make(map[string]domain.ProviderHealth)
	for _, agent := range o.registry.Agents() {
		info := agent.Info()
		if _, exists := result[info.Provider]; exists {
			continue
		}
		configured := info.Status != domain.AgentStatusOffline
		status := "configured"
		if !configured {
			status = "not_configured"
		}
		result[info.Provider] = domain.ProviderHealth{
			Provider: info.Provider, Configured: configured, Status: status,
			ObservedAt: time.Now().UTC(),
		}
		if reporter, ok := agent.(agents.HealthReporter); ok {
			reporters[info.Provider] = reporter
		}
	}
	if o.providerRegistry != nil {
		if probe {
			o.providerRegistry.Refresh(ctx)
		}
		for _, snapshot := range o.providerRegistry.Snapshots() {
			result[snapshot.Provider] = domain.ProviderHealth{
				Provider:   snapshot.Provider,
				Configured: snapshot.Configured,
				Available:  snapshot.Available,
				Status:     snapshot.Status,
				BaseURL:    snapshot.BaseURL,
				Error:      snapshot.Error,
				ObservedAt: snapshot.ObservedAt,
			}
		}
	}
	if !probe {
		return result
	}

	probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	var mu sync.Mutex
	var group sync.WaitGroup
	for provider, reporter := range reporters {
		provider, reporter := provider, reporter
		group.Add(1)
		go func() {
			defer group.Done()
			health := reporter.Probe(probeCtx)
			mu.Lock()
			current, exists := result[provider]
			if !exists || health.Available || current.Status == "configured" {
				result[provider] = health
			}
			mu.Unlock()
		}()
	}
	group.Wait()
	return result
}

func (o *Orchestrator) ModuleState() map[string]any {
	state := make(map[string]any)
	for _, module := range o.registry.Modules() {
		state[module.Info().Name] = module.Snapshot()
	}
	state["store"] = o.store.Snapshot()
	return state
}

func (o *Orchestrator) StateSnapshot(ctx context.Context) map[string]any {
	workflows, _ := o.store.ListWorkflows(ctx)
	providerCatalogs := []domain.ProviderCatalogSnapshot{}
	if o.providerRegistry != nil {
		providerCatalogs = o.providerRegistry.Snapshots()
	}
	return map[string]any{
		"status":            "ready",
		"agent_count":       len(o.registry.AgentInfos()),
		"module_count":      len(o.registry.ModuleInfos()),
		"workflow_count":    len(workflows),
		"modules":           o.ModuleState(),
		"agents":            o.registry.AgentInfos(),
		"providers":         providerCatalogs,
		"provider_health":   o.ProviderHealth(ctx, false),
		"runtime_agents":    o.RuntimeStates(),
		"routing_weights":   o.RuntimeRoutingWeights(),
		"store":             o.store.Snapshot(),
		"delivery":          o.delivery.DeliveryHealthSnapshot(),
		"mailboxes":         o.delivery.RecordsSnapshot(),
		"runtime_streams":   o.runtimeHub.Stats(),
		"inventory_streams": o.inventoryHub.Stats(),
	}
}

func (o *Orchestrator) SubscribeRuntimeEvents(topic string) *realtime.Subscription {
	return o.runtimeHub.Subscribe(topic)
}

func (o *Orchestrator) SubscribeInventoryEvents(topic string) *realtime.Subscription {
	return o.inventoryHub.Subscribe(topic)
}

func (o *Orchestrator) AttachLocalModelManager(manager *localmodels.Manager) {
	o.localModels = manager
	o.publishInventorySnapshot(context.Background())
}

func (o *Orchestrator) LocalModelManager() *localmodels.Manager {
	return o.localModels
}

func (o *Orchestrator) DispatchEnvelope(ctx context.Context, envelope domain.TaskEnvelope) map[string]any {
	return o.delivery.Dispatch(ctx, envelope)
}

func (o *Orchestrator) RefreshDelivery(ctx context.Context, taskID string) map[string]any {
	return o.delivery.Refresh(ctx, taskID)
}

func (o *Orchestrator) InspectDeliveryTimeouts(ctx context.Context) map[string]any {
	return o.delivery.InspectTimeouts(ctx)
}

func (o *Orchestrator) DeliveryHealthSnapshot() map[string]any {
	return o.delivery.DeliveryHealthSnapshot()
}

func (o *Orchestrator) AckDelivery(ctx context.Context, taskID string, status domain.AckStatus, receivedBy string, reason string) domain.MessageAck {
	_ = ctx
	return o.delivery.Ack(taskID, status, receivedBy, reason)
}

func (o *Orchestrator) FetchAgentMailbox(ctx context.Context, agentID string, limit int) []domain.TaskEnvelope {
	return o.delivery.FetchAgentMailbox(ctx, agentID, limit)
}

func (o *Orchestrator) ConfirmDeliveryPayload(ctx context.Context, taskID string, agentID string, envelope domain.TaskEnvelope) bool {
	_ = ctx
	return o.delivery.ConfirmPayload(taskID, agentID, envelope)
}

func (o *Orchestrator) EstablishDeliveryHandshake(ctx context.Context, taskID string, agentID string) domain.MessageAck {
	_ = ctx
	return o.delivery.EstablishDelivery(taskID, agentID)
}

func (o *Orchestrator) MailboxSnapshot(agentID string) map[string]any {
	return o.delivery.MailboxSnapshot(agentID)
}

func (o *Orchestrator) RuntimeStates() []domain.AgentRuntimeState {
	return o.registry.RuntimeStates()
}

func (o *Orchestrator) RuntimeRoutingWeights() map[string]float64 {
	if o.runtime == nil {
		return map[string]float64{}
	}
	return o.runtime.RoutingWeights()
}

func (o *Orchestrator) ProviderCatalogs() []domain.ProviderCatalogSnapshot {
	if o.providerRegistry == nil {
		return nil
	}
	return o.providerRegistry.Snapshots()
}

func (o *Orchestrator) ProviderCatalogSnapshot(provider string) (domain.ProviderCatalogSnapshot, bool) {
	if o.providerRegistry == nil {
		return domain.ProviderCatalogSnapshot{}, false
	}
	return o.providerRegistry.Snapshot(provider)
}

func (o *Orchestrator) RefreshRoutingWeights() map[string]float64 {
	if o.runtime == nil {
		return map[string]float64{}
	}
	return o.runtime.RefreshRoutingWeights()
}

func (o *Orchestrator) ProbeProviderRuntime(ctx context.Context, provider string) map[string]any {
	if o.runtime == nil {
		return map[string]any{"status": "unavailable", "error": "runtime manager is unavailable"}
	}
	return o.runtime.ProbeProviderRuntime(ctx, provider)
}

func (o *Orchestrator) ProbeAgentRuntime(ctx context.Context, agentID string) map[string]any {
	if o.runtime == nil {
		return map[string]any{"status": "unavailable", "error": "runtime manager is unavailable"}
	}
	return o.runtime.ProbeAgentRuntime(ctx, agentID)
}

func (o *Orchestrator) SuppressLane(agentID string, reason string, seconds int) (domain.AgentRuntimeState, bool) {
	if o.runtime == nil {
		return domain.AgentRuntimeState{}, false
	}
	state, ok := o.runtime.SuppressLane(agentID, reason, seconds)
	if ok {
		o.publishInventorySnapshot(context.Background())
	}
	return state, ok
}

func (o *Orchestrator) RecoverLane(agentID string) (domain.AgentRuntimeState, bool) {
	if o.runtime == nil {
		return domain.AgentRuntimeState{}, false
	}
	state, ok := o.runtime.RecoverLane(agentID)
	if ok {
		o.publishInventorySnapshot(context.Background())
	}
	return state, ok
}

func (o *Orchestrator) RefreshInventory(ctx context.Context) {
	o.publishInventorySnapshot(ctx)
}

func (o *Orchestrator) RuntimeEventSnapshot(topic string) []domain.StreamEvent {
	return o.runtimeHub.Snapshot(topic)
}

func (o *Orchestrator) InventoryEventSnapshot(topic string) []domain.StreamEvent {
	return o.inventoryHub.Snapshot(topic)
}

func (o *Orchestrator) publishRuntimeEvent(topic string, kind string, entityID string, payload map[string]any) {
	o.runtimeHub.Publish(topic, kind, entityID, payload)
}

func (o *Orchestrator) publishInventorySnapshot(ctx context.Context) {
	workflows, _ := o.store.ListWorkflows(ctx)
	o.inventoryHub.Publish("agents", "inventory.snapshot", "agents", map[string]any{
		"items": o.registry.AgentInfos(),
	})
	o.inventoryHub.Publish("modules", "inventory.snapshot", "modules", map[string]any{
		"items": o.registry.ModuleInfos(),
	})
	providerCatalogs := []domain.ProviderCatalogSnapshot{}
	if o.providerRegistry != nil {
		providerCatalogs = o.providerRegistry.Snapshots()
	}
	o.inventoryHub.Publish("providers", "inventory.snapshot", "providers", map[string]any{
		"items": providerCatalogs,
	})
	o.inventoryHub.Publish("state", "inventory.snapshot", "state", map[string]any{
		"store":           o.store.Snapshot(),
		"workflow_count":  len(workflows),
		"delivery":        o.delivery.DeliveryHealthSnapshot(),
		"mailboxes":       o.delivery.RecordsSnapshot(),
		"runtime_agents":  o.RuntimeStates(),
		"routing_weights": o.RuntimeRoutingWeights(),
		"providers":       providerCatalogs,
	})
	o.inventoryHub.Publish("workflows", "inventory.snapshot", "workflows", map[string]any{
		"items": workflows,
	})
}

func (o *Orchestrator) attachRuntimeContext(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo) domain.Task {
	runtimeContext := o.memory.LoadMemoryContext(ctx, task, agent.ID, acceptance.Provider, acceptance.ModelName)
	hints := cloneMap(task.RoutingHints)
	hints["memory_context"] = runtimeContext
	if validation, ok := runtimeContext["validation_context"].(map[string]any); ok {
		validationCopy := cloneMap(validation)
		if budget, ok := hints["model_budget"]; ok {
			validationCopy["model_budget"] = budget
		}
		if pressure, ok := hints["token_pressure"]; ok {
			validationCopy["token_pressure"] = pressure
		}
		hints["validation_context"] = validationCopy
		runtimeContext["validation_context"] = validationCopy
	} else if validation, ok := runtimeContext["validation_context"]; ok {
		hints["validation_context"] = validation
	}
	hints["session_id"] = task.SessionID
	hints["cache_policy"] = task.CachePolicy
	hints["memory_scope"] = task.MemoryScope
	task.RoutingHints = hints
	o.persistSessionState(ctx, task, acceptance, agent, nil)
	return task
}

func (o *Orchestrator) persistExecutionState(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, result domain.AgentResult, latency time.Duration) {
	payload := map[string]any{
		"last_result_status":  result.Status,
		"last_result_summary": result.Output.Summary,
		"last_completed_at":   time.Now().UTC(),
		"last_latency_ms":     latency.Milliseconds(),
	}
	o.persistSessionState(ctx, task, acceptance, agent, payload)
	if o.memory != nil {
		_ = o.memory.RecordTaskExchange(ctx, task, result)
		_ = o.memory.RecordRouteOutcome(ctx, task, acceptance, result, latency)
	}
}

func (o *Orchestrator) persistSessionState(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, extra map[string]any) {
	if o.store == nil {
		return
	}
	sessionID := task.SessionID
	branch := defaultBranch(task.Context.Branch)
	stateValue := map[string]any{}
	promptVersion := "go-core"
	contextVersion := "go-core"
	if existing, ok, err := o.store.GetSessionState(ctx, sessionID, branch); err == nil && ok {
		stateValue = cloneMap(existing.State)
		if strings.TrimSpace(existing.PromptVersion) != "" {
			promptVersion = existing.PromptVersion
		}
		if strings.TrimSpace(existing.ContextVersion) != "" {
			contextVersion = existing.ContextVersion
		}
	}
	stateValue["session_id"] = sessionID
	stateValue["task_id"] = task.ID
	stateValue["task_type"] = string(task.Type)
	stateValue["project"] = task.Context.Project
	stateValue["repo_path"] = task.Context.RepoPath
	stateValue["branch"] = branch
	stateValue["agent_id"] = agent.ID
	stateValue["provider"] = acceptance.Provider
	stateValue["model_name"] = acceptance.ModelName
	stateValue["required_capability"] = task.RequiredCapability
	stateValue["memory_scope"] = task.MemoryScope
	stateValue["memory_keys"] = append([]string(nil), task.MemoryKeys...)
	stateValue["cache_policy"] = task.CachePolicy
	stateValue["updated_by"] = "go-core"
	stateValue["updated_at"] = time.Now().UTC()
	for key, value := range extra {
		stateValue[key] = value
	}
	_, _ = o.store.SaveSessionState(ctx, sessionID, branch, stateValue, promptVersion, contextVersion, nil)
}

func (o *Orchestrator) enforceModelBudgetPolicy(ctx context.Context, task domain.Task, plan domain.ExecutionPlan, acceptance domain.TaskAcceptance, agent agents.Agent) (domain.Task, domain.TaskAcceptance, agents.Agent, *domain.AgentResult) {
	if o.memory == nil {
		return task, acceptance, agent, nil
	}
	plannedTokens := o.memory.EstimateTaskTokens(task)
	budget := o.memory.EvaluateModelBudget(ctx, task, acceptance.ModelName, plannedTokens)
	hints := cloneMap(task.RoutingHints)
	hints["model_budget"] = budget
	action := strings.ToLower(kernelString(budget["action"]))
	if action == "reduce" {
		hints["token_pressure"] = "reduce"
	} else {
		delete(hints, "token_pressure")
	}
	task.RoutingHints = hints
	if action != "error" {
		return task, acceptance, agent, nil
	}

	exclude := map[string]struct{}{agent.Info().ID: {}}
	providers := budgetFallbackProviders(acceptance.Provider)
	for {
		fallbackAcceptance, fallbackAgent, ok := o.router.RouteWithinProviders(task, plan, providers, exclude)
		if !ok {
			break
		}
		fallbackBudget := o.memory.EvaluateModelBudget(ctx, task, fallbackAcceptance.ModelName, plannedTokens)
		fallbackAction := strings.ToLower(kernelString(fallbackBudget["action"]))
		if fallbackAction == "error" {
			exclude[fallbackAgent.Info().ID] = struct{}{}
			continue
		}
		hints = cloneMap(task.RoutingHints)
		hints["model_budget"] = fallbackBudget
		if fallbackAction == "reduce" {
			hints["token_pressure"] = "reduce"
		} else {
			delete(hints, "token_pressure")
		}
		task.RoutingHints = hints
		task.AssignedProvider = fallbackAcceptance.Provider
		task.AssignedModel = fallbackAcceptance.ModelName
		return task, fallbackAcceptance, fallbackAgent, nil
	}

	summary := fmt.Sprintf(
		"Model %s blocked: remaining token budget %.2f%% is below floor %.2f%%",
		acceptance.ModelName,
		asFloat64(budget["remaining_percentage"]),
		asFloat64(budget["error_below_percentage"]),
	)
	result := &domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     agent.Info().ID,
		Status:      domain.TaskStatusFailed,
		Provider:    acceptance.Provider,
		ModelName:   acceptance.ModelName,
		Errors:      []string{summary},
		CompletedAt: time.Now().UTC(),
		Output: domain.ResultOutput{
			Summary: summary,
			Artifacts: map[string]any{
				"token_budget": budget,
			},
		},
	}
	return task, acceptance, agent, result
}

func budgetFallbackProviders(currentProvider string) []string {
	providers := []string{currentProvider, "local", "mistral", "openai"}
	ordered := make([]string, 0, len(providers))
	seen := map[string]struct{}{}
	for _, provider := range providers {
		trimmed := strings.ToLower(strings.TrimSpace(provider))
		if trimmed == "" {
			continue
		}
		if _, ok := seen[trimmed]; ok {
			continue
		}
		seen[trimmed] = struct{}{}
		ordered = append(ordered, trimmed)
	}
	return ordered
}

func asFloat64(value any) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int32:
		return float64(typed)
	case int64:
		return float64(typed)
	default:
		return 0
	}
}

func cloneMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}

func defaultBranch(branch string) string {
	if branch == "" {
		return "default"
	}
	return branch
}

func kernelString(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func (o *Orchestrator) acquireExecutionSlots(ctx context.Context, agent domain.AgentInfo, modelName string) (func(), error) {
	releases := make([]func(), 0, 3)
	acquire := func(ch chan struct{}) error {
		if ch == nil {
			return nil
		}
		select {
		case ch <- struct{}{}:
			releases = append(releases, func() { <-ch })
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if err := acquire(o.globalSlots); err != nil {
		return nil, err
	}
	if err := acquire(o.slotForAgent(agent.ID)); err != nil {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
		return nil, err
	}
	if err := acquire(o.slotForModel(modelName)); err != nil {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
		return nil, err
	}
	return func() {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
	}, nil
}

func (o *Orchestrator) slotForAgent(agentID string) chan struct{} {
	if o.perAgentMax <= 0 || strings.TrimSpace(agentID) == "" {
		return nil
	}
	o.slotMu.Lock()
	defer o.slotMu.Unlock()
	if slot, ok := o.agentSlots[agentID]; ok {
		return slot
	}
	slot := make(chan struct{}, o.perAgentMax)
	o.agentSlots[agentID] = slot
	return slot
}

func (o *Orchestrator) slotForModel(modelName string) chan struct{} {
	modelName = strings.TrimSpace(modelName)
	if o.perModelMax <= 0 || modelName == "" {
		return nil
	}
	o.slotMu.Lock()
	defer o.slotMu.Unlock()
	if slot, ok := o.modelSlots[modelName]; ok {
		return slot
	}
	slot := make(chan struct{}, o.perModelMax)
	o.modelSlots[modelName] = slot
	return slot
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}
