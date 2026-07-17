package kernel

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func (o *Orchestrator) PreviewExecutionPlan(ctx context.Context, task domain.Task) (domain.ExecutionPlanPreview, error) {
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

	task, plan := o.planner.Prepare(task)
	artifact := buildPlanArtifact(task, plan)
	pending := make([]string, 0, len(artifact.Tasks))
	for _, planned := range artifact.Tasks {
		pending = append(pending, planned.TaskID)
	}
	checkpoint := domain.ParallelPlanCheckpoint{
		Kind:             "parallel_plan_checkpoint",
		RootTaskID:       task.ID,
		SessionID:        task.SessionID,
		Branch:           checkpointBranchName(task.ID),
		RootTask:         task,
		Plan:             plan,
		PlanArtifact:     artifact,
		PendingTaskIDs:   pending,
		CompletedTaskIDs: []string{},
		ResultsByTaskID:  map[string]any{},
		BatchNo:          1,
		Status:           domain.ParallelPlanStatusPlanned,
		UpdatedAt:        time.Now().UTC(),
	}
	if err := o.saveParallelCheckpointStatic(ctx, checkpoint); err != nil {
		return domain.ExecutionPlanPreview{}, err
	}
	if err := o.saveParallelCheckpoint(ctx, checkpoint); err != nil {
		return domain.ExecutionPlanPreview{}, err
	}
	return domain.ExecutionPlanPreview{
		Task:             task,
		Plan:             plan,
		PlanArtifact:     artifact,
		PendingTaskIDs:   pending,
		CheckpointBranch: checkpoint.Branch,
		CreatedAt:        checkpoint.UpdatedAt,
	}, nil
}

func (o *Orchestrator) RunExecutionPlan(ctx context.Context, task domain.Task) (domain.ExecutionPlanRun, error) {
	preview, err := o.PreviewExecutionPlan(ctx, task)
	if err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	checkpoint, ok, err := o.LoadParallelCheckpoint(ctx, preview.Task.SessionID, preview.Task.ID)
	if err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	if !ok {
		return domain.ExecutionPlanRun{}, fmt.Errorf("parallel checkpoint not found: %s", preview.Task.ID)
	}
	return o.executeParallelPlan(ctx, checkpoint)
}

func (o *Orchestrator) ResumeExecutionPlan(ctx context.Context, sessionID string, rootTaskID string) (domain.ExecutionPlanRun, error) {
	checkpoint, ok, err := o.LoadParallelCheckpoint(ctx, sessionID, rootTaskID)
	if err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	if !ok {
		return domain.ExecutionPlanRun{}, fmt.Errorf("parallel checkpoint not found: %s", rootTaskID)
	}
	return o.executeParallelPlan(ctx, checkpoint)
}

func (o *Orchestrator) LoadParallelCheckpoint(ctx context.Context, sessionID string, rootTaskID string) (domain.ParallelPlanCheckpoint, bool, error) {
	if sessionID == "" || rootTaskID == "" {
		return domain.ParallelPlanCheckpoint{}, false, nil
	}
	if o.vfs != nil {
		record, ok, err := o.vfs.ReadCheckpoint(ctx, parallelPlanRuntimePath(rootTaskID))
		if err != nil {
			return domain.ParallelPlanCheckpoint{}, false, err
		}
		if ok {
			checkpoint := domain.ParallelPlanCheckpoint{}
			if err := decodeSessionState(record.Checkpoint, &checkpoint); err != nil {
				return domain.ParallelPlanCheckpoint{}, false, err
			}
			if checkpoint.RootTaskID == "" {
				checkpoint.RootTaskID = rootTaskID
			}
			if checkpoint.SessionID == "" {
				checkpoint.SessionID = sessionID
			}
			if checkpoint.Branch == "" {
				checkpoint.Branch = checkpointBranchName(rootTaskID)
			}
			checkpoint, err = o.hydrateParallelCheckpoint(ctx, checkpoint)
			if err != nil {
				return domain.ParallelPlanCheckpoint{}, false, err
			}
			return checkpoint, true, nil
		}
	}
	if o.store == nil {
		return domain.ParallelPlanCheckpoint{}, false, nil
	}
	record, ok, err := o.store.GetSessionState(ctx, sessionID, checkpointBranchName(rootTaskID))
	if err != nil || !ok {
		return domain.ParallelPlanCheckpoint{}, ok, err
	}
	checkpoint := domain.ParallelPlanCheckpoint{}
	if err := decodeSessionState(record.State, &checkpoint); err != nil {
		return domain.ParallelPlanCheckpoint{}, false, err
	}
	if checkpoint.RootTaskID == "" {
		checkpoint.RootTaskID = rootTaskID
	}
	if checkpoint.SessionID == "" {
		checkpoint.SessionID = sessionID
	}
	if checkpoint.Branch == "" {
		checkpoint.Branch = checkpointBranchName(rootTaskID)
	}
	checkpoint, err = o.hydrateParallelCheckpoint(ctx, checkpoint)
	if err != nil {
		return domain.ParallelPlanCheckpoint{}, false, err
	}
	return checkpoint, true, nil
}

