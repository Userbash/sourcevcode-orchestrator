package kernel

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/delivery"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type budgetTestAgent struct {
	info          domain.AgentInfo
	executedTasks []domain.Task
	result        domain.AgentResult
}

func (a *budgetTestAgent) Info() domain.AgentInfo {
	return a.info
}

func (a *budgetTestAgent) CanAccept(domain.Task) bool {
	return true
}

func (a *budgetTestAgent) Execute(_ context.Context, task domain.Task) domain.AgentResult {
	a.executedTasks = append(a.executedTasks, task)
	result := a.result
	result.TaskID = task.ID
	result.AgentID = a.info.ID
	result.Provider = a.info.Provider
	result.ModelName = a.info.ModelName
	if result.Status == "" {
		result.Status = domain.TaskStatusDone
	}
	if result.CompletedAt.IsZero() {
		result.CompletedAt = time.Now().UTC()
	}
	if result.Output.Artifacts == nil {
		result.Output.Artifacts = map[string]any{}
	}
	return result
}

func newBudgetTestOrchestrator(t *testing.T) (*Orchestrator, state.Store, *Registry) {
	t.Helper()
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	if strings.TrimSpace(os.Getenv("GO_CORE_SUBMIT_MODE")) == "" {
		t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	}
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	orchestrator := NewOrchestrator(registry, planner, router, store, realtime.NewHub("runtime", 32), realtime.NewHub("inventory", 16), nil)
	return orchestrator, store, registry
}

func TestOrchestratorPeerFailoverRoutesToNextAgent(t *testing.T) {
	orchestrator, _, registry := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	failingAgent := &budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-openai", Type: "coding", Provider: "openai", ModelName: "gpt-5.5",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Status: domain.TaskStatusFailed, Errors: []string{"provider timeout"}, Output: domain.ResultOutput{Summary: "provider timeout"}}}
	successAgent := &budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-local", Type: "coding", Provider: "local", ModelName: "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Output: domain.ResultOutput{Summary: "local recovered", Artifacts: map[string]any{
		"usage": map[string]any{"total_tokens": 21},
	}}}}
	registry.RegisterAgent(failingAgent)
	registry.RegisterAgent(successAgent)

	record, err := orchestrator.SubmitTask(ctx, domain.Task{
		ID:               "task-peer-failover",
		SessionID:        "session-peer-failover",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "openai",
		AssignedModel:    "gpt-5.5",
		Input:            domain.TaskInput{Description: "Implement failover when the preferred agent crashes.", AcceptanceCriteria: []string{"fallback succeeds"}},
		Context:          domain.TaskContext{Branch: "main", Project: "demo"},
	})
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Result == nil {
		t.Fatal("Result = nil, want completed result")
	}
	if record.Result.Status != domain.TaskStatusDone {
		t.Fatalf("Result.Status = %s, want done", record.Result.Status)
	}
	if record.Acceptance.AgentID != "coder-local" {
		t.Fatalf("Acceptance.AgentID = %s, want coder-local", record.Acceptance.AgentID)
	}
	if len(failingAgent.executedTasks) != 1 {
		t.Fatalf("failing agent executed %d tasks, want 1", len(failingAgent.executedTasks))
	}
	if len(successAgent.executedTasks) != 1 {
		t.Fatalf("success agent executed %d tasks, want 1", len(successAgent.executedTasks))
	}
	if successAgent.executedTasks[0].RoutingHints["p2p_attempt"] != 2 {
		t.Fatalf("p2p_attempt = %v, want 2", successAgent.executedTasks[0].RoutingHints["p2p_attempt"])
	}
	failures, ok := successAgent.executedTasks[0].RoutingHints["peer_failures"].([]map[string]any)
	if !ok || len(failures) != 1 {
		t.Fatalf("peer_failures = %#v, want one failure context", successAgent.executedTasks[0].RoutingHints["peer_failures"])
	}
	if failures[0]["agent_id"] != "coder-openai" {
		t.Fatalf("peer_failures[0].agent_id = %v, want coder-openai", failures[0]["agent_id"])
	}
}

