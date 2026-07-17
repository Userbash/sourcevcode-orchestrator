package kernel

import (
	"context"
	"encoding/json"
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
	"sourcevcode-orchestrator/go-core/internal/selflearn"
	"sourcevcode-orchestrator/go-core/internal/state"
	"sourcevcode-orchestrator/go-core/internal/vfs"
)

type Orchestrator struct {
	executionProfile executionProfile
	backgroundCtx    context.Context
	backgroundCancel context.CancelFunc
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
	adaptive         *AdaptiveRuntime
	vfs              *vfs.Manager
	providerRegistry *ProviderModelRegistry
	probeManager     *providerProbeManager
	globalSlots      chan struct{}
	slotMu           sync.Mutex
	agentSlots       map[string]chan struct{}
	modelSlots       map[string]chan struct{}
	perAgentMax      int
	perModelMax      int
	resultMu         sync.Mutex
	resultWaiters    map[string][]chan domain.WorkflowRecord
	workerPools      []*delivery.WorkerPool
	workerPoolsMu    sync.Mutex
	schedulerMu      sync.Mutex
	submissionQueue  *submissionScheduler
	selfLearn        selfLearningRuntime
}

type selfLearningRuntime struct {
	enabled            bool
	traceRecorder      domain.TraceRecorder
	evaluator          domain.CodeEvaluator
	datasetBuilder     domain.PreferenceDatasetBuilder
	trainer            domain.Trainer
	hotReloader        domain.ModelHotReloader
	discovery          domain.ModelDiscovery
	retriever          domain.RAGRetriever
	exporter           *selflearn.JSONLExporter
	trainingWindow     time.Duration
	trainingTicker     time.Duration
	trainingTimeout    time.Duration
	datasetDir         string
	reasoningEnabled   bool
	reasoningMaxRounds int
	criticEnabled      bool
	criticMaxRounds    int
}

type reasoningCandidate struct {
	task           domain.Task
	result         domain.AgentResult
	response       domain.ReasoningResponse
	ragResults     []domain.RAGResult
	evaluation     domain.CodeExecutionResult
	score          float64
	round          int
	ragQueries     []string
	criticFeedback []string
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
	profile := detectExecutionProfile()
	parallelism := profile.UsableParallelism
	backgroundCtx, backgroundCancel := context.WithCancel(context.Background())
	o := &Orchestrator{
		executionProfile: profile,
		backgroundCtx:    backgroundCtx,
		backgroundCancel: backgroundCancel,
		registry:         registry,
		planner:          planner,
		router:           router,
		store:            store,
		runtimeHub:       runtimeHub,
		inventoryHub:     inventoryHub,
		messageBus:       delivery.OpenBusFromEnv(),
		memory:           memory.NewManager(store),
		providerRegistry: providerRegistry,
		probeManager:     newProviderProbeManager(backgroundCtx),
		agentSlots:       map[string]chan struct{}{},
		modelSlots:       map[string]chan struct{}{},
		perAgentMax:      envInt("GO_CORE_MAX_CONCURRENT_PER_AGENT", defaultPerAgentConcurrency(parallelism)),
		perModelMax:      envInt("GO_CORE_MAX_CONCURRENT_PER_MODEL", defaultPerModelConcurrency(parallelism)),
		resultWaiters:    map[string][]chan domain.WorkflowRecord{},
	}
	if vfsManager, err := vfs.NewManager(store); err == nil {
		o.vfs = vfsManager
	}
	if limit := envInt("GO_CORE_MAX_CONCURRENT_TASKS", defaultGlobalConcurrency(parallelism)); limit > 0 {
		o.globalSlots = make(chan struct{}, limit)
	}
	o.delivery = delivery.NewSupervisor(o.messageBus, 30*time.Second)
	o.runtime = NewRuntimeManager(registry, providerRegistry, func(ctx context.Context, probe bool) map[string]domain.ProviderHealth {
		return o.ProviderHealth(ctx, probe)
	})
	o.adaptive = NewAdaptiveRuntime(registry, o.runtime, o.memory)
	if o.providerRegistry != nil {
		o.providerRegistry.Start(o.backgroundCtx)
	}
	o.configureSelfLearning()
	if o.router != nil {
		o.router.runtime = o.runtime
		o.router.AttachMemoryManager(o.memory)
	}
	if submissionModeEnabled() {
		o.StartSubmissionWorker(o.backgroundCtx, envInt("GO_CORE_SUBMIT_WORKERS", defaultSubmitWorkers(parallelism)))
		o.StartResultWorker(o.backgroundCtx, envInt("GO_CORE_RESULT_WORKERS", defaultResultWorkers(parallelism)))
		o.registry.OnAgentRegistered(o.ensureAgentWorkerPool)
		o.startAgentWorkerPools(o.backgroundCtx, envInt("GO_CORE_AGENT_WORKERS", defaultAgentWorkers(parallelism)))
	}
	o.publishInventorySnapshot(o.backgroundCtx)
	return o
}

func (o *Orchestrator) Close() {
	if o == nil || o.backgroundCancel == nil {
		return
	}
	o.backgroundCancel()
}

func (o *Orchestrator) ExecutionProfile() map[string]any {
	if o == nil {
		return map[string]any{}
	}
	return o.executionProfile.Metadata()
}

func defaultGlobalConcurrency(cpuCount int) int {
	if cpuCount < 1 {
		cpuCount = 1
	}
	limit := cpuCount * 2
	if limit < 8 {
		return 8
	}
	return limit
}

func defaultPerAgentConcurrency(cpuCount int) int {
	if cpuCount <= 2 {
		return 1
	}
	if cpuCount <= 8 {
		return 2
	}
	return 4
}

func defaultPerModelConcurrency(cpuCount int) int {
	if cpuCount <= 4 {
		return 1
	}
	return 2
}

func defaultSubmitWorkers(cpuCount int) int {
	if cpuCount <= 2 {
		return 1
	}
	if cpuCount <= 8 {
		return 2
	}
	return 4
}

func defaultResultWorkers(cpuCount int) int {
	if cpuCount <= 4 {
		return 1
	}
	return 2
}

func defaultAgentWorkers(cpuCount int) int {
	if cpuCount <= 2 {
		return 1
	}
	if cpuCount <= 8 {
		return 2
	}
	return 4
}

func agentPollInterval() time.Duration {
	ms := envInt("GO_CORE_AGENT_POLL_INTERVAL_MS", 500)
	if ms < 50 {
		ms = 50
	}
	return time.Duration(ms) * time.Millisecond
}

type scheduledSubmission struct {
	delivery delivery.TaskDelivery
	groupKey string
	enqueued time.Time
}

type submissionScheduler struct {
	mu                  sync.Mutex
	wake                chan struct{}
	space               chan struct{}
	queues              map[string][]scheduledSubmission
	order               []string
	inflight            map[string]int
	maxBuffered         int
	maxInFlightPerGroup int
	totalPending        int
	cursor              int
}

func newSubmissionScheduler(maxBuffered, maxInFlightPerGroup int) *submissionScheduler {
	if maxBuffered <= 0 {
		maxBuffered = 16
	}
	if maxInFlightPerGroup <= 0 {
		maxInFlightPerGroup = 1
	}
	return &submissionScheduler{
		wake:                make(chan struct{}, 1),
		space:               make(chan struct{}, 1),
		queues:              map[string][]scheduledSubmission{},
		inflight:            map[string]int{},
		maxBuffered:         maxBuffered,
		maxInFlightPerGroup: maxInFlightPerGroup,
	}
}

func (s *submissionScheduler) enqueue(ctx context.Context, item scheduledSubmission) error {
	for {
		s.mu.Lock()
		if s.totalPending < s.maxBuffered {
			if _, ok := s.queues[item.groupKey]; !ok {
				s.order = append(s.order, item.groupKey)
			}
			s.queues[item.groupKey] = append(s.queues[item.groupKey], item)
			s.totalPending++
			s.mu.Unlock()
			s.notifyWake()
			return nil
		}
		s.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.space:
		}
	}
}

func (s *submissionScheduler) next(ctx context.Context) (scheduledSubmission, bool, error) {
	for {
		s.mu.Lock()
		item, ok := s.popNextLocked()
		if ok {
			s.mu.Unlock()
			s.notifySpace()
			return item, true, nil
		}
		s.mu.Unlock()
		select {
		case <-ctx.Done():
			return scheduledSubmission{}, false, ctx.Err()
		case <-s.wake:
		}
	}
}

func (s *submissionScheduler) done(groupKey string) {
	s.mu.Lock()
	if s.inflight[groupKey] > 0 {
		s.inflight[groupKey]--
		if s.inflight[groupKey] == 0 {
			delete(s.inflight, groupKey)
		}
	}
	s.mu.Unlock()
	s.notifyWake()
}

func (s *submissionScheduler) popNextLocked() (scheduledSubmission, bool) {
	if len(s.order) == 0 {
		return scheduledSubmission{}, false
	}
	bestOrderIndex := -1
	bestScore := -1.0
	start := 0
	if len(s.order) > 0 {
		start = s.cursor % len(s.order)
	}
	for offset := 0; offset < len(s.order); offset++ {
		orderIndex := (start + offset) % len(s.order)
		groupKey := s.order[orderIndex]
		queue := s.queues[groupKey]
		if len(queue) == 0 {
			continue
		}
		if s.inflight[groupKey] >= s.maxInFlightPerGroup {
			continue
		}
		score := scoreScheduledTask(queue[0])
		if bestOrderIndex == -1 || score > bestScore {
			bestOrderIndex = orderIndex
			bestScore = score
		}
	}
	if bestOrderIndex == -1 {
		return scheduledSubmission{}, false
	}
	groupKey := s.order[bestOrderIndex]
	queue := s.queues[groupKey]
	item := queue[0]
	if len(queue) == 1 {
		delete(s.queues, groupKey)
		s.order = append(s.order[:bestOrderIndex], s.order[bestOrderIndex+1:]...)
		if len(s.order) == 0 {
			s.cursor = 0
		} else if bestOrderIndex >= len(s.order) {
			s.cursor = 0
		} else {
			s.cursor = bestOrderIndex
		}
	} else {
		s.queues[groupKey] = queue[1:]
		s.cursor = (bestOrderIndex + 1) % len(s.order)
	}
	s.totalPending--
	s.inflight[groupKey]++
	return item, true
}

func scoreScheduledTask(item scheduledSubmission) float64 {
	priorityWeight := map[domain.Priority]float64{
		domain.PriorityLow:      10,
		domain.PriorityNormal:   20,
		domain.PriorityHigh:     30,
		domain.PriorityCritical: 40,
	}
	complexityBoost := map[domain.Complexity]float64{
		domain.ComplexityLow:      6,
		domain.ComplexityMedium:   4,
		domain.ComplexityHigh:     2,
		domain.ComplexityCritical: 0,
	}
	task := item.delivery.Task
	score := priorityWeight[task.Priority] + complexityBoost[task.Complexity]
	if score == 0 {
		score = priorityWeight[domain.PriorityNormal] + complexityBoost[domain.ComplexityLow]
	}
	ageSeconds := time.Since(item.enqueued).Seconds()
	if ageSeconds > 0 {
		score += minFloat(ageSeconds/2, 25)
	}
	return score
}

func submissionGroupKey(task domain.Task) string {
	branchKey := submissionBranchKey(task)
	if task.SessionID != "" {
		if branchKey != "" {
			return fmt.Sprintf("%s::%s", task.SessionID, branchKey)
		}
		return task.SessionID
	}
	if branchKey != "" {
		return branchKey
	}
	if task.ParentTaskID != "" {
		return task.ParentTaskID
	}
	if task.ID != "" {
		return task.ID
	}
	return "default"
}

func submissionBranchKey(task domain.Task) string {
	for _, candidate := range []string{
		strings.TrimSpace(task.BranchID),
		routingHintString(task.RoutingHints, "cluster_id"),
		routingHintString(task.RoutingHints, "branch_id"),
		routingHintString(task.RoutingHints, "plan_step_id"),
		executionContractString(task.ExecutionContract, "cluster_id"),
		executionContractString(task.ExecutionContract, "branch_id"),
		executionContractString(task.ExecutionContract, "plan_step_id"),
	} {
		if candidate != "" {
			return candidate
		}
	}
	return ""
}