func (o *Orchestrator) saveParallelCheckpoint(ctx context.Context, checkpoint domain.ParallelPlanCheckpoint) error {
	if o.store == nil && o.vfs == nil {
		return nil
	}
	payload := map[string]any{
		"kind":               checkpoint.Kind,
		"root_task_id":       checkpoint.RootTaskID,
		"session_id":         checkpoint.SessionID,
		"branch":             checkpoint.Branch,
		"pending_task_ids":   append([]string(nil), checkpoint.PendingTaskIDs...),
		"completed_task_ids": append([]string(nil), checkpoint.CompletedTaskIDs...),
		"results_by_task_id": checkpoint.ResultsByTaskID,
		"batch_no":           checkpoint.BatchNo,
		"status":             checkpoint.Status,
		"updated_at":         checkpoint.UpdatedAt,
	}
	if o.store != nil {
		if _, err := o.store.SaveSessionState(ctx, checkpoint.SessionID, checkpoint.Branch, payload, "parallel_plan", "checkpoint_runtime", nil); err != nil {
			return err
		}
	}
	if o.vfs != nil {
		_, err := o.vfs.WriteCheckpoint(ctx, domain.VFSCheckpointRecord{
			Path:       parallelPlanRuntimePath(checkpoint.RootTaskID),
			TaskID:     checkpoint.RootTaskID,
			AgentID:    "kernel",
			Checkpoint: payload,
			Integrity:  "ok",
			Metadata: map[string]any{
				"checkpoint_kind": "parallel_plan_runtime",
				"session_id":      checkpoint.SessionID,
				"branch":          checkpoint.Branch,
				"status":          checkpoint.Status,
				"batch_no":        checkpoint.BatchNo,
			},
		})
		return err
	}
	return nil
}

func (o *Orchestrator) saveParallelCheckpointStatic(ctx context.Context, checkpoint domain.ParallelPlanCheckpoint) error {
	if o.store == nil && o.vfs == nil {
		return nil
	}
	payload := map[string]any{
		"kind":          checkpoint.Kind,
		"root_task_id":  checkpoint.RootTaskID,
		"session_id":    checkpoint.SessionID,
		"branch":        checkpoint.Branch,
		"root_task":     checkpoint.RootTask,
		"plan":          checkpoint.Plan,
		"plan_artifact": checkpoint.PlanArtifact,
	}
	if o.store != nil {
		if _, err := o.store.SaveSessionState(ctx, checkpoint.SessionID, checkpointStaticBranchName(checkpoint.RootTaskID), payload, "parallel_plan", "checkpoint_static", nil); err != nil {
			return err
		}
	}
	if o.vfs != nil {
		_, err := o.vfs.WriteCheckpoint(ctx, domain.VFSCheckpointRecord{
			Path:       parallelPlanStaticPath(checkpoint.RootTaskID),
			TaskID:     checkpoint.RootTaskID,
			AgentID:    "kernel",
			Checkpoint: payload,
			Integrity:  "ok",
			Metadata: map[string]any{
				"checkpoint_kind": "parallel_plan_static",
				"session_id":      checkpoint.SessionID,
				"branch":          checkpoint.Branch,
			},
		})
		return err
	}
	return nil
}