func TestOrchestratorBudgetFallbackRoutesToNextProvider(t *testing.T) {
	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	_, err := store.SaveSessionState(ctx, "session-budget", "main", map[string]any{
		"model_usage": map[string]any{
			"gpt-5.5": 999960,
		},
	}, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}

	openaiAgent := &budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-openai", Type: "coding", Provider: "openai", ModelName: "gpt-5.5",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}}
	localAgent := &budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-local", Type: "coding", Provider: "local", ModelName: "qwen2.5:32b-instruct-q4_k_m",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}, result: domain.AgentResult{Output: domain.ResultOutput{Summary: "local ok", Artifacts: map[string]any{
		"usage": map[string]any{"total_tokens": 33},
	}}}}
	registry.RegisterAgent(openaiAgent)
	registry.RegisterAgent(localAgent)

	record, err := orchestrator.SubmitTask(ctx, domain.Task{
		ID:               "task-budget-fallback",
		SessionID:        "session-budget",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "openai",
		AssignedModel:    "gpt-5.5",
		Input:            domain.TaskInput{Description: "Implement a moderate refactor touching several files to exceed tiny token estimates."},
		Context:          domain.TaskContext{Branch: "main"},
	})
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Acceptance.Provider != "local" {
		t.Fatalf("Acceptance.Provider = %s, want local", record.Acceptance.Provider)
	}
	if len(openaiAgent.executedTasks) != 0 {
		t.Fatalf("openai executed %d tasks, want 0", len(openaiAgent.executedTasks))
	}
	if len(localAgent.executedTasks) != 1 {
		t.Fatalf("local executed %d tasks, want 1", len(localAgent.executedTasks))
	}
	budget, ok := localAgent.executedTasks[0].RoutingHints["model_budget"].(map[string]any)
	if !ok {
		t.Fatalf("task routing hint model_budget type = %T", localAgent.executedTasks[0].RoutingHints["model_budget"])
	}
	if budget["action"] != "ok" {
		t.Fatalf("fallback model_budget.action = %v, want ok", budget["action"])
	}
}

func TestOrchestratorBudgetFailureReturnsTokenBudgetArtifact(t *testing.T) {
	orchestrator, store, registry := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	_, err := store.SaveSessionState(ctx, "session-budget-fail", "main", map[string]any{
		"model_usage": map[string]any{
			"gpt-5.5": 999960,
		},
	}, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}

	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-openai", Type: "coding", Provider: "openai", ModelName: "gpt-5.5",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}})

	record, err := orchestrator.SubmitTask(ctx, domain.Task{
		ID:               "task-budget-fail",
		SessionID:        "session-budget-fail",
		Type:             domain.TaskTypeCode,
		AssignedProvider: "openai",
		AssignedModel:    "gpt-5.5",
		Input:            domain.TaskInput{Description: "Implement a moderate refactor touching several files to exceed tiny token estimates."},
		Context:          domain.TaskContext{Branch: "main"},
	})
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if record.Result == nil {
		t.Fatal("Result = nil, want failed result")
	}
	if record.Acceptance.Status != domain.TaskStatusAccepted {
		t.Fatalf("Acceptance.Status = %s, want accepted", record.Acceptance.Status)
	}
	if record.Result.Status != domain.TaskStatusFailed {
		t.Fatalf("Result.Status = %s, want failed", record.Result.Status)
	}
	if _, ok := record.Result.Output.Artifacts["token_budget"]; !ok {
		t.Fatalf("token_budget artifact missing: %#v", record.Result.Output.Artifacts)
	}
}

func TestSubmissionSchedulerBalancesSessionsAndPriorities(t *testing.T) {
	scheduler := newSubmissionScheduler(16, 1)
	ctx := context.Background()
	now := time.Now().Add(-2 * time.Second)
	mustEnqueue := func(task domain.Task) {
		t.Helper()
		if err := scheduler.enqueue(ctx, scheduledSubmission{
			delivery: delivery.TaskDelivery{Task: task},
			groupKey: submissionGroupKey(task),
			enqueued: now,
		}); err != nil {
			t.Fatalf("enqueue(%s) error = %v", task.ID, err)
		}
	}

	mustEnqueue(domain.Task{ID: "a-1", SessionID: "session-a", Priority: domain.PriorityNormal, Complexity: domain.ComplexityHigh})
	mustEnqueue(domain.Task{ID: "a-2", SessionID: "session-a", Priority: domain.PriorityNormal, Complexity: domain.ComplexityHigh})
	mustEnqueue(domain.Task{ID: "b-1", SessionID: "session-b", Priority: domain.PriorityHigh, Complexity: domain.ComplexityLow})
	mustEnqueue(domain.Task{ID: "c-1", SessionID: "session-c", Priority: domain.PriorityLow, Complexity: domain.ComplexityLow})

	first, ok, err := scheduler.next(ctx)
	if err != nil || !ok {
		t.Fatalf("first next error = %v ok = %v", err, ok)
	}
	if first.delivery.Task.ID != "b-1" {
		t.Fatalf("first task = %s, want b-1", first.delivery.Task.ID)
	}

	second, ok, err := scheduler.next(ctx)
	if err != nil || !ok {
		t.Fatalf("second next error = %v ok = %v", err, ok)
	}
	if second.delivery.Task.ID == "a-2" {
		t.Fatalf("second task = %s, want different session while session-a already in flight", second.delivery.Task.ID)
	}

	scheduler.done(first.groupKey)
	third, ok, err := scheduler.next(ctx)
	if err != nil || !ok {
		t.Fatalf("third next error = %v ok = %v", err, ok)
	}
	if third.delivery.Task.SessionID == second.delivery.Task.SessionID {
		t.Fatalf("third session = %s, want fair rotation away from session %s", third.delivery.Task.SessionID, second.delivery.Task.SessionID)
	}
}

