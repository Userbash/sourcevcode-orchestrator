package kernel

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/selflearn"
)

type codingRuntimeSessionMapper struct {
	mu              sync.RWMutex
	byTaskID        map[string]string
	bySessionID     map[string]string
	sessionMetadata map[string]domain.CodingRuntimeSession
}

func newCodingRuntimeSessionMapper() *codingRuntimeSessionMapper {
	return &codingRuntimeSessionMapper{
		byTaskID:        make(map[string]string),
		bySessionID:     make(map[string]string),
		sessionMetadata: make(map[string]domain.CodingRuntimeSession),
	}
}

func (m *codingRuntimeSessionMapper) bind(taskID string, session domain.CodingRuntimeSession) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.byTaskID[taskID] = session.SessionID
	m.bySessionID[session.SessionID] = taskID
	m.sessionMetadata[session.SessionID] = session
}

func (m *codingRuntimeSessionMapper) unbind(taskID, sessionID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if taskID != "" {
		delete(m.byTaskID, taskID)
	}
	if sessionID != "" {
		delete(m.bySessionID, sessionID)
		delete(m.sessionMetadata, sessionID)
	}
}

func (o *Orchestrator) AttachExternalCodingRuntime(runtime domain.ExternalCodingRuntime) {
	o.codingRuntime = runtime
	if o.codingSessions == nil {
		o.codingSessions = newCodingRuntimeSessionMapper()
	}
	o.publishInventorySnapshot(o.backgroundCtx)
}

func (o *Orchestrator) AttachRAGRetriever(retriever domain.RAGRetriever) {
	if retriever == nil {
		return
	}
	if o.selfLearn.retriever == nil {
		o.selfLearn.retriever = retriever
		return
	}
	o.selfLearn.retriever = selflearn.NewParallelRAGRetriever(o.selfLearn.retriever, retriever)
}

func (o *Orchestrator) shouldDelegateToCodingRuntime(task domain.Task) bool {
	if o.codingRuntime == nil {
		return false
	}
	if !o.codingRuntime.Supports(task) {
		return false
	}
	if hint := strings.TrimSpace(routingHintString(task.RoutingHints, "coding_runtime")); hint != "" {
		switch strings.ToLower(hint) {
		case "disable", "disabled", "false", "off", "none":
			return false
		case "force", "forced", "true", "on", strings.ToLower(o.codingRuntime.Name()):
			return true
		}
	}
	if isSourcecraftWork(task) {
		return true
	}
	switch task.Type {
	case domain.TaskTypeCode, domain.TaskTypeFix, domain.TaskTypeTest, domain.TaskTypeReview, domain.TaskTypePlan:
		return true
	}
	if len(task.Input.Files) > 0 {
		return true
	}
	capability := strings.ToLower(strings.TrimSpace(task.RequiredCapability))
	return capability == "sourcecraft" || capability == "code" || capability == "review"
}

func (o *Orchestrator) dispatchCodingRuntimeTaskSync(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) (domain.WorkflowRecord, error) {
	req := o.buildCodingRuntimeRequest(ctx, task, plan)
	session, err := o.codingRuntime.StartTask(ctx, req)
	if err != nil {
		result := o.failedCodingRuntimeResult(task, err)
		record := domain.WorkflowRecord{Task: task, Plan: plan, Acceptance: newCodingRuntimeAcceptance(task), Result: &result, UpdatedAt: time.Now().UTC()}
		if saveErr := o.store.SaveWorkflow(ctx, record); saveErr != nil {
			return domain.WorkflowRecord{}, saveErr
		}
		return record, nil
	}
	o.codingSessions.bind(task.ID, session)
	defer o.codingSessions.unbind(task.ID, session.SessionID)
	pending := domain.WorkflowRecord{Task: attachCodingRuntimeTaskContext(task, session, req), Plan: plan, Acceptance: newCodingRuntimeAcceptance(task), UpdatedAt: time.Now().UTC()}
	if err := o.store.SaveWorkflow(ctx, pending); err != nil {
		return domain.WorkflowRecord{}, err
	}
	o.startCodingRuntimeEventPump(session, task.ID)
	result, err := o.codingRuntime.WaitTask(ctx, session)
	if err != nil {
		result = o.failedCodingRuntimeResult(task, err)
	} else {
		result = normalizeTaskStatusResult(result, task)
	}
	record := o.finalizeCodingRuntimeWorkflow(task, plan, req, session, result)
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		return domain.WorkflowRecord{}, err
	}
	kind := "task.completed"
	if isFailureStatus(record.Acceptance.Status) {
		kind = "task.failed"
	}
	o.publishRuntimeEvent("tasks", kind, task.ID, map[string]any{"task": record.Task, "plan": plan, "acceptance": record.Acceptance, "result": result})
	o.publishInventorySnapshot(ctx)
	o.notifyWorkflowWaiters(record)
	return record, nil
}