func (o *Orchestrator) executeParallelPlan(ctx context.Context, checkpoint domain.ParallelPlanCheckpoint) (domain.ExecutionPlanRun, error) {
	if checkpoint.RootTaskID == "" {
		return domain.ExecutionPlanRun{}, fmt.Errorf("parallel checkpoint is missing root task id")
	}
	if checkpoint.SessionID == "" {
		checkpoint.SessionID = checkpoint.RootTask.SessionID
	}
	if checkpoint.SessionID == "" {
		checkpoint.SessionID = checkpoint.RootTaskID
	}
	if checkpoint.Branch == "" {
		checkpoint.Branch = checkpointBranchName(checkpoint.RootTaskID)
	}
	if checkpoint.RootTask.ID == "" {
		checkpoint.RootTask = domain.Task{ID: checkpoint.RootTaskID, SessionID: checkpoint.SessionID, Type: domain.TaskTypePlan}
	}
	if checkpoint.RootTask.SessionID == "" {
		checkpoint.RootTask.SessionID = checkpoint.SessionID
	}
	if checkpoint.RootTaskID == "" {
		checkpoint.RootTaskID = checkpoint.RootTask.ID
	}
	if checkpoint.RootTask.CreatedAt.IsZero() {
		checkpoint.RootTask.CreatedAt = time.Now().UTC()
	}
	if checkpoint.ResultsByTaskID == nil {
		checkpoint.ResultsByTaskID = map[string]any{}
	}
	if checkpoint.BatchNo <= 0 {
		checkpoint.BatchNo = 1
	}
	if err := o.saveParallelCheckpointStatic(ctx, checkpoint); err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	startedAt := time.Now().UTC()
	if checkpoint.Status == domain.ParallelPlanStatusCompleted {
		return domain.ExecutionPlanRun{
			Task:         checkpoint.RootTask,
			Plan:         checkpoint.Plan,
			PlanArtifact: checkpoint.PlanArtifact,
			Checkpoint:   checkpoint,
			StartedAt:    startedAt,
			CompletedAt:  checkpoint.UpdatedAt,
		}, nil
	}
	checkpoint.Status = domain.ParallelPlanStatusRunning
	checkpoint.UpdatedAt = startedAt
	if err := o.saveParallelCheckpoint(ctx, checkpoint); err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	run := domain.ExecutionPlanRun{
		Task:         checkpoint.RootTask,
		Plan:         checkpoint.Plan,
		PlanArtifact: checkpoint.PlanArtifact,
		StartedAt:    startedAt,
		Workflows:    []domain.WorkflowRecord{},
	}
	type planTaskResult struct {
		artifact domain.PlanTaskArtifact
		record   domain.WorkflowRecord
		err      error
	}
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan planTaskResult, len(checkpoint.PlanArtifact.Tasks))
	running := map[string]domain.PlanTaskArtifact{}
	for len(checkpoint.PendingTaskIDs) > 0 || len(running) > 0 {
		activeConflicts := collectArtifactConflictKeys(running)
		ready := scheduleReadyArtifactsWithConflicts(
			readyPlanArtifacts(checkpoint.PlanArtifact.Tasks, checkpoint.PendingTaskIDs, checkpoint.CompletedTaskIDs),
			activeConflicts,
		)
		for _, artifact := range ready {
			if _, ok := running[artifact.TaskID]; ok {
				continue
			}
			running[artifact.TaskID] = artifact
			batchNo := checkpoint.BatchNo
			go func(planned domain.PlanTaskArtifact, currentBatch int) {
				record, err := o.SubmitTask(runCtx, buildPlanTask(checkpoint.RootTask, planned, currentBatch))
				if err == nil && !isTerminalTaskStatus(record.Acceptance.Status) {
					record, err = o.WaitWorkflowTerminal(runCtx, record.Task.ID)
				}
				results <- planTaskResult{artifact: planned, record: record, err: err}
			}(artifact, batchNo)
		}
		if len(running) == 0 {
			checkpoint.Status = domain.ParallelPlanStatusBlocked
			checkpoint.UpdatedAt = time.Now().UTC()
			_ = o.saveParallelCheckpoint(ctx, checkpoint)
			return domain.ExecutionPlanRun{}, fmt.Errorf("parallel plan %s is blocked by unresolved dependencies", checkpoint.RootTaskID)
		}
		result := <-results
		delete(running, result.artifact.TaskID)
		if result.err != nil {
			cancel()
			checkpoint.ResultsByTaskID[result.artifact.TaskID] = map[string]any{
				"task_id": result.artifact.TaskID,
				"status":  "failed",
				"error":   result.err.Error(),
			}
			checkpoint.Status = domain.ParallelPlanStatusFailed
			checkpoint.UpdatedAt = time.Now().UTC()
			_ = o.saveParallelCheckpoint(ctx, checkpoint)
			return domain.ExecutionPlanRun{}, result.err
		}
		if err := validatePlanWorkflow(result.record); err != nil {
			cancel()
			checkpoint.ResultsByTaskID[result.artifact.TaskID] = map[string]any{
				"task_id": result.artifact.TaskID,
				"status":  "failed",
				"error":   err.Error(),
			}
			checkpoint.Status = domain.ParallelPlanStatusFailed
			checkpoint.UpdatedAt = time.Now().UTC()
			_ = o.saveParallelCheckpoint(ctx, checkpoint)
			return domain.ExecutionPlanRun{}, err
		}
		run.Workflows = append(run.Workflows, result.record)
		checkpoint.ResultsByTaskID[result.artifact.TaskID] = workflowSummary(result.record)
		checkpoint.PendingTaskIDs = removePending(checkpoint.PendingTaskIDs, result.artifact.TaskID)
		checkpoint.CompletedTaskIDs = append(checkpoint.CompletedTaskIDs, result.artifact.TaskID)
		checkpoint.BatchNo++
		checkpoint.UpdatedAt = time.Now().UTC()
		if err := o.saveParallelCheckpoint(ctx, checkpoint); err != nil {
			cancel()
			return domain.ExecutionPlanRun{}, err
		}
	}
	checkpoint.Status = domain.ParallelPlanStatusCompleted
	checkpoint.UpdatedAt = time.Now().UTC()
	if err := o.saveParallelCheckpoint(ctx, checkpoint); err != nil {
		return domain.ExecutionPlanRun{}, err
	}
	run.Checkpoint = checkpoint
	run.CompletedAt = checkpoint.UpdatedAt
	return run, nil
}