func routingHintString(hints map[string]any, key string) string {
	if len(hints) == 0 {
		return ""
	}
	value, ok := hints[key]
	if !ok || value == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(value))
}

func executionContractString(contract map[string]any, key string) string {
	if len(contract) == 0 {
		return ""
	}
	value, ok := contract[key]
	if !ok || value == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(value))
}

func submissionBufferSize(concurrency int) int {
	if concurrency <= 0 {
		concurrency = 1
	}
	return maxInt(concurrency*4, 16)
}

func submissionMaxInFlightPerGroup() int {
	return envInt("GO_CORE_MAX_INFLIGHT_PER_SESSION", 1)
}

func (s *submissionScheduler) notifyWake() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *submissionScheduler) notifySpace() {
	select {
	case s.space <- struct{}{}:
	default:
	}
}

func minFloat(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func (o *Orchestrator) SubmitTask(ctx context.Context, task domain.Task) (domain.WorkflowRecord, error) {
	if submissionModeEnabled() {
		return o.SubmitTaskAsync(ctx, task)
	}
	return o.submitTaskSync(ctx, task)
}

func (o *Orchestrator) submitTaskSync(ctx context.Context, task domain.Task) (domain.WorkflowRecord, error) {
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
			Status:      domain.TaskStatusRejected,
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
		o.publishRuntimeEvent("tasks", "task.rejected", task.ID, map[string]any{
			"task":       task,
			"acceptance": acceptance,
			"result":     result,
			"validation": guard,
		})
		o.publishInventorySnapshot(ctx)
		return record, nil
	}

	task, plan := o.planner.Prepare(task)
	task = o.applyAdaptivePolicy(ctx, task, plan)
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
	finalAcceptance, finalTask, result := o.executeTaskP2P(ctx, task, plan, acceptance, agent)
	record.Task = finalTask
	record.Acceptance = finalAcceptance
	record.Acceptance.Status = normalizeTaskStatus(result.Status)
	if record.Acceptance.Status == domain.TaskStatusCompleted && strings.TrimSpace(record.Acceptance.Reason) == "" {
		record.Acceptance.Reason = "execution completed"
	}
	record.Result = &result
	record.UpdatedAt = time.Now().UTC()
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		return domain.WorkflowRecord{}, err
	}
	kind := "task.completed"
	if record.Acceptance.Status == domain.TaskStatusFailed || record.Acceptance.Status == domain.TaskStatusDeadLettered {
		kind = "task.failed"
	}
	o.publishRuntimeEvent("tasks", kind, task.ID, map[string]any{
		"task":       finalTask,
		"plan":       plan,
		"acceptance": record.Acceptance,
		"result":     result,
	})
	o.publishInventorySnapshot(ctx)
	o.notifyWorkflowWaiters(record)
	return record, nil
}

func (o *Orchestrator) SubmitTaskAsync(ctx context.Context, task domain.Task) (domain.WorkflowRecord, error) {
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
	queue, ok := o.messageBus.(delivery.TaskSubmissionQueue)
	if !ok {
		return o.submitTaskSync(ctx, task)
	}
	complexity := task.Complexity
	if complexity == "" {
		complexity = domain.ComplexityLow
	}
	acceptance := domain.TaskAcceptance{
		TaskID:     task.ID,
		Status:     domain.TaskStatusQueued,
		Complexity: complexity,
		Reason:     "queued_for_async_submission",
		AcceptedAt: time.Now().UTC(),
	}
	record := domain.WorkflowRecord{Task: task, Acceptance: acceptance, UpdatedAt: time.Now().UTC()}
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		return domain.WorkflowRecord{}, err
	}
	if err := queue.EnqueueTask(ctx, task); err != nil {
		return domain.WorkflowRecord{}, err
	}
	o.publishRuntimeEvent("tasks", "task.queued", task.ID, map[string]any{
		"task":       task,
		"acceptance": acceptance,
		"queue":      delivery.SubmissionTopic,
	})
	o.publishInventorySnapshot(ctx)
	return record, nil
}

func (o *Orchestrator) StartSubmissionWorker(ctx context.Context, concurrency int) {
	if ctx == nil {
		ctx = context.Background()
	}
	if concurrency <= 0 {
		concurrency = 1
	}
	queue, ok := o.messageBus.(delivery.TaskSubmissionQueue)
	if !ok {
		return
	}
	o.schedulerMu.Lock()
	if o.submissionQueue != nil {
		o.schedulerMu.Unlock()
		return
	}
	scheduler := newSubmissionScheduler(submissionBufferSize(concurrency), submissionMaxInFlightPerGroup())
	o.submissionQueue = scheduler
	o.schedulerMu.Unlock()
	for workerID := 0; workerID < concurrency; workerID++ {
		go o.runSubmissionWorker(ctx, workerID, scheduler)
	}
	if stream, ok := o.messageBus.(delivery.TaskDeliveryStream); ok {
		tasks, err := stream.ConsumeTaskDeliveries(ctx, concurrency)
		if err != nil {
			return
		}
		go o.fillSubmissionScheduler(ctx, scheduler, tasks)
		return
	}
	plainTasks, err := queue.ConsumeTasks(ctx, concurrency)
	if err != nil {
		return
	}
	tasks := make(chan delivery.TaskDelivery)
	go o.forwardTaskDeliveries(ctx, plainTasks, tasks)
	go o.fillSubmissionScheduler(ctx, scheduler, tasks)
}

func (o *Orchestrator) fillSubmissionScheduler(ctx context.Context, scheduler *submissionScheduler, tasks <-chan delivery.TaskDelivery) {
	for {
		select {
		case <-ctx.Done():
			return
		case deliveryTask, ok := <-tasks:
			if !ok {
				return
			}
			item := scheduledSubmission{
				delivery: deliveryTask,
				groupKey: submissionGroupKey(deliveryTask.Task),
				enqueued: time.Now().UTC(),
			}
			if err := scheduler.enqueue(ctx, item); err != nil {
				if deliveryTask.Nack != nil {
					_ = deliveryTask.Nack(true)
				}
				return
			}
		}
	}
}

func (o *Orchestrator) forwardTaskDeliveries(ctx context.Context, plainTasks <-chan domain.Task, tasks chan<- delivery.TaskDelivery) {
	defer close(tasks)
	for {
		select {
		case <-ctx.Done():
			return
		case task, ok := <-plainTasks:
			if !ok {
				return
			}
			deliveryTask := delivery.TaskDelivery{Task: task, Ack: func() error { return nil }, Nack: func(bool) error { return nil }}
			select {
			case <-ctx.Done():
				return
			case tasks <- deliveryTask:
			}
		}
	}
}

func (o *Orchestrator) forwardResultDeliveries(ctx context.Context, plainResults <-chan domain.TaskResultEnvelope, results chan<- delivery.ResultDelivery) {
	defer close(results)
	for {
		select {
		case <-ctx.Done():
			return
		case result, ok := <-plainResults:
			if !ok {
				return
			}
			deliveryResult := delivery.ResultDelivery{Result: result, Ack: func() error { return nil }, Nack: func(bool) error { return nil }}
			select {
			case <-ctx.Done():
				return
			case results <- deliveryResult:
			}
		}
	}
}

func (o *Orchestrator) runSubmissionWorker(ctx context.Context, workerID int, scheduler *submissionScheduler) {
	for {
		item, ok, err := scheduler.next(ctx)
		if err != nil || !ok {
			return
		}
		task := item.delivery.Task
		o.publishRuntimeEvent("tasks", "task.dequeued", task.ID, map[string]any{
			"task":       task,
			"worker_id":  workerID,
			"queue":      delivery.SubmissionTopic,
			"group_key":  item.groupKey,
			"fair_queue": true,
		})
		if _, err := o.dispatchTaskAsync(ctx, task); err != nil {
			o.publishRuntimeEvent("tasks", "task.failed", task.ID, map[string]any{
				"task":      task,
				"worker_id": workerID,
				"error":     err.Error(),
			})
			if item.delivery.Nack != nil {
				_ = item.delivery.Nack(true)
			}
			scheduler.done(item.groupKey)
			continue
		}
		if item.delivery.Ack != nil {
			_ = item.delivery.Ack()
		}
		scheduler.done(item.groupKey)
	}
}

func submissionModeEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv("GO_CORE_SUBMIT_MODE")), "async")
}

func (o *Orchestrator) applyAdaptivePolicy(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) domain.Task {
	if o == nil || o.adaptive == nil {
		return task
	}
	adapted, _ := o.adaptive.Apply(ctx, task, plan)
	return adapted
}

func (o *Orchestrator) executeTaskP2P(ctx context.Context, task domain.Task, plan domain.ExecutionPlan, acceptance domain.TaskAcceptance, agent agents.Agent) (domain.TaskAcceptance, domain.Task, domain.AgentResult) {
	excluded := map[string]struct{}{}
	maxAttempts := maxInt(1, len(o.registry.Agents()))
	currentTask := task
	currentAcceptance := acceptance
	currentAgent := agent
	attempt := 0
	for {
		attempt++
		agentInfo := currentAgent.Info()
		currentTask = o.annotatePeerAttempt(currentTask, currentAcceptance, attempt, excluded)
		envelope := o.buildPeerEnvelope(currentTask, currentAcceptance, plan, agentInfo, attempt)
		deliveryState, deliveryErr := o.preparePeerDelivery(ctx, envelope, agentInfo.ID)
		if deliveryErr != nil {
			result := o.failedAttemptResult(currentTask, currentAcceptance, agentInfo, deliveryErr.Error())
			o.persistExecutionState(ctx, currentTask, currentAcceptance, agentInfo, result, 0)
			if fallbackAcceptance, fallbackAgent, ok := o.routePeerFallback(currentTask, plan, excluded, agentInfo.ID); ok && attempt < maxAttempts {
				excluded[agentInfo.ID] = struct{}{}
				currentAcceptance = fallbackAcceptance
				currentAgent = fallbackAgent
				currentTask = o.rebindTaskAssignment(o.withFailureContext(currentTask, agentInfo, result, attempt, 0), currentAcceptance)
				currentTask = o.attachRuntimeContext(ctx, currentTask, currentAcceptance, currentAgent.Info())
				continue
			}
			return currentAcceptance, currentTask, result
		}
		o.publishRuntimeEvent("tasks", "task.running", currentTask.ID, map[string]any{
			"task":           currentTask,
			"agent_id":       agentInfo.ID,
			"plan":           plan,
			"attempt":        attempt,
			"delivery_state": deliveryState,
		})
		result, latency := o.executeDeliveredTask(ctx, currentTask, currentAcceptance, currentAgent, envelope)
		if result.Status != domain.TaskStatusFailed {
			return currentAcceptance, currentTask, result
		}
		if fallbackAcceptance, fallbackAgent, ok := o.routePeerFallback(currentTask, plan, excluded, agentInfo.ID); ok && attempt < maxAttempts {
			excluded[agentInfo.ID] = struct{}{}
			currentAcceptance = fallbackAcceptance
			currentAgent = fallbackAgent
			currentTask = o.rebindTaskAssignment(o.withFailureContext(currentTask, agentInfo, result, attempt, latency), currentAcceptance)
			currentTask = o.attachRuntimeContext(ctx, currentTask, currentAcceptance, currentAgent.Info())
			continue
		}
		return currentAcceptance, currentTask, result
	}
}

func (o *Orchestrator) annotatePeerAttempt(task domain.Task, acceptance domain.TaskAcceptance, attempt int, excluded map[string]struct{}) domain.Task {
	hints := cloneMap(task.RoutingHints)
	hints["peer_to_peer"] = true
	hints["p2p_attempt"] = attempt
	hints["p2p_agent_id"] = acceptance.AgentID
	hints["p2p_provider"] = acceptance.Provider
	hints["p2p_model_name"] = acceptance.ModelName
	if len(excluded) > 0 {
		tried := make([]string, 0, len(excluded))
		for agentID := range excluded {
			tried = append(tried, agentID)
		}
		hints["p2p_excluded_agents"] = tried
	}
	task.RoutingHints = hints
	return task
}

