package kernel

import (
	"context"
	"errors"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/delivery"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestFirstIntEnvSkipsInvalidValues(t *testing.T) {
	t.Setenv("TEST_INVALID_INT", "bad")
	t.Setenv("TEST_VALID_INT", " 7 ")

	value, ok := firstIntEnv("TEST_INVALID_INT", "TEST_VALID_INT")
	if !ok {
		t.Fatal("firstIntEnv() ok = false, want true")
	}
	if value != 7 {
		t.Fatalf("firstIntEnv() = %d, want 7", value)
	}

	if _, ok := firstIntEnv("TEST_MISSING_INT"); ok {
		t.Fatal("firstIntEnv() ok = true for missing key, want false")
	}
}

func TestDetectExecutionProfileRespectsOverridesAndMetadata(t *testing.T) {
	previous := runtime.GOMAXPROCS(0)
	defer runtime.GOMAXPROCS(previous)

	t.Setenv("AI_BRIDGE_GOMAXPROCS", "4")
	t.Setenv("AI_BRIDGE_CPU_RESERVE", "99")
	t.Setenv("GO_CORE_MAX_PARALLELISM", "")

	profile := detectExecutionProfile()
	if profile.MaxProcs != 4 {
		t.Fatalf("MaxProcs = %d, want 4", profile.MaxProcs)
	}
	if profile.ReservedProcs != 3 {
		t.Fatalf("ReservedProcs = %d, want 3", profile.ReservedProcs)
	}
	if profile.UsableParallelism != 1 {
		t.Fatalf("UsableParallelism = %d, want 1", profile.UsableParallelism)
	}
	metadata := profile.Metadata()
	if metadata["gomaxprocs"] != 4 {
		t.Fatalf("metadata gomaxprocs = %v, want 4", metadata["gomaxprocs"])
	}
	if metadata["reserved_procs"] != 3 {
		t.Fatalf("metadata reserved_procs = %v, want 3", metadata["reserved_procs"])
	}
	if metadata["usable_parallelism"] != 1 {
		t.Fatalf("metadata usable_parallelism = %v, want 1", metadata["usable_parallelism"])
	}
}

func TestNewTaskIDUsesRandomAndFallbackPaths(t *testing.T) {
	originalRandRead := taskIDRandRead
	originalNowUnixNano := taskIDNowUnixNano
	defer func() {
		taskIDRandRead = originalRandRead
		taskIDNowUnixNano = originalNowUnixNano
	}()

	taskIDRandRead = func(buf []byte) (int, error) {
		for i := range buf {
			buf[i] = byte(i + 1)
		}
		return len(buf), nil
	}
	if got := newTaskID(); got != "task_0102030405060708" {
		t.Fatalf("newTaskID() random path = %q, want %q", got, "task_0102030405060708")
	}

	taskIDRandRead = func([]byte) (int, error) {
		return 0, errors.New("entropy unavailable")
	}
	taskIDNowUnixNano = func() int64 { return 42 }
	if got := newTaskID(); got != "task_42" {
		t.Fatalf("newTaskID() fallback path = %q, want %q", got, "task_42")
	}
}