func validatePlanWorkflow(record domain.WorkflowRecord) error {
	if !isSuccessfulPlanStatus(record.Acceptance.Status) {
		reason := strings.TrimSpace(record.Acceptance.Reason)
		if reason == "" && record.Result != nil {
			reason = strings.TrimSpace(record.Result.Output.Summary)
		}
		reason = firstNonEmptyString(reason, "workflow did not reach a successful terminal status")
		return fmt.Errorf("plan task %s finished with status %s: %s", record.Task.ID, record.Acceptance.Status, reason)
	}
	if record.Result != nil && !isSuccessfulPlanStatus(record.Result.Status) {
		reason := strings.TrimSpace(record.Result.Output.Summary)
		reason = firstNonEmptyString(reason, "agent result did not reach a successful terminal status")
		return fmt.Errorf("plan task %s produced result status %s: %s", record.Task.ID, record.Result.Status, reason)
	}
	return nil
}

func isSuccessfulPlanStatus(status domain.TaskStatus) bool {
	return status == domain.TaskStatusCompleted || status == domain.TaskStatusDone
}

func buildPlanArtifact(task domain.Task, plan domain.ExecutionPlan) domain.PlanArtifact {
	tasks := make([]domain.PlanTaskArtifact, 0, len(plan.Steps))
	parallelGroups := parallelGroups(plan.Steps)
	handoffs := make([]map[string]any, 0, len(plan.Steps))
	for _, step := range plan.Steps {
		stepProvider, stepModel := stepAssignment(task, plan, step)
		weight := estimatePlanStepWeight(task, plan, step)
		workerClass := firstNonEmptyString(step.WorkerClass, workerClassForCapability(step.Capability))
		clusterID := firstNonEmptyString(step.ClusterID, step.ID)
		contextBudget := step.ContextBudget
		if contextBudget <= 0 {
			contextBudget = stepContextBudget(step.Capability, step.Files)
		}
		conflictKeys := append([]string(nil), step.ConflictKeys...)
		if len(conflictKeys) == 0 {
			conflictKeys = append(conflictKeys, step.Files...)
		}
		tasks = append(tasks, domain.PlanTaskArtifact{
			TaskID:        step.ID,
			Title:         step.Title,
			Capability:    step.Capability,
			WorkerClass:   workerClass,
			ClusterID:     clusterID,
			ContextBudget: contextBudget,
			ConflictKeys:  conflictKeys,
			Provider:      stepProvider,
			ModelName:     stepModel,
			Files:         append([]string(nil), step.Files...),
			Dependencies:  append([]string(nil), step.Dependencies...),
			BranchID:      clusterID,
			DraftLayer:    draftLayer(step),
			EstimatedCost: estimatePlanStepCost(task, plan, step),
			Weight:        weight,
			ExecutionContract: map[string]any{
				"root_task_id":        task.ID,
				"session_id":          task.SessionID,
				"step_id":             step.ID,
				"required_capability": step.Capability,
				"worker_class":        workerClass,
				"cluster_id":          clusterID,
				"context_budget":      contextBudget,
				"conflict_keys":       append([]string(nil), conflictKeys...),
				"selected_provider":   stepProvider,
				"selected_model":      stepModel,
				"review_depth":        task.ReviewDepth,
				"checkpoint_policy":   firstNonEmptyString(task.CheckpointPolicy, "on_plan_preview"),
				"resume_token":        firstNonEmptyString(task.ResumeToken, step.ID),
				"repo_fingerprint":    task.RepoFingerprint,
				"acceptance_criteria": append([]string(nil), task.Input.AcceptanceCriteria...),
			},
		})
		for _, dependency := range step.Dependencies {
			handoffs = append(handoffs, map[string]any{
				"from": dependency,
				"to":   step.ID,
				"kind": "dependency",
			})
		}
	}
	return domain.PlanArtifact{
		RootTaskID:        task.ID,
		PrimaryCapability: plan.PrimaryCapability,
		TaskCount:         len(tasks),
		Tasks:             tasks,
		ParallelGroups:    parallelGroups,
		Handoffs:          handoffs,
		CreatedAt:         time.Now().UTC(),
	}
}