func (o *Orchestrator) dispatchCodingRuntimeTaskAsync(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) (domain.WorkflowRecord, error) {
	req := o.buildCodingRuntimeRequest(ctx, task, plan)
	session, err := o.codingRuntime.StartTask(ctx, req)
	if err != nil {
		result := o.failedCodingRuntimeResult(task, err)
		record := domain.WorkflowRecord{Task: task, Plan: plan, Acceptance: newCodingRuntimeAcceptance(task), Result: &result, UpdatedAt: time.Now().UTC()}
		if saveErr := o.store.SaveWorkflow(ctx, record); saveErr != nil {
			return domain.WorkflowRecord{}, saveErr
		}
		o.notifyWorkflowWaiters(record)
		return record, nil
	}
	o.codingSessions.bind(task.ID, session)
	record := domain.WorkflowRecord{Task: attachCodingRuntimeTaskContext(task, session, req), Plan: plan, Acceptance: newCodingRuntimeAcceptance(task), UpdatedAt: time.Now().UTC()}
	if err := o.store.SaveWorkflow(ctx, record); err != nil {
		o.codingSessions.unbind(task.ID, session.SessionID)
		return domain.WorkflowRecord{}, err
	}
	o.startCodingRuntimeEventPump(session, task.ID)
	go func() {
		defer o.codingSessions.unbind(task.ID, session.SessionID)
		result, err := o.codingRuntime.WaitTask(o.backgroundCtx, session)
		if err != nil {
			result = o.failedCodingRuntimeResult(task, err)
		} else {
			result = normalizeTaskStatusResult(result, task)
		}
		finalRecord := o.finalizeCodingRuntimeWorkflow(task, plan, req, session, result)
		if saveErr := o.store.SaveWorkflow(o.backgroundCtx, finalRecord); saveErr == nil {
			kind := "task.completed"
			if isFailureStatus(finalRecord.Acceptance.Status) {
				kind = "task.failed"
			}
			o.publishRuntimeEvent("tasks", kind, task.ID, map[string]any{"task": finalRecord.Task, "plan": plan, "acceptance": finalRecord.Acceptance, "result": result})
			o.publishInventorySnapshot(o.backgroundCtx)
		}
		o.notifyWorkflowWaiters(finalRecord)
	}()
	return record, nil
}

func (o *Orchestrator) buildCodingRuntimeRequest(ctx context.Context, task domain.Task, plan domain.ExecutionPlan) domain.CodingRuntimeRequest {
	mode := codingRuntimeModeFromHints(task)
	ragResults := o.retrieveCodingRuntimeRAGContext(ctx, task)
	metadata := cloneMap(task.ExecutionContract)
	if metadata == nil {
		metadata = map[string]any{}
	}
	metadata["project"] = task.Context.Project
	metadata["repo"] = firstNonEmptyString(task.Context.RepoPath, task.Context.Project)
	metadata["session_id"] = task.SessionID
	metadata["routing_mode"] = string(mode)
	metadata["orchestrator"] = "go-core"
	metadata["plan_step_count"] = len(plan.Steps)
	metadata["plan_primary_capability"] = plan.PrimaryCapability
	if len(ragResults) > 0 {
		metadata["rag_context"] = formatRAGResults(ragResults)
	}
	return domain.CodingRuntimeRequest{
		Task:             task,
		Plan:             plan,
		Mode:             mode,
		AllowedTools:     firstNonEmptyList(stringListFromAny(task.ExecutionContract["allowed_tools"]), stringListFromAny(task.RoutingHints["allowed_tools"])),
		AllowedSubagents: firstNonEmptyList(stringListFromAny(task.ExecutionContract["allowed_subagents"]), stringListFromAny(task.RoutingHints["allowed_subagents"])),
		RAGResults:       ragResults,
		Metadata:         metadata,
	}
}