func (o *Orchestrator) withFailureContext(task domain.Task, agent domain.AgentInfo, result domain.AgentResult, attempt int, latency time.Duration) domain.Task {
	hints := cloneMap(task.RoutingHints)
	rawFailures, _ := hints["peer_failures"].([]map[string]any)
	failures := append([]map[string]any(nil), rawFailures...)
	failures = append(failures, map[string]any{
		"attempt":      attempt,
		"agent_id":     agent.ID,
		"provider":     agent.Provider,
		"model_name":   agent.ModelName,
		"status":       result.Status,
		"summary":      result.Output.Summary,
		"errors":       append([]string(nil), result.Errors...),
		"latency_ms":   latency.Milliseconds(),
		"completed_at": result.CompletedAt,
	})
	hints["peer_failures"] = failures
	task.RoutingHints = hints
	return task
}

func (o *Orchestrator) buildPeerEnvelope(task domain.Task, acceptance domain.TaskAcceptance, plan domain.ExecutionPlan, agent domain.AgentInfo, attempt int) domain.TaskEnvelope {
	traceID := firstNonEmptyString(kernelString(task.RoutingHints["trace_id"]), task.ID)
	return domain.TaskEnvelope{
		ProtocolVersion:  "go-core/p2p.v1",
		TaskID:           task.ID,
		ParentTaskID:     task.ParentTaskID,
		TraceID:          traceID,
		CorrelationID:    firstNonEmptyString(task.SessionID, task.ID),
		SourceAgent:      "orchestrator",
		TargetAgent:      agent.ID,
		TargetCapability: firstNonEmptyString(acceptance.Capability, plan.PrimaryCapability, task.RequiredCapability, string(task.Type)),
		Priority:         string(task.Priority),
		ContextScope:     defaultBranch(task.Context.Branch),
		MaxHops:          maxInt(1, len(o.registry.Agents())),
		RetryCount:       maxInt(0, attempt-1),
		MaxRetries:       maxInt(0, len(o.registry.Agents())-1),
		Payload: domain.TaskPayload{
			Objective: task.Input.Description,
			InputData: map[string]any{
				"task":          task,
				"routing_hints": cloneMap(task.RoutingHints),
			},
			Context:              map[string]any{"project": task.Context.Project, "repo_path": task.Context.RepoPath, "branch": task.Context.Branch},
			AcceptanceCriteria:   append([]string(nil), task.Input.AcceptanceCriteria...),
			ExpectedOutputFormat: "domain.AgentResult",
			Artifacts:            append([]string(nil), task.Input.Files...),
		},
		CreatedAt: time.Now().UTC(),
	}
}

func (o *Orchestrator) preparePeerDelivery(ctx context.Context, envelope domain.TaskEnvelope, agentID string) (map[string]any, error) {
	deliveryState := o.DispatchEnvelope(ctx, envelope)
	payloads := o.FetchAgentMailbox(ctx, agentID, 1)
	if len(payloads) == 0 {
		ack := o.AckDelivery(ctx, envelope.TaskID, domain.AckStatusFailed, agentID, "empty_mailbox")
		return deliveryState, errors.New(firstNonEmptyString(ack.Reason, "delivery mailbox was empty"))
	}
	if !o.ConfirmDeliveryPayload(ctx, envelope.TaskID, agentID, payloads[0]) {
		ack := o.AckDelivery(ctx, envelope.TaskID, domain.AckStatusFailed, agentID, "payload_validation_failed")
		return deliveryState, errors.New(firstNonEmptyString(ack.Reason, "delivery payload validation failed"))
	}
	handshake := o.EstablishDeliveryHandshake(ctx, envelope.TaskID, agentID)
	if handshake.AckStatus == domain.AckStatusFailed {
		return deliveryState, errors.New(firstNonEmptyString(handshake.Reason, "delivery handshake failed"))
	}
	o.AckDelivery(ctx, envelope.TaskID, domain.AckStatusAccepted, agentID, "execution_started")
	return o.RefreshDelivery(ctx, envelope.TaskID), nil
}

func (o *Orchestrator) StartResultWorker(ctx context.Context, concurrency int) {
	if ctx == nil {
		ctx = context.Background()
	}
	if concurrency <= 0 {
		return
	}
	queue, ok := o.messageBus.(delivery.TaskResultQueue)
	if !ok {
		return
	}
	if stream, ok := o.messageBus.(delivery.ResultDeliveryStream); ok {
		results, err := stream.ConsumeResultDeliveries(ctx, concurrency)
		if err != nil {
			return
		}
		for workerID := 0; workerID < concurrency; workerID++ {
			go o.runResultWorker(ctx, workerID, results)
		}
		return
	}
	plainResults, err := queue.ConsumeResults(ctx, concurrency)
	if err != nil {
		return
	}
	results := make(chan delivery.ResultDelivery)
	for workerID := 0; workerID < concurrency; workerID++ {
		go o.runResultWorker(ctx, workerID, results)
	}
	go o.forwardResultDeliveries(ctx, plainResults, results)
}

func (o *Orchestrator) startAgentWorkerPools(ctx context.Context, concurrency int) {
	if concurrency <= 0 {
		return
	}
	for _, info := range o.registry.AgentInfos() {
		agent, ok := o.registry.AgentByID(info.ID)
		if !ok {
			continue
		}
		o.ensureAgentWorkerPoolWithConcurrency(ctx, agent, concurrency)
	}
}

func (o *Orchestrator) ensureAgentWorkerPool(agent agents.Agent) {
	if o == nil {
		return
	}
	o.ensureAgentWorkerPoolWithConcurrency(o.backgroundCtx, agent, envInt("GO_CORE_AGENT_WORKERS", defaultAgentWorkers(o.executionProfile.UsableParallelism)))
}

func (o *Orchestrator) ensureAgentWorkerPoolWithConcurrency(ctx context.Context, agent agents.Agent, concurrency int) {
	if o == nil || agent == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if concurrency <= 0 {
		return
	}
	agentID := agent.Info().ID
	o.workerPoolsMu.Lock()
	for _, pool := range o.workerPools {
		if pool != nil {
			snapshot := pool.Snapshot()
			if snapshot["agent_id"] == agentID {
				o.workerPoolsMu.Unlock()
				return
			}
		}
	}
	pool := delivery.NewWorkerPool(o.delivery, agentID, concurrency, agentPollInterval(), func(workerCtx context.Context, envelope domain.TaskEnvelope) error {
		return o.handleAgentEnvelope(workerCtx, agent, envelope)
	}, func(workerCtx context.Context, envelope domain.TaskEnvelope, reason string) error {
		return o.publishEnvelopeDeadLetter(workerCtx, envelope, reason)
	})
	o.workerPools = append(o.workerPools, pool)
	o.workerPoolsMu.Unlock()
	go pool.Start(ctx)
}

func (o *Orchestrator) runResultWorker(ctx context.Context, workerID int, results <-chan delivery.ResultDelivery) {
	for {
		select {
		case <-ctx.Done():
			return
		case deliveryResult, ok := <-results:
			if !ok {
				return
			}
			result := deliveryResult.Result
			o.publishRuntimeEvent("tasks", "task.result_received", result.TaskID, map[string]any{
				"task_id":   result.TaskID,
				"worker_id": workerID,
				"status":    result.Status,
				"agent_id":  result.TargetAgent,
			})
			if err := o.handleTaskResult(ctx, result); err != nil {
				o.publishRuntimeEvent("tasks", "task.result_failed", result.TaskID, map[string]any{
					"task_id":   result.TaskID,
					"worker_id": workerID,
					"error":     err.Error(),
				})
				if deliveryResult.Nack != nil {
					_ = deliveryResult.Nack(true)
				}
				continue
			}
			if deliveryResult.Ack != nil {
				_ = deliveryResult.Ack()
			}
		}
	}
}

func (o *Orchestrator) dispatchTaskAsync(ctx context.Context, task domain.Task) (domain.WorkflowRecord, error) {
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
		return domain.WorkflowRecord{}, errors.New("task type is required")
	}

	record := domain.WorkflowRecord{Task: task, UpdatedAt: time.Now().UTC()}
	if decision := preflightPolicy(task); decisionBlocks(decision) {
		record.Acceptance = domain.TaskAcceptance{TaskID: task.ID, Status: domain.TaskStatusRejected, Complexity: domain.ComplexityLow, Reason: firstNonEmptyString(strings.Join(decision.Reasons, "; "), "task rejected by policy"), AcceptedAt: time.Now().UTC()}
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.notifyWorkflowWaiters(record)
		return record, nil
	}

	guard := o.memory.CacheGuardSnapshot(ctx, task.SessionID, task.Context.Branch)
	if blocked, _ := guard["blocked"].(bool); blocked {
		reason := firstNonEmptyString(kernelString(guard["reason"]), "cache guard blocked task execution")
		record.Acceptance = domain.TaskAcceptance{TaskID: task.ID, Status: domain.TaskStatusRejected, Complexity: domain.ComplexityLow, Reason: reason, AcceptedAt: time.Now().UTC()}
		record.Result = &domain.AgentResult{TaskID: task.ID, Status: domain.TaskStatusRejected, Errors: []string{reason}, CompletedAt: time.Now().UTC(), Output: domain.ResultOutput{Summary: reason, Artifacts: map[string]any{"cache_guard": guard}}}
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.notifyWorkflowWaiters(record)
		return record, nil
	}

	task, plan := o.planner.Prepare(task)
	task = o.applyAdaptivePolicy(ctx, task, plan)
	acceptance, agent, ok := o.router.Route(task, plan)
	record.Task = task
	record.Plan = plan
	record.Acceptance = acceptance
	if !ok {
		record.Acceptance.Status = domain.TaskStatusRejected
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.notifyWorkflowWaiters(record)
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
			o.notifyWorkflowWaiters(record)
			return record, nil
		}
	}

	task, acceptance, agent, budgetFailure := o.enforceModelBudgetPolicy(ctx, task, plan, acceptance, agent)
	record.Task = task
	record.Acceptance = acceptance
	if budgetFailure != nil {
		record.Result = budgetFailure
		record.Acceptance.Status = normalizeTaskStatus(budgetFailure.Status)
		record.UpdatedAt = time.Now().UTC()
		if err := o.store.SaveWorkflow(ctx, record); err != nil {
			return domain.WorkflowRecord{}, err
		}
		o.notifyWorkflowWaiters(record)
		return record, nil
	}

	task = o.attachRuntimeContext(ctx, task, acceptance, agent.Info())
	acceptance.Status = domain.TaskStatusRunning
	record.Task = task
	record.Acceptance = acceptance
	record.UpdatedAt = time.Now().UTC()
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		return domain.WorkflowRecord{}, err
	}
	attempt := routingHintInt(task, "p2p_attempt", 1)
	envelope := o.buildPeerEnvelope(task, acceptance, plan, agent.Info(), attempt)
	o.DispatchEnvelope(ctx, envelope)
	o.publishRuntimeEvent("tasks", "task.running", task.ID, map[string]any{
		"task":       task,
		"plan":       plan,
		"acceptance": acceptance,
		"agent_id":   agent.Info().ID,
	})
	o.publishInventorySnapshot(ctx)
	return record, nil
}