func stepAssignment(task domain.Task, plan domain.ExecutionPlan, step domain.PlanStep) (string, string) {
	if shouldUseDynamicStepRouting(step) {
		return "", ""
	}
	return firstNonEmptyString(task.AssignedProvider, plan.Selection.Provider), firstNonEmptyString(task.AssignedModel, plan.Selection.ModelName)
}

func shouldUseDynamicStepRouting(step domain.PlanStep) bool {
	switch strings.ToLower(strings.TrimSpace(step.Capability)) {
	case "plan", "analysis":
		return true
	default:
		return len(step.Dependencies) == 0
	}
}

func checkpointBranchName(rootTaskID string) string {
	return "parallel_plan:" + rootTaskID
}

func checkpointStaticBranchName(rootTaskID string) string {
	return "parallel_plan_static:" + rootTaskID
}

func parallelPlanRuntimePath(rootTaskID string) string {
	return "parallel_plan/" + rootTaskID
}

func parallelPlanStaticPath(rootTaskID string) string {
	return "parallel_plan_static/" + rootTaskID
}

func (o *Orchestrator) hydrateParallelCheckpoint(ctx context.Context, checkpoint domain.ParallelPlanCheckpoint) (domain.ParallelPlanCheckpoint, error) {
	if checkpoint.RootTask.ID != "" && len(checkpoint.PlanArtifact.Tasks) > 0 {
		return checkpoint, nil
	}
	staticState, ok, err := o.loadParallelCheckpointStatic(ctx, checkpoint.SessionID, checkpoint.RootTaskID)
	if err != nil || !ok {
		return checkpoint, err
	}
	if checkpoint.RootTask.ID == "" {
		checkpoint.RootTask = staticState.RootTask
	}
	if checkpoint.Plan.TaskID == "" && len(checkpoint.Plan.Steps) == 0 {
		checkpoint.Plan = staticState.Plan
	}
	if len(checkpoint.PlanArtifact.Tasks) == 0 {
		checkpoint.PlanArtifact = staticState.PlanArtifact
	}
	return checkpoint, nil
}