var _ agents.Agent = (*budgetTestAgent)(nil)

func TestOrchestratorCacheGuardBlockedReturnsRejectedResult(t *testing.T) {
	orchestrator, _, registry := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	agent := &budgetTestAgent{info: domain.AgentInfo{
		ID: "coder-openai", Type: "coding", Provider: "openai", ModelName: "gpt-5.5",
		Capabilities: []string{"code", "fix", "test"}, Status: domain.AgentStatusReady,
	}}
	registry.RegisterAgent(agent)

	blockedTask := domain.Task{
		ID:        "task-cache-guard",
		SessionID: "session-cache-guard",
		Type:      domain.TaskTypeCode,
		Input:     domain.TaskInput{Description: "This task should be blocked by cache guard."},
		Context:   domain.TaskContext{Branch: "main"},
	}
	if err := orchestrator.memory.RecordHardStop(ctx, blockedTask, "agent_execute", "provider failure"); err != nil {
		t.Fatalf("RecordHardStop() error = %v", err)
	}

	record, err := orchestrator.SubmitTask(ctx, blockedTask)
	if err != nil {
		t.Fatalf("SubmitTask() error = %v", err)
	}
	if len(agent.executedTasks) != 0 {
		t.Fatalf("agent executed %d tasks, want 0", len(agent.executedTasks))
	}
	if record.Result == nil {
		t.Fatal("Result = nil, want rejected cache-guard result")
	}
	if record.Acceptance.Status != domain.TaskStatusRejected {
		t.Fatalf("Acceptance.Status = %s, want rejected", record.Acceptance.Status)
	}
	if record.Result.Status != domain.TaskStatusRejected {
		t.Fatalf("Result.Status = %s, want rejected", record.Result.Status)
	}
	if !strings.Contains(record.Result.Output.Summary, "cache guard") {
		t.Fatalf("Result.Output.Summary = %q, want cache guard summary", record.Result.Output.Summary)
	}
	if _, ok := record.Result.Output.Artifacts["cache_guard"]; !ok {
		t.Fatalf("cache_guard artifact missing: %#v", record.Result.Output.Artifacts)
	}
}

func TestOrchestratorInjectsVectorMemoryIntoRuntimeContext(t *testing.T) {
	orchestrator, _, _ := newBudgetTestOrchestrator(t)
	ctx := context.Background()

	if err := orchestrator.memory.RecordPromptInput(ctx, domain.Task{
		ID:        "seed-task",
		SessionID: "session-vector",
		Type:      domain.TaskTypeCode,
		Input:     domain.TaskInput{Description: "Use JWT middleware and hybrid vector retrieval for auth prompts"},
		Context:   domain.TaskContext{Project: "demo", Branch: "main"},
	}, "seed-agent"); err != nil {
		t.Fatalf("RecordPromptInput() error = %v", err)
	}

	task := domain.Task{
		ID:        "task-vector-runtime",
		SessionID: "session-vector",
		Type:      domain.TaskTypeCode,
		Input:     domain.TaskInput{Description: "Build augmented prompt using vector retrieval for auth flow"},
		Context:   domain.TaskContext{Project: "demo", Branch: "main"},
	}
	acceptance := domain.TaskAcceptance{Provider: "local", ModelName: "qwen2.5:32b-instruct-q4_k_m"}
	agent := domain.AgentInfo{ID: "coder-local", Provider: "local", ModelName: "qwen2.5:32b-instruct-q4_k_m"}

	withContext := orchestrator.attachRuntimeContext(ctx, task, acceptance, agent)
	memoryContext, ok := withContext.RoutingHints["memory_context"].(map[string]any)
	if !ok {
		t.Fatalf("memory_context type = %T", withContext.RoutingHints["memory_context"])
	}
	if strings.TrimSpace(fmt.Sprint(memoryContext["vector_memory_brief"])) == "" {
		t.Fatalf("vector_memory_brief empty in memory_context: %#v", memoryContext)
	}
	if !strings.Contains(fmt.Sprint(memoryContext["augmented_prompt"]), "[CONTEXT]") {
		t.Fatalf("augmented_prompt = %v, want [CONTEXT] block", memoryContext["augmented_prompt"])
	}
}