func (o *Orchestrator) handleAgentEnvelope(ctx context.Context, agent agents.Agent, envelope domain.TaskEnvelope) error {
	task, err := decodeTaskFromEnvelope(envelope)
	if err != nil {
		return o.publishEnvelopeFailure(ctx, envelope, agent.Info(), err.Error())
	}
	workflow, ok, loadErr := o.store.GetWorkflow(ctx, task.ID)
	acceptance := workflow.Acceptance
	if loadErr != nil || !ok || acceptance.TaskID == "" {
		acceptance = domain.TaskAcceptance{
			TaskID:     task.ID,
			AgentID:    envelope.TargetAgent,
			Provider:   task.AssignedProvider,
			ModelName:  task.AssignedModel,
			Capability: task.RequiredCapability,
			Status:     domain.TaskStatusRunning,
			AcceptedAt: time.Now().UTC(),
			Complexity: task.Complexity,
		}
	}
	if acceptance.AgentID == "" {
		acceptance.AgentID = agent.Info().ID
	}
	release, acquireErr := o.acquireExecutionSlots(ctx, agent.Info(), acceptance.ModelName)
	if acquireErr != nil {
		return o.publishEnvelopeFailure(ctx, envelope, agent.Info(), acquireErr.Error())
	}
	defer release()

	task, result, latency := o.executeAgentWithReasoning(ctx, task, acceptance, agent)
	result.TaskID = task.ID
	result.AgentID = agent.Info().ID
	result.Provider = firstNonEmptyString(result.Provider, acceptance.Provider)
	result.ModelName = firstNonEmptyString(result.ModelName, acceptance.ModelName)
	if result.CompletedAt.IsZero() {
		result.CompletedAt = time.Now().UTC()
	}
	_ = o.memory.RecordModelUsage(ctx, task, acceptance.ModelName, result)
	if result.Status == domain.TaskStatusFailed {
		_ = o.memory.RecordHardStop(ctx, task, "agent_execute", result.Output.Summary)
		if o.runtime != nil {
			state := o.runtime.RecordRuntimeFailure(agent.Info().ID, result.Output.Summary)
			action := o.runtime.RecoveryActionForFailure(agent.Info().ID)
			if action == "quarantine_agent" {
				state = o.runtime.QuarantineAgent(agent.Info().ID, result.Output.Summary)
			}
			o.publishRuntimeEvent("agents", "agent.runtime_failure", agent.Info().ID, map[string]any{"agent_id": agent.Info().ID, "task_id": task.ID, "action": action, "state": state})
		}
	} else if o.runtime != nil {
		o.runtime.RecordSuccess(agent.Info().ID)
	}
	o.persistExecutionState(ctx, task, acceptance, agent.Info(), result, latency)
	_ = o.memory.RecordPeerExchange(ctx, envelope, acceptance, &result, result.Output.Summary)
	queue, ok := o.messageBus.(delivery.TaskResultQueue)
	if !ok {
		return errors.New("result queue is unavailable")
	}
	return queue.PublishResult(ctx, buildTaskResultEnvelope(task, envelope, acceptance, result))
}

func (o *Orchestrator) publishEnvelopeFailure(ctx context.Context, envelope domain.TaskEnvelope, agent domain.AgentInfo, reason string) error {
	queue, ok := o.messageBus.(delivery.TaskResultQueue)
	if !ok {
		return errors.New("result queue is unavailable")
	}
	task, err := decodeTaskFromEnvelope(envelope)
	if err != nil {
		task = domain.Task{ID: envelope.TaskID, ParentTaskID: envelope.ParentTaskID, SessionID: firstNonEmptyString(envelope.CorrelationID, envelope.TaskID)}
	}
	acceptance := domain.TaskAcceptance{TaskID: task.ID, AgentID: agent.ID, Provider: agent.Provider, ModelName: agent.ModelName, Status: domain.TaskStatusFailed, AcceptedAt: time.Now().UTC()}
	result := o.failedAttemptResult(task, acceptance, agent, reason)
	return queue.PublishResult(ctx, buildTaskResultEnvelope(task, envelope, acceptance, result))
}

func (o *Orchestrator) publishEnvelopeDeadLetter(ctx context.Context, envelope domain.TaskEnvelope, reason string) error {
	queue, ok := o.messageBus.(delivery.TaskResultQueue)
	if !ok {
		return errors.New("result queue is unavailable")
	}
	task, err := decodeTaskFromEnvelope(envelope)
	if err != nil {
		task = domain.Task{ID: envelope.TaskID, ParentTaskID: envelope.ParentTaskID, SessionID: firstNonEmptyString(envelope.CorrelationID, envelope.TaskID)}
	}
	acceptance := domain.TaskAcceptance{
		TaskID:     task.ID,
		AgentID:    envelope.TargetAgent,
		Status:     domain.TaskStatusDeadLettered,
		AcceptedAt: time.Now().UTC(),
	}
	if workflow, ok, loadErr := o.store.GetWorkflow(ctx, task.ID); loadErr == nil && ok {
		if workflow.Acceptance.TaskID != "" {
			acceptance = workflow.Acceptance
		}
	}
	acceptance.AgentID = firstNonEmptyString(acceptance.AgentID, envelope.TargetAgent)
	acceptance.Status = domain.TaskStatusDeadLettered
	result := domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     acceptance.AgentID,
		Status:      domain.TaskStatusDeadLettered,
		Errors:      []string{reason},
		Provider:    acceptance.Provider,
		ModelName:   acceptance.ModelName,
		CompletedAt: time.Now().UTC(),
		Output:      domain.ResultOutput{Summary: reason},
	}
	return queue.PublishResult(ctx, buildTaskResultEnvelope(task, envelope, acceptance, result))
}

func (o *Orchestrator) handleTaskResult(ctx context.Context, result domain.TaskResultEnvelope) error {
	workflow, ok, err := o.store.GetWorkflow(ctx, result.TaskID)
	if err != nil {
		return err
	}
	if !ok {
		workflow = domain.WorkflowRecord{Task: domain.Task{ID: result.TaskID, ParentTaskID: result.ParentTaskID, SessionID: firstNonEmptyString(result.CorrelationID, result.TaskID)}}
	}
	workflow.Result = &result.Result
	workflow.UpdatedAt = time.Now().UTC()
	if workflow.Acceptance.TaskID == "" {
		workflow.Acceptance = domain.TaskAcceptance{TaskID: result.TaskID, AgentID: result.TargetAgent, Status: normalizeTaskStatus(result.Status), AcceptedAt: time.Now().UTC()}
	} else {
		workflow.Acceptance.Status = normalizeTaskStatus(result.Status)
	}

	if result.Status == domain.TaskStatusFailed {
		excluded := excludedAgentsFromTask(workflow.Task)
		failedAgentID := firstNonEmptyString(result.Result.AgentID, result.TargetAgent, workflow.Acceptance.AgentID)
		if failedAgentID != "" {
			excluded[failedAgentID] = struct{}{}
		}
		if fallbackAcceptance, fallbackAgent, reroute := o.routePeerFallback(workflow.Task, workflow.Plan, excluded, failedAgentID); reroute {
			attempt := routingHintInt(workflow.Task, "p2p_attempt", 1) + 1
			workflow.Task = o.withFailureContext(workflow.Task, domain.AgentInfo{ID: failedAgentID, Provider: result.Result.Provider, ModelName: result.Result.ModelName}, result.Result, attempt-1, 0)
			workflow.Task = o.rebindTaskAssignment(workflow.Task, fallbackAcceptance)
			workflow.Task = o.annotatePeerAttempt(workflow.Task, fallbackAcceptance, attempt, excluded)
			workflow.Task = o.attachRuntimeContext(ctx, workflow.Task, fallbackAcceptance, fallbackAgent.Info())
			workflow.Acceptance = fallbackAcceptance
			workflow.Acceptance.Status = domain.TaskStatusRunning
			workflow.Result = nil
			workflow.UpdatedAt = time.Now().UTC()
			if err := o.store.SaveWorkflow(ctx, workflow); err != nil {
				return err
			}
			envelope := o.buildPeerEnvelope(workflow.Task, workflow.Acceptance, workflow.Plan, fallbackAgent.Info(), attempt)
			o.DispatchEnvelope(ctx, envelope)
			o.publishRuntimeEvent("tasks", "task.rerouted", workflow.Task.ID, map[string]any{
				"task":              workflow.Task,
				"acceptance":        workflow.Acceptance,
				"previous_agent_id": failedAgentID,
				"next_agent_id":     fallbackAgent.Info().ID,
				"attempt":           attempt,
			})
			return nil
		}
	}

	if result.Status == domain.TaskStatusQueued || result.Status == domain.TaskStatusRunning {
		if err := o.store.SaveWorkflow(ctx, workflow); err != nil {
			return err
		}
		return nil
	}
	if err := o.store.SaveWorkflow(ctx, workflow); err != nil {
		return err
	}
	kind := "task.completed"
	if result.Status == domain.TaskStatusFailed || result.Status == domain.TaskStatusDeadLettered {
		kind = "task.failed"
	}
	o.publishRuntimeEvent("tasks", kind, workflow.Task.ID, map[string]any{
		"task":       workflow.Task,
		"plan":       workflow.Plan,
		"acceptance": workflow.Acceptance,
		"result":     workflow.Result,
	})
	o.publishInventorySnapshot(ctx)
	o.notifyWorkflowWaiters(workflow)
	return nil
}

func (o *Orchestrator) WaitWorkflowTerminal(ctx context.Context, workflowID string) (domain.WorkflowRecord, error) {
	if workflowID == "" {
		return domain.WorkflowRecord{}, errors.New("workflow id is required")
	}
	record, ok, err := o.store.GetWorkflow(ctx, workflowID)
	if err != nil {
		return domain.WorkflowRecord{}, err
	}
	if ok && isTerminalTaskStatus(record.Acceptance.Status) {
		return record, nil
	}
	ch := make(chan domain.WorkflowRecord, 1)
	o.resultMu.Lock()
	o.resultWaiters[workflowID] = append(o.resultWaiters[workflowID], ch)
	o.resultMu.Unlock()
	defer o.removeWorkflowWaiter(workflowID, ch)
	for {
		select {
		case <-ctx.Done():
			return domain.WorkflowRecord{}, ctx.Err()
		case record := <-ch:
			if isTerminalTaskStatus(record.Acceptance.Status) {
				return record, nil
			}
		}
	}
}

func (o *Orchestrator) notifyWorkflowWaiters(record domain.WorkflowRecord) {
	o.resultMu.Lock()
	waiters := append([]chan domain.WorkflowRecord(nil), o.resultWaiters[record.Task.ID]...)
	if isTerminalTaskStatus(record.Acceptance.Status) {
		delete(o.resultWaiters, record.Task.ID)
	}
	o.resultMu.Unlock()
	for _, waiter := range waiters {
		select {
		case waiter <- record:
		default:
		}
	}
}

func (o *Orchestrator) removeWorkflowWaiter(workflowID string, ch chan domain.WorkflowRecord) {
	o.resultMu.Lock()
	defer o.resultMu.Unlock()
	waiters := o.resultWaiters[workflowID]
	for i, waiter := range waiters {
		if waiter == ch {
			o.resultWaiters[workflowID] = append(waiters[:i], waiters[i+1:]...)
			break
		}
	}
	if len(o.resultWaiters[workflowID]) == 0 {
		delete(o.resultWaiters, workflowID)
	}
}

func isTerminalTaskStatus(status domain.TaskStatus) bool {
	status = normalizeTaskStatus(status)
	return status == domain.TaskStatusCompleted || status == domain.TaskStatusFailed || status == domain.TaskStatusRejected || status == domain.TaskStatusDeadLettered
}

func normalizeTaskStatus(status domain.TaskStatus) domain.TaskStatus {
	switch status {
	case "", domain.TaskStatusDone:
		return domain.TaskStatusCompleted
	default:
		return status
	}
}

func decodeTaskFromEnvelope(envelope domain.TaskEnvelope) (domain.Task, error) {
	rawTask, ok := envelope.Payload.InputData["task"]
	if !ok {
		return domain.Task{}, errors.New("task payload is missing")
	}
	data, err := json.Marshal(rawTask)
	if err != nil {
		return domain.Task{}, err
	}
	var task domain.Task
	if err := json.Unmarshal(data, &task); err != nil {
		return domain.Task{}, err
	}
	if task.ID == "" {
		task.ID = envelope.TaskID
	}
	if task.ParentTaskID == "" {
		task.ParentTaskID = envelope.ParentTaskID
	}
	if task.SessionID == "" {
		task.SessionID = firstNonEmptyString(envelope.CorrelationID, task.ID)
	}
	return task, nil
}