func (o *Orchestrator) loadParallelCheckpointStatic(ctx context.Context, sessionID string, rootTaskID string) (domain.ParallelPlanCheckpoint, bool, error) {
	if sessionID == "" || rootTaskID == "" {
		return domain.ParallelPlanCheckpoint{}, false, nil
	}
	if o.vfs != nil {
		record, ok, err := o.vfs.ReadCheckpoint(ctx, parallelPlanStaticPath(rootTaskID))
		if err != nil {
			return domain.ParallelPlanCheckpoint{}, false, err
		}
		if ok {
			checkpoint := domain.ParallelPlanCheckpoint{}
			if err := decodeSessionState(record.Checkpoint, &checkpoint); err != nil {
				return domain.ParallelPlanCheckpoint{}, false, err
			}
			return checkpoint, true, nil
		}
	}
	if o.store == nil {
		return domain.ParallelPlanCheckpoint{}, false, nil
	}
	record, ok, err := o.store.GetSessionState(ctx, sessionID, checkpointStaticBranchName(rootTaskID))
	if err != nil || !ok {
		return domain.ParallelPlanCheckpoint{}, ok, err
	}
	checkpoint := domain.ParallelPlanCheckpoint{}
	if err := decodeSessionState(record.State, &checkpoint); err != nil {
		return domain.ParallelPlanCheckpoint{}, false, err
	}
	return checkpoint, true, nil
}

func decodeSessionState(state map[string]any, target any) error {
	encoded, err := json.Marshal(state)
	if err != nil {
		return err
	}
	return json.Unmarshal(encoded, target)
}