func (o *Orchestrator) retrieveCodingRuntimeRAGContext(ctx context.Context, task domain.Task) []domain.RAGResult {
	if o.selfLearn.retriever == nil {
		return nil
	}
	query := strings.TrimSpace(task.Input.Description)
	if query == "" {
		query = strings.Join(task.Input.Files, " ")
	}
	if query == "" {
		query = firstNonEmptyString(task.RequiredCapability, task.Context.Project)
	}
	if query == "" {
		return nil
	}
	ragCtx, cancel := context.WithTimeout(ctx, envDuration("GO_CORE_CODING_RAG_TIMEOUT", 2*time.Second))
	defer cancel()
	results, err := o.selfLearn.retriever.Retrieve(ragCtx, domain.RAGQuery{
		Query:     query,
		SessionID: task.SessionID,
		TaskID:    task.ID,
		Limit:     routingHintInt(task, "coding_rag_limit", 6),
		Filters: map[string]any{
			"repository": firstNonEmptyString(task.Context.RepoPath, task.Context.Project),
			"task_type":  string(task.Type),
		},
	})
	if err != nil {
		return nil
	}
	return results
}

func (o *Orchestrator) failedCodingRuntimeResult(task domain.Task, err error) domain.AgentResult {
	return domain.AgentResult{
		TaskID:              task.ID,
		AgentID:             o.codingRuntimeName(),
		Status:              domain.TaskStatusFailed,
		Provider:            o.codingRuntimeName(),
		Errors:              []string{err.Error()},
		NextRecommendations: []string{"retry_with_fallback_model", "inspect_coding_runtime"},
		CompletedAt:         time.Now().UTC(),
	}
}

func (o *Orchestrator) finalizeCodingRuntimeWorkflow(task domain.Task, plan domain.ExecutionPlan, req domain.CodingRuntimeRequest, session domain.CodingRuntimeSession, result domain.AgentResult) domain.WorkflowRecord {
	if strings.TrimSpace(result.TaskID) == "" {
		result.TaskID = task.ID
	}
	if strings.TrimSpace(result.AgentID) == "" {
		result.AgentID = o.codingRuntimeName()
	}
	if strings.TrimSpace(result.Provider) == "" {
		result.Provider = firstNonEmptyString(session.Provider, o.codingRuntimeName())
	}
	if strings.TrimSpace(result.ModelName) == "" {
		result.ModelName = session.Model
	}
	if result.CompletedAt.IsZero() {
		result.CompletedAt = time.Now().UTC()
	}
	acceptance := newCodingRuntimeAcceptance(task)
	acceptance.Status = normalizeTaskStatus(result.Status)
	if acceptance.Status == domain.TaskStatusCompleted {
		acceptance.Reason = firstNonEmptyString(acceptance.Reason, "coding runtime execution completed")
	} else if len(result.Errors) > 0 {
		acceptance.Reason = firstNonEmptyString(strings.Join(result.Errors, "; "), acceptance.Reason)
	}
	return domain.WorkflowRecord{
		Task:       attachCodingRuntimeTaskContext(task, session, req),
		Plan:       plan,
		Acceptance: acceptance,
		Result:     &result,
		UpdatedAt:  time.Now().UTC(),
	}
}

func (o *Orchestrator) startCodingRuntimeEventPump(session domain.CodingRuntimeSession, taskID string) {
	if o.codingRuntime == nil {
		return
	}
	events, err := o.codingRuntime.Events(o.backgroundCtx, session.SessionID)
	if err != nil {
		return
	}
	go func() {
		for event := range events {
			metadata := cloneMap(event.Metadata)
			metadata["session_id"] = session.SessionID
			metadata["task_id"] = taskID
			metadata["runtime"] = firstNonEmptyString(session.Runtime, o.codingRuntimeName())
			if event.Message != "" {
				metadata["message"] = event.Message
			}
			if event.Progress > 0 {
				metadata["progress"] = event.Progress
			}
			o.publishRuntimeEvent("tasks", firstNonEmptyString(event.Kind, "task.progress"), taskID, metadata)
		}
	}()
}