func buildTaskResultEnvelope(task domain.Task, envelope domain.TaskEnvelope, acceptance domain.TaskAcceptance, result domain.AgentResult) domain.TaskResultEnvelope {
	return domain.TaskResultEnvelope{
		TaskID:        task.ID,
		ParentTaskID:  task.ParentTaskID,
		TraceID:       envelope.TraceID,
		CorrelationID: firstNonEmptyString(task.SessionID, envelope.CorrelationID, task.ID),
		SourceAgent:   envelope.SourceAgent,
		TargetAgent:   firstNonEmptyString(result.AgentID, acceptance.AgentID, envelope.TargetAgent),
		Status:        normalizeTaskStatus(result.Status),
		Result:        result,
		CreatedAt:     time.Now().UTC(),
	}
}

func excludedAgentsFromTask(task domain.Task) map[string]struct{} {
	excluded := map[string]struct{}{}
	raw, ok := task.RoutingHints["p2p_excluded_agents"]
	if !ok {
		return excluded
	}
	switch typed := raw.(type) {
	case []string:
		for _, agentID := range typed {
			excluded[agentID] = struct{}{}
		}
	case []any:
		for _, item := range typed {
			if agentID, ok := item.(string); ok && agentID != "" {
				excluded[agentID] = struct{}{}
			}
		}
	}
	return excluded
}

func routingHintInt(task domain.Task, key string, fallback int) int {
	value, ok := task.RoutingHints[key]
	if !ok {
		return fallback
	}
	switch typed := value.(type) {
	case int:
		return typed
	case int32:
		return int(typed)
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	default:
		return fallback
	}
}

func (o *Orchestrator) executeDeliveredTask(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent agents.Agent, envelope domain.TaskEnvelope) (domain.AgentResult, time.Duration) {
	agentInfo := agent.Info()
	releaseSlots, acquireErr := o.acquireExecutionSlots(ctx, agentInfo, acceptance.ModelName)
	if acquireErr != nil {
		result := o.failedAttemptResult(task, acceptance, agentInfo, acquireErr.Error())
		_ = o.memory.RecordPeerExchange(ctx, envelope, acceptance, &result, acquireErr.Error())
		return result, 0
	}
	defer releaseSlots()
	task, result, latency := o.executeAgentWithReasoning(ctx, task, acceptance, agent)
	_ = o.memory.RecordModelUsage(ctx, task, acceptance.ModelName, result)
	if result.Status == "" {
		result.Status = domain.TaskStatusDone
	}
	if result.Status == domain.TaskStatusFailed {
		_ = o.memory.RecordHardStop(ctx, task, "agent_execute", result.Output.Summary)
		if o.runtime != nil {
			state := o.runtime.RecordRuntimeFailure(agentInfo.ID, result.Output.Summary)
			action := o.runtime.RecoveryActionForFailure(agentInfo.ID)
			if action == "quarantine_agent" {
				state = o.runtime.QuarantineAgent(agentInfo.ID, result.Output.Summary)
			}
			o.publishRuntimeEvent("agents", "agent.runtime_failure", agentInfo.ID, map[string]any{
				"agent_id": agentInfo.ID,
				"task_id":  task.ID,
				"action":   action,
				"state":    state,
			})
		}
		o.AckDelivery(ctx, envelope.TaskID, domain.AckStatusFailed, agentInfo.ID, result.Output.Summary)
	} else {
		if o.runtime != nil {
			o.runtime.RecordSuccess(agentInfo.ID)
		}
		o.AckDelivery(ctx, envelope.TaskID, domain.AckStatusAccepted, agentInfo.ID, "execution_completed")
	}
	o.persistExecutionState(ctx, task, acceptance, agentInfo, result, latency)
	_ = o.memory.RecordPeerExchange(ctx, envelope, acceptance, &result, result.Output.Summary)
	return result, latency
}

func (o *Orchestrator) routePeerFallback(task domain.Task, plan domain.ExecutionPlan, excluded map[string]struct{}, failedAgentID string) (domain.TaskAcceptance, agents.Agent, bool) {
	nextExcluded := map[string]struct{}{failedAgentID: {}}
	for agentID := range excluded {
		nextExcluded[agentID] = struct{}{}
	}
	return o.router.RouteExcluding(task, plan, nextExcluded)
}