func TestPolicyDecisionsCoverRejectAndAllowBranches(t *testing.T) {
	if decision := preflightPolicy(domain.Task{}); decision.Decision != "reject" || !decisionBlocks(decision) {
		t.Fatalf("preflightPolicy(empty) = %+v, want reject", decision)
	}
	if decision := preflightPolicy(domain.Task{Type: domain.TaskTypeDocs}); decision.Reasons[0] != "task description is required" {
		t.Fatalf("preflightPolicy(no description) = %+v", decision)
	}
	allow := preflightPolicy(domain.Task{Type: domain.TaskTypeDocs, Input: domain.TaskInput{Description: "write docs"}})
	if allow.Decision != "allow" || decisionBlocks(allow) {
		t.Fatalf("preflightPolicy(allow) = %+v, want allow", allow)
	}

	acceptedAt := time.Now().UTC()
	suppressedUntil := acceptedAt.Add(time.Minute)
	reject := assignmentPolicy(
		domain.Task{AssignedProvider: "openai"},
		domain.TaskAcceptance{AcceptedAt: acceptedAt},
		domain.AgentInfo{ID: "agent-1", Provider: "local"},
		domain.AgentRuntimeState{Status: domain.AgentStatusMaintenance, SuppressedUntil: &suppressedUntil},
	)
	if reject.Decision != "reject" {
		t.Fatalf("assignmentPolicy(reject) decision = %q, want reject", reject.Decision)
	}
	if len(reject.Reasons) != 3 {
		t.Fatalf("assignmentPolicy(reject) reasons = %v, want 3 reasons", reject.Reasons)
	}
	if reject.AgentID != "agent-1" {
		t.Fatalf("assignmentPolicy(reject) agent_id = %q, want agent-1", reject.AgentID)
	}

	allowed := assignmentPolicy(
		domain.Task{},
		domain.TaskAcceptance{AcceptedAt: acceptedAt},
		domain.AgentInfo{ID: "agent-2", Provider: "local"},
		domain.AgentRuntimeState{Status: domain.AgentStatusReady},
	)
	if allowed.Decision != "allow" || allowed.NextAction != "execute" {
		t.Fatalf("assignmentPolicy(allow) = %+v, want allow/execute", allowed)
	}
	if !decisionBlocks(domain.PolicyDecision{Decision: "block"}) {
		t.Fatal("decisionBlocks(block) = false, want true")
	}
}

func TestOrchestratorHelperDefaultsAndNilSafety(t *testing.T) {
	if got := (*Orchestrator)(nil).ExecutionProfile(); len(got) != 0 {
		t.Fatalf("nil ExecutionProfile() = %v, want empty", got)
	}
	if defaultGlobalConcurrency(0) != 8 {
		t.Fatalf("defaultGlobalConcurrency(0) = %d, want 8", defaultGlobalConcurrency(0))
	}
	if defaultPerAgentConcurrency(2) != 1 || defaultPerAgentConcurrency(4) != 2 || defaultPerAgentConcurrency(16) != 4 {
		t.Fatalf("defaultPerAgentConcurrency thresholds changed unexpectedly")
	}
	if defaultPerModelConcurrency(4) != 1 || defaultPerModelConcurrency(5) != 2 {
		t.Fatalf("defaultPerModelConcurrency thresholds changed unexpectedly")
	}
	if defaultSubmitWorkers(2) != 1 || defaultSubmitWorkers(5) != 2 || defaultSubmitWorkers(16) != 4 {
		t.Fatalf("defaultSubmitWorkers thresholds changed unexpectedly")
	}
	if defaultResultWorkers(4) != 1 || defaultResultWorkers(5) != 2 {
		t.Fatalf("defaultResultWorkers thresholds changed unexpectedly")
	}
	if defaultAgentWorkers(2) != 1 || defaultAgentWorkers(6) != 2 || defaultAgentWorkers(16) != 4 {
		t.Fatalf("defaultAgentWorkers thresholds changed unexpectedly")
	}

	t.Setenv("GO_CORE_AGENT_POLL_INTERVAL_MS", "1")
	if got := agentPollInterval(); got != 50*time.Millisecond {
		t.Fatalf("agentPollInterval() = %s, want 50ms floor", got)
	}
}