func attachCodingRuntimeTaskContext(task domain.Task, session domain.CodingRuntimeSession, req domain.CodingRuntimeRequest) domain.Task {
	updated := task
	updated.ExecutionContract = cloneMap(task.ExecutionContract)
	if updated.ExecutionContract == nil {
		updated.ExecutionContract = map[string]any{}
	}
	updated.ExecutionContract["coding_runtime"] = firstNonEmptyString(session.Runtime, session.Provider)
	updated.ExecutionContract["coding_runtime_session_id"] = session.SessionID
	updated.ExecutionContract["coding_runtime_mode"] = string(req.Mode)
	updated.ExecutionContract["coding_runtime_accepted_at"] = firstNonZeroTime(session.AcceptedAt, time.Now().UTC()).Format(time.RFC3339)
	if len(req.RAGResults) > 0 {
		updated.ExecutionContract["coding_runtime_rag_results"] = formatRAGResults(req.RAGResults)
	}
	return updated
}

func newCodingRuntimeAcceptance(task domain.Task) domain.TaskAcceptance {
	return domain.TaskAcceptance{
		TaskID:     task.ID,
		Status:     domain.TaskStatusAccepted,
		Complexity: firstNonEmptyComplexity(task.Complexity, domain.ComplexityMedium),
		Reason:     "delegated to coding runtime",
		Capability: task.RequiredCapability,
		Provider:   firstNonEmptyString(task.AssignedProvider),
		ModelName:  firstNonEmptyString(task.AssignedModel),
		AcceptedAt: time.Now().UTC(),
	}
}

func codingRuntimeModeFromHints(task domain.Task) domain.CodingRuntimeMode {
	if hint := strings.TrimSpace(routingHintString(task.RoutingHints, "coding_mode")); hint != "" {
		switch strings.ToLower(hint) {
		case string(domain.CodingRuntimeModePlan):
			return domain.CodingRuntimeModePlan
		case string(domain.CodingRuntimeModeReview):
			return domain.CodingRuntimeModeReview
		case string(domain.CodingRuntimeModeBuild):
			return domain.CodingRuntimeModeBuild
		}
	}
	switch task.Type {
	case domain.TaskTypePlan:
		return domain.CodingRuntimeModePlan
	case domain.TaskTypeReview:
		return domain.CodingRuntimeModeReview
	default:
		return domain.CodingRuntimeModeBuild
	}
}

func stringListFromAny(value any) []string {
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		items := make([]string, 0, len(typed))
		for _, item := range typed {
			if text := strings.TrimSpace(fmt.Sprint(item)); text != "" {
				items = append(items, text)
			}
		}
		return items
	case string:
		if strings.TrimSpace(typed) == "" {
			return nil
		}
		parts := strings.Split(typed, ",")
		items := make([]string, 0, len(parts))
		for _, part := range parts {
			if text := strings.TrimSpace(part); text != "" {
				items = append(items, text)
			}
		}
		return items
	default:
		return nil
	}
}

func firstNonEmptyList(lists ...[]string) []string {
	for _, list := range lists {
		if len(list) == 0 {
			continue
		}
		items := make([]string, 0, len(list))
		for _, item := range list {
			if text := strings.TrimSpace(item); text != "" {
				items = append(items, text)
			}
		}
		if len(items) > 0 {
			return items
		}
	}
	return nil
}

func normalizeTaskStatusResult(result domain.AgentResult, task domain.Task) domain.AgentResult {
	result.Status = normalizeTaskStatus(result.Status)
	if result.TaskID == "" {
		result.TaskID = task.ID
	}
	return result
}

func isFailureStatus(status domain.TaskStatus) bool {
	status = normalizeTaskStatus(status)
	return status == domain.TaskStatusFailed || status == domain.TaskStatusRejected || status == domain.TaskStatusDeadLettered
}

func firstNonEmptyComplexity(values ...domain.Complexity) domain.Complexity {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return domain.ComplexityMedium
}

func (o *Orchestrator) codingRuntimeName() string {
	if o.codingRuntime == nil {
		return "opencode"
	}
	if name := strings.TrimSpace(o.codingRuntime.Name()); name != "" {
		return name
	}
	return "opencode"
}