func parallelGroups(steps []domain.PlanStep) [][]string {
	groups := map[string][]string{}
	for _, step := range steps {
		if len(step.Dependencies) != 1 {
			continue
		}
		key := step.Dependencies[0]
		groups[key] = append(groups[key], step.ID)
	}
	keys := make([]string, 0, len(groups))
	for key, items := range groups {
		if len(items) > 1 {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	out := make([][]string, 0, len(keys))
	for _, key := range keys {
		group := append([]string(nil), groups[key]...)
		sort.Strings(group)
		out = append(out, group)
	}
	return out
}

func draftLayer(step domain.PlanStep) string {
	if len(step.Dependencies) == 0 {
		return "analysis"
	}
	if step.Capability == "review" || step.Capability == "docs" {
		return "review"
	}
	if strings.Contains(step.ID, "test") {
		return "verification"
	}
	if strings.Contains(step.ID, "branch-") {
		return "parallel"
	}
	return "execution"
}

func estimatePlanStepCost(task domain.Task, plan domain.ExecutionPlan, step domain.PlanStep) float64 {
	base := 1.0
	switch plan.Complexity {
	case domain.ComplexityMedium:
		base = 1.5
	case domain.ComplexityHigh:
		base = 2.5
	case domain.ComplexityCritical:
		base = 4
	}
	base += float64(len(step.Files)) * 0.35
	base += float64(len(step.Dependencies)) * 0.25
	base += float64(len(task.Input.AcceptanceCriteria)) * 0.15
	if step.Capability == "review" || step.Capability == "test" {
		base += 0.5
	}
	return base
}

func estimatePlanStepWeight(task domain.Task, plan domain.ExecutionPlan, step domain.PlanStep) float64 {
	weight := estimatePlanStepCost(task, plan, step)
	weight += float64(len(step.Files)) * 0.4
	weight += float64(len(step.Dependencies)) * 0.2
	if step.Capability == "review" || step.Capability == "test" {
		weight += 0.35
	}
	if plan.Complexity == domain.ComplexityCritical {
		weight += 0.75
	}
	return weight
}

func readyPlanArtifacts(tasks []domain.PlanTaskArtifact, pending []string, completed []string) []domain.PlanTaskArtifact {
	pendingSet := completedSet(pending)
	completedSet := completedSet(completed)
	ready := make([]domain.PlanTaskArtifact, 0, len(tasks))
	for _, task := range tasks {
		if _, ok := pendingSet[task.TaskID]; !ok {
			continue
		}
		allDepsDone := true
		for _, dependency := range task.Dependencies {
			if _, ok := completedSet[dependency]; !ok {
				allDepsDone = false
				break
			}
		}
		if allDepsDone {
			ready = append(ready, task)
		}
	}
	sort.Slice(ready, func(i, j int) bool {
		return ready[i].TaskID < ready[j].TaskID
	})
	return ready
}

func scheduleReadyArtifacts(ready []domain.PlanTaskArtifact) []domain.PlanTaskArtifact {
	return scheduleReadyArtifactsWithConflicts(ready, nil)
}

func scheduleReadyArtifactsWithConflicts(ready []domain.PlanTaskArtifact, activeConflicts map[string]struct{}) []domain.PlanTaskArtifact {
	if len(ready) <= 1 {
		if len(ready) == 1 && hasConflictKeys(ready[0], activeConflicts) {
			return nil
		}
		return ready
	}
	sorted := append([]domain.PlanTaskArtifact(nil), ready...)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].Weight == sorted[j].Weight {
			if len(sorted[i].Files) == len(sorted[j].Files) {
				return sorted[i].TaskID < sorted[j].TaskID
			}
			return len(sorted[i].Files) < len(sorted[j].Files)
		}
		return sorted[i].Weight < sorted[j].Weight
	})
	scheduled := make([]domain.PlanTaskArtifact, 0, len(sorted))
	usedConflicts := map[string]struct{}{}
	for key := range activeConflicts {
		usedConflicts[key] = struct{}{}
	}
	for _, task := range sorted {
		if hasConflictKeys(task, usedConflicts) {
			continue
		}
		scheduled = append(scheduled, task)
		registerConflictKeys(task, usedConflicts)
	}
	if len(scheduled) == 0 {
		return nil
	}
	return scheduled
}

func collectArtifactConflictKeys(running map[string]domain.PlanTaskArtifact) map[string]struct{} {
	if len(running) == 0 {
		return nil
	}
	used := make(map[string]struct{}, len(running)*2)
	for _, task := range running {
		registerConflictKeys(task, used)
	}
	return used
}

func hasConflictKeys(task domain.PlanTaskArtifact, used map[string]struct{}) bool {
	for _, key := range task.ConflictKeys {
		trimmed := strings.TrimSpace(key)
		if trimmed == "" {
			continue
		}
		if _, ok := used[trimmed]; ok {
			return true
		}
	}
	return false
}

func registerConflictKeys(task domain.PlanTaskArtifact, used map[string]struct{}) {
	for _, key := range task.ConflictKeys {
		trimmed := strings.TrimSpace(key)
		if trimmed == "" {
			continue
		}
		used[trimmed] = struct{}{}
	}
}

func removePending(pending []string, taskID string) []string {
	if len(pending) == 0 {
		return pending
	}
	out := make([]string, 0, len(pending))
	for _, candidate := range pending {
		if candidate == taskID {
			continue
		}
		out = append(out, candidate)
	}
	return out
}

func completedSet(ids []string) map[string]struct{} {
	out := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		trimmed := strings.TrimSpace(id)
		if trimmed == "" {
			continue
		}
		out[trimmed] = struct{}{}
	}
	return out
}