func (o *Orchestrator) failedAttemptResult(task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, reason string) domain.AgentResult {
	return domain.AgentResult{
		TaskID:      task.ID,
		AgentID:     agent.ID,
		Status:      domain.TaskStatusFailed,
		Errors:      []string{reason},
		Provider:    acceptance.Provider,
		ModelName:   acceptance.ModelName,
		CompletedAt: time.Now().UTC(),
		Output:      domain.ResultOutput{Summary: reason},
	}
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
	now := time.Now().UTC()
	for _, agent := range o.registry.Agents() {
		info := agent.Info()
		if _, exists := result[info.Provider]; exists {
			continue
		}
		configured := info.Status != domain.AgentStatusOffline
		status := "configured"
		available := false
		if !configured {
			status = "not_configured"
		} else if strings.EqualFold(strings.TrimSpace(info.Provider), "kernel") {
			status = "ready"
			available = true
		}
		result[info.Provider] = domain.ProviderHealth{
			Provider:   info.Provider,
			Configured: configured,
			Available:  available,
			Status:     status,
			ObservedAt: now,
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
	for provider, fallback := range result {
		reporter := reporters[provider]
		if o.probeManager != nil {
			result[provider] = o.probeManager.Observe(provider, fallback, reporter, probe)
			continue
		}
		if probe && reporter != nil {
			probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
			health := reporter.Probe(probeCtx)
			cancel()
			current, exists := result[provider]
			if !exists || health.Available || current.Status == "configured" {
				result[provider] = health
			}
		}
	}
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
	providerHealth := o.ProviderHealth(ctx, false)
	if o.runtime != nil {
		o.runtime.SyncProviderHealth(providerHealth)
	}
	return map[string]any{
		"status":            "ready",
		"agent_count":       len(o.registry.AgentInfos()),
		"module_count":      len(o.registry.ModuleInfos()),
		"workflow_count":    len(workflows),
		"modules":           o.ModuleState(),
		"agents":            o.registry.AgentInfos(),
		"providers":         providerCatalogs,
		"provider_health":   providerHealth,
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
	o.publishInventorySnapshot(o.backgroundCtx)
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
		o.publishInventorySnapshot(o.backgroundCtx)
	}
	return state, ok
}

func (o *Orchestrator) RecoverLane(agentID string) (domain.AgentRuntimeState, bool) {
	if o.runtime == nil {
		return domain.AgentRuntimeState{}, false
	}
	state, ok := o.runtime.RecoverLane(agentID)
	if ok {
		o.publishInventorySnapshot(o.backgroundCtx)
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

func (o *Orchestrator) rebindTaskAssignment(task domain.Task, acceptance domain.TaskAcceptance) domain.Task {
	task.AssignedProvider = acceptance.Provider
	task.AssignedModel = acceptance.ModelName
	hints := cloneMap(task.RoutingHints)
	hints["selected_provider"] = acceptance.Provider
	hints["selected_model"] = acceptance.ModelName
	task.RoutingHints = hints
	return task
}

func (o *Orchestrator) attachRuntimeContext(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo) domain.Task {
	runtimeContext := o.memory.LoadMemoryContext(ctx, task, agent.ID, acceptance.Provider, acceptance.ModelName)
	hints := cloneMap(task.RoutingHints)
	hints["memory_context"] = runtimeContext
	if reasoning, ok := runtimeContext["reasoning_memory_brief"]; ok {
		hints["reasoning_context"] = reasoning
	}
	if traceHits, ok := runtimeContext["reasoning_trace_hits"]; ok {
		hints["reasoning_trace_hits"] = traceHits
	}
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

func (o *Orchestrator) executeAgentWithReasoning(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent agents.Agent) (domain.Task, domain.AgentResult, time.Duration) {
	startedAt := time.Now().UTC()
	if !o.shouldUseSelfLearningReasoning(task, acceptance, agent) {
		return task, agent.Execute(ctx, task), time.Since(startedAt)
	}

	agentInfo := agent.Info()
	workingTask := task
	ragQueries := make([]string, 0, o.selfLearn.reasoningMaxRounds)
	criticFeedback := make([]string, 0, o.selfLearn.criticMaxRounds)
	best := reasoningCandidate{}
	bestSet := false
	totalRounds := maxInt(1, o.selfLearn.reasoningMaxRounds)
	usedStructuredReasoning := false

	for round := 1; round <= totalRounds; round++ {
		reasoningTask := o.buildReasoningExecutionTask(workingTask, acceptance, round, totalRounds, ragQueries, criticFeedback)
		result := agent.Execute(ctx, reasoningTask)
		candidateTask, candidate := o.parseReasoningCandidate(ctx, workingTask, reasoningTask, acceptance, agentInfo, result, round, ragQueries, criticFeedback)
		workingTask = candidateTask

		structured := strings.TrimSpace(candidate.response.Thought) != "" || strings.TrimSpace(candidate.response.RAGQuery) != "" || strings.TrimSpace(candidate.response.Code) != ""
		if !structured {
			return task, result, time.Since(startedAt)
		}
		usedStructuredReasoning = true

		if !bestSet || candidate.score >= best.score {
			best = candidate
			bestSet = true
		}
		if candidate.response.RAGQuery != "" && len(candidate.ragResults) > 0 && round < totalRounds {
			ragQueries = append(ragQueries, candidate.response.RAGQuery)
			continue
		}
		break
	}

	if !bestSet || !usedStructuredReasoning {
		return task, agent.Execute(ctx, task), time.Since(startedAt)
	}

	if o.selfLearn.criticEnabled && (strings.TrimSpace(best.response.Thought) != "" || strings.TrimSpace(best.response.Code) != "") {
		current := best
		for criticRound := 1; criticRound <= maxInt(1, o.selfLearn.criticMaxRounds); criticRound++ {
			critiqueTask := o.buildCritiqueTask(current.task, acceptance, current.result, criticRound)
			critiqueResult := agent.Execute(ctx, critiqueTask)
			critiqueResponse := o.parseReasoningOutput(critiqueResult.Output.Summary)
			feedback := strings.TrimSpace(firstNonEmptyString(critiqueResponse.Thought, critiqueResponse.FinalText, critiqueResult.Output.Summary))
			if feedback == "" || (strings.TrimSpace(critiqueResponse.Thought) == "" && strings.TrimSpace(critiqueResponse.Code) == "" && strings.TrimSpace(critiqueResponse.RAGQuery) == "") {
				break
			}
			criticFeedback = append(criticFeedback, feedback)
			revisedTask := o.enrichTaskWithReasoningContext(current.task, critiqueResponse.Thought, feedback, criticRound)
			reasoningTask := o.buildReasoningExecutionTask(revisedTask, acceptance, totalRounds+criticRound, totalRounds+o.selfLearn.criticMaxRounds, ragQueries, criticFeedback)
			revisedResult := agent.Execute(ctx, reasoningTask)
			revisedTask, revisedCandidate := o.parseReasoningCandidate(ctx, revisedTask, reasoningTask, acceptance, agentInfo, revisedResult, totalRounds+criticRound, ragQueries, criticFeedback)
			structured := strings.TrimSpace(revisedCandidate.response.Thought) != "" || strings.TrimSpace(revisedCandidate.response.RAGQuery) != "" || strings.TrimSpace(revisedCandidate.response.Code) != ""
			if !structured {
				break
			}
			if revisedCandidate.score >= current.score {
				current = revisedCandidate
				best = revisedCandidate
				bestSet = true
				workingTask = revisedTask
				continue
			}
			break
		}
	}

	if best.result.TaskID == "" {
		best.result.TaskID = task.ID
	}
	if best.result.AgentID == "" {
		best.result.AgentID = agentInfo.ID
	}
	if best.result.Provider == "" {
		best.result.Provider = firstNonEmptyString(best.result.Provider, acceptance.Provider, agentInfo.Provider)
	}
	if best.result.ModelName == "" {
		best.result.ModelName = firstNonEmptyString(best.result.ModelName, acceptance.ModelName, agentInfo.ModelName)
	}
	if best.result.CompletedAt.IsZero() {
		best.result.CompletedAt = time.Now().UTC()
	}
	return best.task, best.result, time.Since(startedAt)
}

func (o *Orchestrator) shouldUseSelfLearningReasoning(task domain.Task, acceptance domain.TaskAcceptance, agent agents.Agent) bool {
	if o == nil || !o.selfLearn.enabled || !o.selfLearn.reasoningEnabled || agent == nil {
		return false
	}
	switch normalizeTaskStatus(acceptance.Status) {
	case domain.TaskStatusDeadLettered, domain.TaskStatusRejected:
		return false
	}
	switch task.Type {
	case domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeReview, domain.TaskTypeTest, domain.TaskTypeResearch, domain.TaskTypePlan, domain.TaskTypeDocs:
		return true
	default:
		return strings.TrimSpace(task.Input.Description) != ""
	}
}

func (o *Orchestrator) buildReasoningExecutionTask(task domain.Task, acceptance domain.TaskAcceptance, round int, maxRounds int, ragQueries []string, criticFeedback []string) domain.Task {
	cloned := task
	cloned.Input.Files = append([]string(nil), task.Input.Files...)
	cloned.Input.Constraints = append([]string(nil), task.Input.Constraints...)
	cloned.Input.AcceptanceCriteria = append([]string(nil), task.Input.AcceptanceCriteria...)
	cloned.RoutingHints = cloneMap(task.RoutingHints)
	if cloned.RoutingHints == nil {
		cloned.RoutingHints = map[string]any{}
	}
	var builder strings.Builder
	builder.WriteString("You are the runtime reasoning core for the orchestrator. Solve the task deliberately and return structured output.\n")
	builder.WriteString("Return XML-like tags in this exact shape:\n")
	builder.WriteString("<thought>step by step reasoning</thought>\n")
	builder.WriteString("<rag_query>optional retrieval query when more context is required</rag_query>\n")
	builder.WriteString("<code>final code when code is needed</code>\n")
	builder.WriteString("After the tags, add the final user-facing answer in plain text.\n\n")
	builder.WriteString(fmt.Sprintf("Round %d of %d. Provider=%s Model=%s\n\n", round, maxRounds, acceptance.Provider, acceptance.ModelName))
	builder.WriteString("Original task:\n")
	builder.WriteString(strings.TrimSpace(task.Input.Description))
	if len(task.Input.Files) > 0 {
		builder.WriteString("\n\nRelevant files:\n- ")
		builder.WriteString(strings.Join(task.Input.Files, "\n- "))
	}
	if len(task.Input.Constraints) > 0 {
		builder.WriteString("\n\nConstraints:\n- ")
		builder.WriteString(strings.Join(task.Input.Constraints, "\n- "))
	}
	if len(task.Input.AcceptanceCriteria) > 0 {
		builder.WriteString("\n\nAcceptance criteria:\n- ")
		builder.WriteString(strings.Join(task.Input.AcceptanceCriteria, "\n- "))
	}
	if context := strings.TrimSpace(kernelString(task.RoutingHints["reasoning_context"])); context != "" {
		builder.WriteString("\n\nPrevious reasoning context:\n")
		builder.WriteString(context)
	}
	if ragContext := strings.TrimSpace(kernelString(task.RoutingHints["reasoning_rag_context"])); ragContext != "" {
		builder.WriteString("\n\nRetrieved context:\n")
		builder.WriteString(ragContext)
	}
	if len(ragQueries) > 0 {
		builder.WriteString("\n\nPrevious retrieval queries:\n- ")
		builder.WriteString(strings.Join(ragQueries, "\n- "))
	}
	if len(criticFeedback) > 0 {
		builder.WriteString("\n\nCritic feedback to address:\n- ")
		builder.WriteString(strings.Join(criticFeedback, "\n- "))
	}
	cloned.Input.Description = strings.TrimSpace(builder.String())
	cloned.RoutingHints["reasoning_round"] = round
	cloned.RoutingHints["reasoning_mode"] = "deliberative_runtime"
	cloned.RoutingHints["reasoning_requested_tags"] = []string{"thought", "rag_query", "code"}
	return cloned
}

func (o *Orchestrator) buildCritiqueTask(task domain.Task, acceptance domain.TaskAcceptance, result domain.AgentResult, criticRound int) domain.Task {
	critique := task
	critique.RoutingHints = cloneMap(task.RoutingHints)
	if critique.RoutingHints == nil {
		critique.RoutingHints = map[string]any{}
	}
	code := extractGeneratedCode(result)
	var builder strings.Builder
	builder.WriteString("You are the critic for the orchestrator runtime. Review the candidate solution and decide whether it should be revised.\n")
	builder.WriteString("Return tags in the same structured format. If the answer is already good, keep <code> identical and explain approval in <thought>.\n\n")
	builder.WriteString(fmt.Sprintf("Critique round %d. Provider=%s Model=%s\n\n", criticRound, acceptance.Provider, acceptance.ModelName))
	builder.WriteString("Original task:\n")
	builder.WriteString(strings.TrimSpace(task.Input.Description))
	builder.WriteString("\n\nCurrent answer:\n")
	builder.WriteString(strings.TrimSpace(result.Output.Summary))
	if strings.TrimSpace(code) != "" {
		builder.WriteString("\n\nCurrent generated code:\n```go\n")
		builder.WriteString(code)
		builder.WriteString("\n```")
	}
	critique.Input.Description = strings.TrimSpace(builder.String())
	critique.RoutingHints["reasoning_mode"] = "critic"
	critique.RoutingHints["critic_round"] = criticRound
	return critique
}
func (o *Orchestrator) parseReasoningCandidate(ctx context.Context, baseTask domain.Task, reasoningTask domain.Task, acceptance domain.TaskAcceptance, agentInfo domain.AgentInfo, result domain.AgentResult, round int, ragQueries []string, criticFeedback []string) (domain.Task, reasoningCandidate) {
	if result.CompletedAt.IsZero() {
		result.CompletedAt = time.Now().UTC()
	}
	response := o.parseReasoningOutput(result.Output.Summary)
	if strings.TrimSpace(response.Code) == "" {
		response.Code = strings.TrimSpace(extractGeneratedCode(result))
	}
	if strings.TrimSpace(response.FinalText) == "" {
		response.FinalText = strings.TrimSpace(result.Output.Summary)
	}
	artifacts := cloneMap(result.Output.Artifacts)
	if artifacts == nil {
		artifacts = map[string]any{}
	}
	artifacts["reasoning_round"] = round
	artifacts["reasoning_thought"] = response.Thought
	artifacts["rag_queries"] = append([]string(nil), ragQueries...)
	artifacts["critic_feedback"] = append([]string(nil), criticFeedback...)
	if strings.TrimSpace(response.Code) != "" {
		artifacts["generated_code"] = response.Code
	}
	updatedTask := baseTask
	updatedTask.RoutingHints = cloneMap(baseTask.RoutingHints)
	if updatedTask.RoutingHints == nil {
		updatedTask.RoutingHints = map[string]any{}
	}
	if strings.TrimSpace(response.Thought) != "" {
		updatedTask.RoutingHints["reasoning_context"] = response.Thought
	}
	if strings.TrimSpace(response.FinalText) != "" {
		result.Output.Summary = response.FinalText
	}
	ragResults := []domain.RAGResult(nil)
	if strings.TrimSpace(response.RAGQuery) != "" && o.selfLearn.retriever != nil {
		ragResults, _ = o.selfLearn.retriever.Retrieve(ctx, domain.RAGQuery{
			Query:     response.RAGQuery,
			SessionID: firstNonEmptyString(baseTask.SessionID, reasoningTask.SessionID),
			TaskID:    baseTask.ID,
			Limit:     5,
		})
		if len(ragResults) > 0 {
			updatedTask.RoutingHints["reasoning_rag_query"] = response.RAGQuery
			updatedTask.RoutingHints["reasoning_rag_context"] = formatRAGResults(ragResults)
			artifacts["rag_query"] = response.RAGQuery
			artifacts["rag_result_count"] = len(ragResults)
		}
	}
	result.Output.Artifacts = artifacts
	evaluation, score := o.scoreReasoningCandidate(ctx, baseTask, acceptance, agentInfo, result, response, ragResults)
	artifacts["reasoning_candidate_score"] = score
	if !evaluation.CompletedAt.IsZero() {
		artifacts["reasoning_candidate_status"] = evaluation.Status
	}
	result.Output.Artifacts = artifacts
	return updatedTask, reasoningCandidate{
		task:           updatedTask,
		result:         result,
		response:       response,
		ragResults:     ragResults,
		evaluation:     evaluation,
		score:          score,
		round:          round,
		ragQueries:     append([]string(nil), ragQueries...),
		criticFeedback: append([]string(nil), criticFeedback...),
	}
}

func (o *Orchestrator) parseReasoningOutput(summary string) domain.ReasoningResponse {
	parser := selflearn.NewTraceParser()
	parser.Consume(summary)
	response, err := parser.Result()
	if err != nil {
		return domain.ReasoningResponse{FinalText: strings.TrimSpace(summary)}
	}
	if strings.TrimSpace(response.FinalText) == "" {
		response.FinalText = strings.TrimSpace(summary)
	}
	return response
}

func (o *Orchestrator) enrichTaskWithReasoningContext(task domain.Task, thought string, critic string, round int) domain.Task {
	cloned := task
	cloned.RoutingHints = cloneMap(task.RoutingHints)
	if cloned.RoutingHints == nil {
		cloned.RoutingHints = map[string]any{}
	}
	if strings.TrimSpace(thought) != "" {
		cloned.RoutingHints["reasoning_context"] = thought
	}
	if strings.TrimSpace(critic) != "" {
		cloned.RoutingHints["reasoning_critic_feedback"] = critic
	}
	cloned.RoutingHints["reasoning_revision_round"] = round
	return cloned
}

func (o *Orchestrator) scoreReasoningCandidate(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agentInfo domain.AgentInfo, result domain.AgentResult, response domain.ReasoningResponse, ragResults []domain.RAGResult) (domain.CodeExecutionResult, float64) {
	score := 0.0
	if result.Status != domain.TaskStatusFailed {
		score += 0.25
	}
	if strings.TrimSpace(response.Thought) != "" {
		score += 0.1
	}
	if len(ragResults) > 0 {
		score += 0.1
	}
	if strings.TrimSpace(response.FinalText) != "" {
		score += 0.15
	}
	code := strings.TrimSpace(response.Code)
	if code == "" {
		if result.Confidence > 0 {
			score += result.Confidence * 0.4
		}
		if score > 1 {
			score = 1
		}
		return domain.CodeExecutionResult{}, score
	}
	if o.selfLearn.evaluator == nil {
		score += 0.35
		if score > 1 {
			score = 1
		}
		return domain.CodeExecutionResult{Status: domain.TraceRecordStatusPending, CompletedAt: time.Now().UTC(), Score: score}, score
	}
	trace := domain.TraceRecord{
		TraceID:        fmt.Sprintf("%s-reasoning-%d", task.ID, time.Now().UTC().UnixNano()),
		SessionID:      firstNonEmptyString(task.SessionID, task.ID),
		TaskID:         task.ID,
		Provider:       firstNonEmptyString(result.Provider, acceptance.Provider, agentInfo.Provider),
		ModelName:      firstNonEmptyString(result.ModelName, acceptance.ModelName, agentInfo.ModelName),
		Prompt:         summarizeTaskPrompt(task),
		ThoughtProcess: strings.TrimSpace(response.Thought),
		GeneratedCode:  code,
		FinalAnswer:    strings.TrimSpace(response.FinalText),
		CreatedAt:      time.Now().UTC(),
	}
	evaluation, err := o.selfLearn.evaluator.Evaluate(ctx, trace)
	if err != nil {
		evaluation.Status = domain.TraceRecordStatusFail
		evaluation.ErrorLog = firstNonEmptyString(strings.TrimSpace(evaluation.ErrorLog), err.Error())
		if evaluation.CompletedAt.IsZero() {
			evaluation.CompletedAt = time.Now().UTC()
		}
	}
	score += 0.4 * evaluation.Score
	if score > 1 {
		score = 1
	}
	return evaluation, score
}

func formatRAGResults(results []domain.RAGResult) string {
	if len(results) == 0 {
		return ""
	}
	var builder strings.Builder
	for idx, item := range results {
		builder.WriteString(fmt.Sprintf("[%d] %s\n", idx+1, strings.TrimSpace(item.DocumentID)))
		content := strings.TrimSpace(item.Content)
		if content != "" {
			builder.WriteString(content)
			builder.WriteString("\n")
		}
	}
	return strings.TrimSpace(builder.String())
}

func (o *Orchestrator) persistExecutionState(ctx context.Context, task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, result domain.AgentResult, latency time.Duration) {
	reasoningTrace := o.buildReasoningTrace(task, acceptance, agent, result, latency)
	payload := map[string]any{
		"last_result_status":          result.Status,
		"last_result_summary":         result.Output.Summary,
		"last_completed_at":           time.Now().UTC(),
		"last_latency_ms":             latency.Milliseconds(),
		"last_reasoning_trace_id":     reasoningTrace.TraceID,
		"last_reflection_summary":     reasoningTrace.ReflectionSummary,
		"last_reasoning_mode":         reasoningTrace.ReasoningMode,
		"last_retrieval_used":         reasoningTrace.RetrievalUsed,
		"last_reasoning_memory_count": reasoningTrace.ReasoningMemoryCount,
	}
	o.persistSessionState(ctx, task, acceptance, agent, payload)
	if o.memory != nil {
		_ = o.memory.RecordTaskExchange(ctx, task, result)
		_ = o.memory.RecordRouteOutcome(ctx, task, acceptance, result, latency)
		_ = o.memory.RecordReasoningTrace(ctx, reasoningTrace)
	}
	o.scheduleSelfLearningFeedback(task, acceptance, agent, result, latency, reasoningTrace)
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
	providers := []string{currentProvider, "local", "mistral", "codexsale", "openai"}
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

func (o *Orchestrator) buildReasoningTrace(task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, result domain.AgentResult, latency time.Duration) domain.ReasoningTrace {
	memoryContext := normalizeAnyMap(task.RoutingHints["memory_context"])
	supportLanes := normalizeSupportLanes(task.RoutingHints["support_lanes"])
	selectionTrace := normalizeAnyMap(task.RoutingHints["selection_trace"])
	retrievalProvenance, _ := memoryContext["retrieval_provenance"].([]map[string]any)
	routeMemory := normalizeAnyMap(memoryContext["route_memory"])
	metadata := map[string]any{
		"project":         task.Context.Project,
		"repo_path":       task.Context.RepoPath,
		"selection_trace": selectionTrace,
	}
	return domain.ReasoningTrace{
		TraceID:              firstNonEmptyString(kernelString(task.RoutingHints["trace_id"]), task.ID+"-reasoning"),
		SessionID:            firstNonEmptyString(task.SessionID, task.ID),
		TaskID:               task.ID,
		ParentTaskID:         task.ParentTaskID,
		AgentID:              firstNonEmptyString(result.AgentID, agent.ID, acceptance.AgentID),
		Provider:             firstNonEmptyString(result.Provider, acceptance.Provider, agent.Provider),
		ModelName:            firstNonEmptyString(result.ModelName, acceptance.ModelName, agent.ModelName),
		TaskType:             task.Type,
		Branch:               defaultBranch(task.Context.Branch),
		PromptSummary:        summarizeTaskPrompt(task),
		ReflectionSummary:    summarizeTaskReflection(task, acceptance, result, supportLanes, memoryContext),
		ResultSummary:        truncateKernelText(result.Output.Summary, 280),
		ReasoningMode:        inferReasoningMode(acceptance.Provider, supportLanes),
		RetrievalUsed:        len(retrievalProvenance) > 0 || strings.TrimSpace(kernelString(memoryContext["vector_memory_brief"])) != "",
		VectorMemoryCount:    normalizeInt(memoryContext["vector_memory_count"]),
		RouteMemoryCount:     normalizeInt(routeMemory["route_memory_count"]),
		ReasoningMemoryCount: normalizeInt(memoryContext["reasoning_memory_count"]),
		LatencyMS:            latency.Milliseconds(),
		DecisionPoints:       buildReasoningDecisionPoints(selectionTrace, supportLanes, acceptance, result),
		NextQuestions:        summarizeNextQuestions(task),
		Metadata:             metadata,
		CreatedAt:            time.Now().UTC(),
	}
}

func summarizeTaskPrompt(task domain.Task) string {
	parts := []string{truncateKernelText(task.Input.Description, 220)}
	if len(task.Input.Files) > 0 {
		parts = append(parts, "files="+strings.Join(task.Input.Files, ", "))
	}
	if len(task.Input.AcceptanceCriteria) > 0 {
		parts = append(parts, "acceptance="+truncateKernelText(strings.Join(task.Input.AcceptanceCriteria, "; "), 160))
	}
	return strings.TrimSpace(strings.Join(parts, "; "))
}

func summarizeTaskReflection(task domain.Task, acceptance domain.TaskAcceptance, result domain.AgentResult, supportLanes []domain.SupportLane, memoryCtx map[string]any) string {
	parts := []string{fmt.Sprintf("provider=%s model=%s capability=%s", acceptance.Provider, acceptance.ModelName, task.RequiredCapability)}
	if len(supportLanes) > 0 {
		laneParts := make([]string, 0, len(supportLanes))
		for _, lane := range supportLanes {
			laneParts = append(laneParts, firstNonEmptyString(strings.TrimSpace(lane.Role), strings.TrimSpace(lane.Provider)))
		}
		parts = append(parts, "helpers="+strings.Join(laneParts, ","))
	}
	if brief := strings.TrimSpace(kernelString(memoryCtx["reasoning_memory_brief"])); brief != "" {
		parts = append(parts, "reasoning_memory="+truncateKernelText(brief, 160))
	}
	if summary := strings.TrimSpace(result.Output.Summary); summary != "" {
		parts = append(parts, "result="+truncateKernelText(summary, 180))
	}
	return strings.Join(parts, "; ")
}

func inferReasoningMode(provider string, supportLanes []domain.SupportLane) string {
	if len(supportLanes) > 0 {
		return "hybrid_parallel"
	}
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "ai_kernel", "local":
		return "local_reflection"
	default:
		return "single_route"
	}
}

func buildReasoningDecisionPoints(selectionTrace map[string]any, supportLanes []domain.SupportLane, acceptance domain.TaskAcceptance, result domain.AgentResult) []domain.ReasoningDecisionPoint {
	points := make([]domain.ReasoningDecisionPoint, 0, 4)
	if len(selectionTrace) > 0 {
		points = append(points, domain.ReasoningDecisionPoint{
			Kind:     "selection",
			Summary:  truncateKernelText(kernelString(selectionTrace["reason"]), 180),
			Outcome:  firstNonEmptyString(acceptance.Provider, acceptance.ModelName),
			Metadata: cloneMap(selectionTrace),
		})
	}
	if len(supportLanes) > 0 {
		lanes := make([]map[string]any, 0, len(supportLanes))
		for _, lane := range supportLanes {
			lanes = append(lanes, map[string]any{
				"provider":   lane.Provider,
				"model_name": lane.ModelName,
				"role":       lane.Role,
				"reason":     lane.Reason,
			})
		}
		points = append(points, domain.ReasoningDecisionPoint{
			Kind:    "assistant_pool",
			Summary: fmt.Sprintf("attached %d assistant lanes", len(supportLanes)),
			Outcome: "support_enabled",
			Metadata: map[string]any{
				"lanes": lanes,
			},
		})
	}
	points = append(points, domain.ReasoningDecisionPoint{
		Kind:    "result",
		Summary: truncateKernelText(result.Output.Summary, 180),
		Outcome: string(result.Status),
		Metadata: map[string]any{
			"provider":    result.Provider,
			"model_name":  result.ModelName,
			"error_count": len(result.Errors),
		},
	})
	return points
}

func summarizeNextQuestions(task domain.Task) []string {
	questions := make([]string, 0, 3)
	if len(task.Input.AcceptanceCriteria) > 0 {
		questions = append(questions, "Какие acceptance criteria еще не покрыты полностью?")
	}
	if len(task.Input.Files) > 1 {
		questions = append(questions, "Какие связанные файлы стоит проверить следующими?")
	}
	questions = append(questions, "Нужен ли дополнительный review или тестовый прогон?")
	if len(questions) > 3 {
		questions = questions[:3]
	}
	return questions
}

func normalizeSupportLanes(value any) []domain.SupportLane {
	lanes, ok := value.([]domain.SupportLane)
	if ok {
		return append([]domain.SupportLane(nil), lanes...)
	}
	return nil
}

func normalizeAnyMap(value any) map[string]any {
	mapped, ok := value.(map[string]any)
	if !ok || mapped == nil {
		return map[string]any{}
	}
	return cloneMap(mapped)
}

func normalizeInt(value any) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int32:
		return int(typed)
	case int64:
		return int(typed)
	case float32:
		return int(typed)
	case float64:
		return int(typed)
	default:
		return 0
	}
}

func truncateKernelText(text string, limit int) string {
	trimmed := strings.TrimSpace(text)
	if limit <= 0 || len(trimmed) <= limit {
		return trimmed
	}
	if limit <= 3 {
		return trimmed[:limit]
	}
	return trimmed[:limit-3] + "..."
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
	agentSlot := o.slotForAgent(agent.ID)
	modelSlot := o.slotForModel(modelName)
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
	if err := acquire(agentSlot); err != nil {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
		return nil, err
	}
	if err := acquire(modelSlot); err != nil {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
		return nil, err
	}
	o.refreshRuntimeCapacity(agent, agentSlot, modelSlot)
	return func() {
		for i := len(releases) - 1; i >= 0; i-- {
			releases[i]()
		}
		o.refreshRuntimeCapacity(agent, agentSlot, modelSlot)
	}, nil
}

func (o *Orchestrator) refreshRuntimeCapacity(agent domain.AgentInfo, agentSlot chan struct{}, modelSlot chan struct{}) {
	if o == nil || o.runtime == nil {
		return
	}
	snapshot := CapacitySnapshot{
		InFlight:        queueUsage(agentSlot),
		AgentSlotUsage:  queuePressure(agentSlot),
		ModelSlotUsage:  queuePressure(modelSlot),
		GlobalSlotUsage: queuePressure(o.globalSlots),
	}
	o.runtime.UpdateCapacitySnapshot(agent.ID, snapshot)
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

func queuePressure(ch chan struct{}) float64 {
	if ch == nil || cap(ch) == 0 {
		return 0
	}
	return float64(len(ch)) / float64(cap(ch))
}

func queueUsage(ch chan struct{}) int {
	if ch == nil {
		return 0
	}
	return len(ch)
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func envBool(key string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	switch value {
	case "", "default":
		return fallback
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func envDuration(key string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err == nil && parsed > 0 {
		return parsed
	}
	if seconds, err := strconv.Atoi(value); err == nil && seconds > 0 {
		return time.Duration(seconds) * time.Second
	}
	return fallback
}

func (o *Orchestrator) configureSelfLearning() {
	if o == nil {
		return
	}
	enabled := envBool("GO_CORE_SELFLEARN_ENABLED", o.store != nil)
	if !enabled || o.store == nil {
		return
	}
	runtime := selfLearningRuntime{
		enabled:            true,
		traceRecorder:      selflearn.NewStoreTraceRecorder(o.store, "self_learning"),
		evaluator:          selflearn.NewGoCodeEvaluator(selflearn.EvalConfig{}),
		datasetBuilder:     selflearn.NewStorePreferenceDatasetBuilder(o.store, "self_learning", "", envInt("GO_CORE_SELFLEARN_DATASET_LIMIT", 500)),
		trainingWindow:     envDuration("GO_CORE_SELFLEARN_TRAINING_WINDOW", 24*time.Hour),
		trainingTicker:     envDuration("GO_CORE_SELFLEARN_TRAINING_INTERVAL", 24*time.Hour),
		trainingTimeout:    envDuration("GO_CORE_SELFLEARN_TRAINING_TIMEOUT", 2*time.Hour),
		datasetDir:         strings.TrimSpace(os.Getenv("GO_CORE_SELFLEARN_DATASET_DIR")),
		reasoningEnabled:   envBool("GO_CORE_SELFLEARN_REASONING_ENABLED", true),
		reasoningMaxRounds: maxInt(1, envInt("GO_CORE_SELFLEARN_REASONING_MAX_ROUNDS", 3)),
		criticEnabled:      envBool("GO_CORE_SELFLEARN_CRITIC_ENABLED", true),
		criticMaxRounds:    maxInt(1, envInt("GO_CORE_SELFLEARN_CRITIC_MAX_ROUNDS", 1)),
	}
	if runtime.datasetDir == "" {
		runtime.datasetDir = os.TempDir()
	}
	if o.memory != nil {
		runtime.retriever = selflearn.NewMemoryRAGRetriever(o.memory)
	}
	runtime.exporter = selflearn.NewJSONLExporter(runtime.datasetDir)
	if o.providerRegistry != nil {
		runtime.discovery = selflearn.NewDiscoveryService(o.providerRegistry, selflearn.DiscoveryConfig{
			Provider:        domain.TargetKernelProvider,
			TargetModel:     domain.TargetReasoningModel,
			LegacyModel:     domain.LegacyReasoningModel,
			RefreshInterval: envDuration("GO_CORE_SELFLEARN_DISCOVERY_INTERVAL", 5*time.Minute),
		})
		runtime.discovery.Start(o.backgroundCtx)
	}
	trainTemplate := splitCommandTemplate(os.Getenv("GO_CORE_SELFLEARN_TRAIN_CMD"))
	if len(trainTemplate) > 0 {
		runtime.trainer = selflearn.NewExecTrainer(selflearn.ExecConfig{
			CommandTemplate: trainTemplate,
			WorkDir:         strings.TrimSpace(os.Getenv("GO_CORE_SELFLEARN_TRAIN_WORKDIR")),
			Env:             os.Environ(),
		})
	}
	reloadTemplate := splitCommandTemplate(os.Getenv("GO_CORE_SELFLEARN_RELOAD_CMD"))
	if len(reloadTemplate) > 0 {
		runtime.hotReloader = selflearn.NewExecHotReloader(selflearn.ExecConfig{
			CommandTemplate: reloadTemplate,
			WorkDir:         strings.TrimSpace(os.Getenv("GO_CORE_SELFLEARN_RELOAD_WORKDIR")),
			Env:             os.Environ(),
		}, runtime.discovery)
	}
	o.selfLearn = runtime
	if o.selfLearn.datasetBuilder != nil && o.selfLearn.trainer != nil && o.selfLearn.hotReloader != nil {
		go o.runSelfLearningScheduler(o.backgroundCtx)
	}
}

func splitCommandTemplate(value string) []string {
	fields := strings.Fields(strings.TrimSpace(value))
	if len(fields) == 0 {
		return nil
	}
	return fields
}

func (o *Orchestrator) scheduleSelfLearningFeedback(task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, result domain.AgentResult, latency time.Duration, reasoningTrace domain.ReasoningTrace) {
	if o == nil || !o.selfLearn.enabled || o.selfLearn.traceRecorder == nil {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(o.backgroundCtx, 30*time.Second)
		defer cancel()
		trace := o.buildSelfLearningTrace(task, acceptance, agent, result, latency, reasoningTrace)
		if o.selfLearn.evaluator != nil && strings.TrimSpace(trace.GeneratedCode) != "" {
			evaluation, err := o.selfLearn.evaluator.Evaluate(ctx, trace)
			if err != nil {
				evaluation.Status = domain.TraceRecordStatusFail
				evaluation.ErrorLog = firstNonEmptyString(strings.TrimSpace(evaluation.ErrorLog), err.Error())
				if evaluation.CompletedAt.IsZero() {
					evaluation.CompletedAt = time.Now().UTC()
				}
			}
			trace.Evaluation = evaluation
		}
		if trace.Evaluation.CompletedAt.IsZero() {
			trace.Evaluation.CompletedAt = time.Now().UTC()
		}
		if trace.Evaluation.Status == "" {
			trace.Evaluation.Status = statusToTraceRecordStatus(result.Status)
		}
		if trace.Evaluation.Score == 0 {
			trace.Evaluation.Score = defaultTraceScore(trace.Evaluation.Status)
		}
		if err := o.selfLearn.traceRecorder.RecordTrace(ctx, trace); err != nil {
			o.publishRuntimeEvent("selflearn", "selflearn.trace_failed", task.ID, map[string]any{
				"task_id": task.ID,
				"error":   err.Error(),
			})
			return
		}
		o.publishRuntimeEvent("selflearn", "selflearn.trace_recorded", task.ID, map[string]any{
			"task_id":     task.ID,
			"trace_id":    trace.TraceID,
			"status":      trace.Evaluation.Status,
			"score":       trace.Evaluation.Score,
			"model_name":  trace.ModelName,
			"provider":    trace.Provider,
			"latency_ms":  latency.Milliseconds(),
			"has_code":    trace.GeneratedCode != "",
			"session_id":  trace.SessionID,
			"dataset_dir": o.selfLearn.datasetDir,
		})
	}()
}

func (o *Orchestrator) buildSelfLearningTrace(task domain.Task, acceptance domain.TaskAcceptance, agent domain.AgentInfo, result domain.AgentResult, latency time.Duration, reasoningTrace domain.ReasoningTrace) domain.TraceRecord {
	code := extractGeneratedCode(result)
	thought := strings.TrimSpace(kernelString(task.RoutingHints["reasoning_context"]))
	if thought == "" {
		thought = strings.TrimSpace(reasoningTrace.ReflectionSummary)
	}
	return domain.TraceRecord{
		TraceID:        firstNonEmptyString(reasoningTrace.TraceID, task.ID+"-selflearn"),
		SessionID:      firstNonEmptyString(task.SessionID, reasoningTrace.SessionID),
		TaskID:         task.ID,
		Provider:       firstNonEmptyString(result.Provider, acceptance.Provider, agent.Provider),
		ModelName:      firstNonEmptyString(result.ModelName, acceptance.ModelName, agent.ModelName),
		Prompt:         summarizeTaskPrompt(task),
		ThoughtProcess: thought,
		GeneratedCode:  code,
		FinalAnswer:    strings.TrimSpace(result.Output.Summary),
		Evaluation: domain.CodeExecutionResult{
			Status:      statusToTraceRecordStatus(result.Status),
			Compiler:    "go",
			Score:       defaultTraceScore(statusToTraceRecordStatus(result.Status)),
			ErrorLog:    strings.Join(result.Errors, "\n"),
			CompletedAt: firstNonZeroTime(result.CompletedAt, time.Now().UTC()),
			DurationMS:  latency.Milliseconds(),
		},
		Metadata: map[string]any{
			"agent_id":             firstNonEmptyString(result.AgentID, agent.ID),
			"required_capability":  task.RequiredCapability,
			"task_type":            task.Type,
			"files_changed":        append([]string(nil), result.Output.FilesChanged...),
			"commands_run":         append([]string(nil), result.Output.CommandsRun...),
			"next_recommendations": append([]string(nil), result.NextRecommendations...),
		},
		CreatedAt: firstNonZeroTime(result.CompletedAt, time.Now().UTC()),
	}
}

func extractGeneratedCode(result domain.AgentResult) string {
	artifacts := result.Output.Artifacts
	for _, key := range []string{"generated_code", "code", "patch", "diff"} {
		if value := strings.TrimSpace(kernelString(artifacts[key])); value != "" {
			return value
		}
	}
	summary := strings.TrimSpace(result.Output.Summary)
	if strings.Contains(summary, "```") {
		parts := strings.Split(summary, "```")
		for i := 1; i < len(parts); i += 2 {
			candidate := strings.TrimSpace(parts[i])
			candidate = strings.TrimPrefix(candidate, "go\n")
			candidate = strings.TrimSpace(candidate)
			if candidate != "" {
				return candidate
			}
		}
	}
	return ""
}

func statusToTraceRecordStatus(status domain.TaskStatus) domain.TraceRecordStatus {
	switch status {
	case domain.TaskStatusDone:
		return domain.TraceRecordStatusSuccess
	case domain.TaskStatusFailed:
		return domain.TraceRecordStatusFail
	default:
		return domain.TraceRecordStatusPending
	}
}

func defaultTraceScore(status domain.TraceRecordStatus) float64 {
	switch status {
	case domain.TraceRecordStatusSuccess:
		return 1
	case domain.TraceRecordStatusFail:
		return 0
	default:
		return 0.25
	}
}

func firstNonZeroTime(values ...time.Time) time.Time {
	for _, value := range values {
		if !value.IsZero() {
			return value
		}
	}
	return time.Now().UTC()
}

func (o *Orchestrator) runSelfLearningScheduler(ctx context.Context) {
	ticker := time.NewTicker(o.selfLearn.trainingTicker)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			o.runSelfLearningTrainingCycle(ctx)
		}
	}
}

func (o *Orchestrator) runSelfLearningTrainingCycle(parent context.Context) {
	if o == nil || !o.selfLearn.enabled || o.selfLearn.datasetBuilder == nil || o.selfLearn.trainer == nil || o.selfLearn.hotReloader == nil || o.selfLearn.exporter == nil {
		return
	}
	ctx, cancel := context.WithTimeout(parent, o.selfLearn.trainingTimeout)
	defer cancel()
	since := time.Now().UTC().Add(-o.selfLearn.trainingWindow)
	examples, err := o.selfLearn.datasetBuilder.BuildPreferenceDataset(ctx, since)
	if err != nil {
		o.publishRuntimeEvent("selflearn", "selflearn.dataset_failed", "selflearn", map[string]any{"error": err.Error()})
		return
	}
	if len(examples) == 0 {
		return
	}
	datasetPath, err := o.selfLearn.exporter.ExportPreferenceDataset(ctx, examples)
	if err != nil {
		o.publishRuntimeEvent("selflearn", "selflearn.export_failed", "selflearn", map[string]any{"error": err.Error()})
		return
	}
	job := domain.FineTuneJob{
		JobID:       fmt.Sprintf("selflearn-%d", time.Now().UTC().UnixNano()),
		ModelName:   domain.TargetReasoningModel,
		BaseModel:   domain.TargetReasoningModel,
		DatasetPath: datasetPath,
		Status:      domain.FineTuneJobStatusQueued,
		Metadata: map[string]any{
			"example_count": len(examples),
			"window_start":  since,
		},
	}
	job, err = o.selfLearn.trainer.StartTraining(ctx, job)
	if err != nil {
		o.publishRuntimeEvent("selflearn", "selflearn.training_failed", job.JobID, map[string]any{
			"job_id":        job.JobID,
			"dataset_path":  datasetPath,
			"example_count": len(examples),
			"error":         err.Error(),
		})
		return
	}
	modelPath := firstNonEmptyString(job.MergedPath, job.AdapterPath)
	if strings.TrimSpace(modelPath) == "" {
		o.publishRuntimeEvent("selflearn", "selflearn.reload_skipped", job.JobID, map[string]any{
			"job_id": job.JobID,
			"reason": "trainer did not return merged_path or adapter_path",
		})
		return
	}
	if err := o.selfLearn.hotReloader.ReloadModel(ctx, domain.HotReloadRequest{
		Provider:  domain.TargetKernelProvider,
		ModelName: domain.TargetReasoningModel,
		ModelPath: modelPath,
		Metadata: map[string]any{
			"job_id":        job.JobID,
			"dataset_path":  datasetPath,
			"example_count": len(examples),
		},
	}); err != nil {
		o.publishRuntimeEvent("selflearn", "selflearn.reload_failed", job.JobID, map[string]any{
			"job_id":     job.JobID,
			"model_path": modelPath,
			"error":      err.Error(),
		})
		return
	}
	o.publishRuntimeEvent("selflearn", "selflearn.training_completed", job.JobID, map[string]any{
		"job_id":        job.JobID,
		"dataset_path":  datasetPath,
		"example_count": len(examples),
		"model_path":    modelPath,
	})
}

func maxInt(a int, b int) int {
	if a > b {
		return a
	}
	return b
}