func TestSubmissionSchedulerContextAndGroupingHelpers(t *testing.T) {
	scheduler := newSubmissionScheduler(0, 0)
	if scheduler.maxBuffered != 16 {
		t.Fatalf("maxBuffered = %d, want 16", scheduler.maxBuffered)
	}
	if scheduler.maxInFlightPerGroup != 1 {
		t.Fatalf("maxInFlightPerGroup = %d, want 1", scheduler.maxInFlightPerGroup)
	}

	cancelledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, ok, err := scheduler.next(cancelledCtx); !errors.Is(err, context.Canceled) || ok {
		t.Fatalf("next(cancelled) = ok:%v err:%v, want context.Canceled", ok, err)
	}

	fullScheduler := newSubmissionScheduler(1, 1)
	first := scheduledSubmission{groupKey: "group-a"}
	if err := fullScheduler.enqueue(context.Background(), first); err != nil {
		t.Fatalf("enqueue(first) error = %v", err)
	}
	ctx, stop := context.WithCancel(context.Background())
	stop()
	if err := fullScheduler.enqueue(ctx, scheduledSubmission{groupKey: "group-b"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("enqueue(cancelled) error = %v, want context.Canceled", err)
	}

	fullScheduler.done("group-a")
	if len(fullScheduler.inflight) != 0 {
		t.Fatalf("done() inflight = %v, want empty", fullScheduler.inflight)
	}

	task := domain.Task{
		SessionID:         "session-1",
		BranchID:          "",
		ParentTaskID:      "parent-1",
		ID:                "task-1",
		RoutingHints:      map[string]any{"cluster_id": "cluster-a"},
		ExecutionContract: map[string]any{"branch_id": "branch-x"},
	}
	if got := submissionGroupKey(task); got != "session-1::cluster-a" {
		t.Fatalf("submissionGroupKey(cluster hint) = %q", got)
	}

	if got := submissionGroupKey(domain.Task{RoutingHints: map[string]any{"plan_step_id": "step-1"}}); got != "step-1" {
		t.Fatalf("submissionGroupKey(plan_step_id) = %q, want step-1", got)
	}
	if got := submissionGroupKey(domain.Task{ParentTaskID: "parent-2"}); got != "parent-2" {
		t.Fatalf("submissionGroupKey(parent) = %q, want parent-2", got)
	}
	if got := submissionGroupKey(domain.Task{ID: "task-2"}); got != "task-2" {
		t.Fatalf("submissionGroupKey(id) = %q, want task-2", got)
	}
	if got := submissionGroupKey(domain.Task{}); got != "default" {
		t.Fatalf("submissionGroupKey(default) = %q, want default", got)
	}
}

func TestScoreScheduledTaskRewardsPriorityAndAge(t *testing.T) {
	now := time.Now()
	high := scoreScheduledTask(scheduledSubmission{
		delivery: deliveryTask(domain.Task{Priority: domain.PriorityCritical, Complexity: domain.ComplexityLow}),
		enqueued: now.Add(-20 * time.Second),
	})
	low := scoreScheduledTask(scheduledSubmission{
		delivery: deliveryTask(domain.Task{Priority: domain.PriorityLow, Complexity: domain.ComplexityCritical}),
		enqueued: now,
	})
	if high <= low {
		t.Fatalf("scoreScheduledTask() high=%f low=%f, want high > low", high, low)
	}
	defaulted := scoreScheduledTask(scheduledSubmission{delivery: deliveryTask(domain.Task{}), enqueued: now})
	if defaulted <= 0 {
		t.Fatalf("scoreScheduledTask(default) = %f, want > 0", defaulted)
	}
}

func TestNewWithStoreRegistersCoreModulesAndSupportsCheckpointRoundTrip(t *testing.T) {
	store, err := state.NewFileStore(t.TempDir() + "/state.json")
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	orchestrator := NewWithStore(store)
	if orchestrator == nil {
		t.Fatal("NewWithStore() = nil")
	}
	defer orchestrator.Close()

	if orchestrator.registry == nil || orchestrator.planner == nil || orchestrator.router == nil {
		t.Fatal("NewWithStore() did not initialize core collaborators")
	}
	if orchestrator.memory == nil || orchestrator.runtime == nil || orchestrator.delivery == nil {
		t.Fatal("NewWithStore() did not initialize runtime helpers")
	}
	if orchestrator.vfs == nil {
		t.Fatal("NewWithStore() vfs = nil, want initialized manager")
	}

	moduleNames := map[string]bool{}
	for _, module := range orchestrator.registry.ModuleInfos() {
		moduleNames[module.Name] = true
	}
	for _, required := range []string{"planner", "router", "memory_control", "validation_memory_gate"} {
		if !moduleNames[required] {
			t.Fatalf("module %q not registered", required)
		}
	}

	agents := orchestrator.registry.AgentInfos()
	if len(agents) == 0 {
		t.Fatal("NewWithStore() registered zero agents")
	}
	providers := map[string]bool{}
	for _, agent := range agents {
		providers[agent.Provider] = true
	}
	if !providers["local"] {
		t.Fatalf("registered providers = %v, want local provider", providers)
	}

	checkpoint := domain.ParallelPlanCheckpoint{
		Kind:            "parallel_plan_checkpoint",
		RootTaskID:      "root-1",
		SessionID:       "session-1",
		Branch:          checkpointBranchName("root-1"),
		RootTask:        domain.Task{ID: "root-1", SessionID: "session-1", Type: domain.TaskTypeCode, Input: domain.TaskInput{Description: "checkpoint"}},
		Plan:            domain.ExecutionPlan{TaskID: "root-1"},
		PlanArtifact:    domain.PlanArtifact{RootTaskID: "root-1"},
		PendingTaskIDs:  []string{"child-1"},
		ResultsByTaskID: map[string]any{"child-1": map[string]any{"status": "done"}},
		Status:          domain.ParallelPlanStatusPlanned,
		UpdatedAt:       time.Now().UTC(),
	}
	ctx := context.Background()
	if err := orchestrator.saveParallelCheckpointStatic(ctx, checkpoint); err != nil {
		t.Fatalf("saveParallelCheckpointStatic() error = %v", err)
	}
	if err := orchestrator.saveParallelCheckpoint(ctx, checkpoint); err != nil {
		t.Fatalf("saveParallelCheckpoint() error = %v", err)
	}
	loaded, ok, err := orchestrator.LoadParallelCheckpoint(ctx, checkpoint.SessionID, checkpoint.RootTaskID)
	if err != nil {
		t.Fatalf("LoadParallelCheckpoint() error = %v", err)
	}
	if !ok {
		t.Fatal("LoadParallelCheckpoint() ok = false, want true")
	}
	if loaded.RootTaskID != checkpoint.RootTaskID || loaded.SessionID != checkpoint.SessionID {
		t.Fatalf("loaded checkpoint identifiers = %+v", loaded)
	}
	if !reflect.DeepEqual(loaded.PendingTaskIDs, checkpoint.PendingTaskIDs) {
		t.Fatalf("loaded pending = %v, want %v", loaded.PendingTaskIDs, checkpoint.PendingTaskIDs)
	}
}

func TestNewDefaultFailsWithoutDatabaseConfiguration(t *testing.T) {
	for _, key := range []string{
		"AI_BRIDGE_MEMORY_DATABASE_URL",
		"AI_BRIDGE_POSTGRES_USER",
		"POSTGRES_USER",
		"AI_BRIDGE_POSTGRES_PASSWORD",
		"POSTGRES_PASSWORD",
		"AI_BRIDGE_POSTGRES_DB",
		"POSTGRES_DB",
		"AI_BRIDGE_POSTGRES_HOST",
		"POSTGRES_HOST",
		"AI_BRIDGE_POSTGRES_PORT",
		"POSTGRES_PORT",
	} {
		t.Setenv(key, "")
	}

	orchestrator, err := NewDefault(t.TempDir() + "/unused")
	if err == nil {
		if orchestrator != nil {
			orchestrator.Close()
		}
		t.Fatal("NewDefault() error = nil, want missing database store error")
	}
	if !strings.Contains(err.Error(), "database store is required") {
		t.Fatalf("NewDefault() error = %v, want database store is required", err)
	}
}

func deliveryTask(task domain.Task) delivery.TaskDelivery {
	return delivery.TaskDelivery{Task: task}
}