func buildPlanTask(root domain.Task, artifact domain.PlanTaskArtifact, batchNo int) domain.Task {
	task := root
	task.ID = artifact.TaskID
	task.SessionID = firstNonEmptyString(root.SessionID, root.ID)
	task.ParentTaskID = root.ID
	task.Type = taskTypeForCapability(root.Type, artifact.Capability)
	task.RequiredCapability = artifact.Capability
	task.AssignedProvider = artifact.Provider
	task.AssignedModel = artifact.ModelName
	task.Input.Description = artifact.Title
	task.Input.Files = append([]string(nil), artifact.Files...)
	task.Dependencies = append([]string(nil), artifact.Dependencies...)
	task.BranchID = firstNonEmptyString(artifact.BranchID, artifact.TaskID)
	task.DraftLayer = artifact.DraftLayer
	task.EstimatedCost = artifact.EstimatedCost
	task.ExecutionContract = cloneMap(artifact.ExecutionContract)
	if task.ExecutionContract == nil {
		task.ExecutionContract = map[string]any{}
	}
	task.ExecutionContract["worker_class"] = artifact.WorkerClass
	task.ExecutionContract["cluster_id"] = artifact.ClusterID
	task.ExecutionContract["context_budget"] = artifact.ContextBudget
	task.ExecutionContract["conflict_keys"] = append([]string(nil), artifact.ConflictKeys...)
	task.ExecutionContract["task_weight"] = artifact.Weight
	task.ResumeToken = firstNonEmptyString(root.ResumeToken, artifact.TaskID)
	task.RoutingHints = cloneMap(root.RoutingHints)
	if task.RoutingHints == nil {
		task.RoutingHints = map[string]any{}
	}
	task.RoutingHints["root_task_id"] = root.ID
	task.RoutingHints["plan_step_id"] = artifact.TaskID
	task.RoutingHints["plan_batch_no"] = batchNo
	task.RoutingHints["parallel_plan"] = true
	task.RoutingHints["worker_class"] = artifact.WorkerClass
	task.RoutingHints["cluster_id"] = artifact.ClusterID
	task.RoutingHints["context_budget"] = artifact.ContextBudget
	task.RoutingHints["conflict_keys"] = append([]string(nil), artifact.ConflictKeys...)
	task.RoutingHints["task_weight"] = artifact.Weight
	task.ExecutionContract["root_task_id"] = root.ID
	task.ExecutionContract["session_id"] = task.SessionID
	task.ExecutionContract["step_id"] = artifact.TaskID
	task.ExecutionContract["plan_batch_no"] = batchNo
	task.ExecutionContract["parallel_plan"] = true
	return task
}

func taskTypeForCapability(rootType domain.TaskType, capability string) domain.TaskType {
	switch strings.ToLower(strings.TrimSpace(capability)) {
	case "plan", "analysis":
		return domain.TaskTypePlan
	case "code", "sourcecraft":
		return domain.TaskTypeCode
	case "review", "security":
		return domain.TaskTypeReview
	case "test":
		return domain.TaskTypeTest
	case "docs":
		return domain.TaskTypeDocs
	case "fix":
		return domain.TaskTypeFix
	case "research":
		return domain.TaskTypeResearch
	default:
		if rootType != "" {
			return rootType
		}
		return domain.TaskTypePlan
	}
}

func workflowSummary(record domain.WorkflowRecord) map[string]any {
	summary := map[string]any{
		"task_id":      record.Task.ID,
		"status":       record.Acceptance.Status,
		"agent_id":     record.Acceptance.AgentID,
		"provider":     record.Acceptance.Provider,
		"model_name":   record.Acceptance.ModelName,
		"capability":   record.Acceptance.Capability,
		"reason":       record.Acceptance.Reason,
		"updated_at":   record.UpdatedAt,
		"dependencies": append([]string(nil), record.Task.Dependencies...),
	}
	if record.Result != nil {
		summary["result_status"] = record.Result.Status
		summary["confidence"] = record.Result.Confidence
		summary["files_changed"] = append([]string(nil), record.Result.Output.FilesChanged...)
		summary["commands_run"] = append([]string(nil), record.Result.Output.CommandsRun...)
		summary["summary"] = record.Result.Output.Summary
		if len(record.Result.Errors) > 0 {
			summary["errors"] = append([]string(nil), record.Result.Errors...)
		}
	}
	return summary
}
