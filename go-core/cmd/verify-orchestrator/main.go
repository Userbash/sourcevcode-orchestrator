package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type StepResult struct {
	Name      string        `json:"name"`
	Command   []string      `json:"command,omitempty"`
	Duration  time.Duration `json:"duration"`
	Success   bool          `json:"success"`
	ExitCode  int           `json:"exit_code,omitempty"`
	Output    string        `json:"output,omitempty"`
	Error     string        `json:"error,omitempty"`
	StartedAt time.Time     `json:"started_at"`
	EndedAt   time.Time     `json:"ended_at"`
}

type EnvironmentInfo struct {
	GoVersion    string `json:"go_version"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	CPUCount     int    `json:"cpu_count"`
	GOMAXPROCS   int    `json:"gomaxprocs"`
	WorkingDir   string `json:"working_dir"`
	GoModCache   string `json:"go_mod_cache,omitempty"`
	GoBuildCache string `json:"go_build_cache,omitempty"`
}

type RuntimeProfile struct {
	Scenario                string              `json:"scenario"`
	Level                   string              `json:"level,omitempty"`
	Description             string              `json:"description,omitempty"`
	FocusAreas              []string            `json:"focus_areas,omitempty"`
	Warnings                []string            `json:"warnings,omitempty"`
	RootTaskID              string              `json:"root_task_id"`
	SessionID               string              `json:"session_id"`
	Duration                time.Duration       `json:"duration"`
	GoroutinesBefore        int                 `json:"goroutines_before"`
	GoroutinesPeak          int                 `json:"goroutines_peak"`
	GoroutinesAfter         int                 `json:"goroutines_after"`
	HeapAllocBeforeBytes    uint64              `json:"heap_alloc_before_bytes"`
	HeapAllocPeakBytes      uint64              `json:"heap_alloc_peak_bytes"`
	HeapAllocAfterBytes     uint64              `json:"heap_alloc_after_bytes"`
	TotalAllocDeltaBytes    uint64              `json:"total_alloc_delta_bytes"`
	MallocDelta             uint64              `json:"malloc_delta"`
	FreeDelta               uint64              `json:"free_delta"`
	GCDelta                 uint32              `json:"gc_delta"`
	PlannedTaskCount        int                 `json:"planned_task_count"`
	CompletedTaskCount      int                 `json:"completed_task_count"`
	ParallelBranchCount     int                 `json:"parallel_branch_count"`
	RegisteredAgentCount    int                 `json:"registered_agent_count"`
	WorkflowCount           int                 `json:"workflow_count"`
	RoutingWeights          map[string]any      `json:"routing_weights,omitempty"`
	ExecutionProfile        map[string]any      `json:"execution_profile,omitempty"`
	DeliverySnapshot        map[string]any      `json:"delivery_snapshot,omitempty"`
	RuntimeSnapshot         map[string]any      `json:"runtime_snapshot,omitempty"`
	PlanTaskIDs             []string            `json:"plan_task_ids,omitempty"`
	CompletedTaskIDs        []string            `json:"completed_task_ids,omitempty"`
	ResultArtifactCount     int                 `json:"result_artifact_count"`
	AcceptanceCriteriaCount int                 `json:"acceptance_criteria_count"`
	RequestedFileCount      int                 `json:"requested_file_count"`
	WorkflowTraces          []WorkflowTrace     `json:"workflow_traces,omitempty"`
	Distribution            DistributionSummary `json:"distribution,omitempty"`
	TaskEventCount          int                 `json:"task_event_count"`
	UnexpectedEventCount    int                 `json:"unexpected_event_count"`
	NoisyWorkflowCount      int                 `json:"noisy_workflow_count"`
	MeanQueueLatency        time.Duration       `json:"mean_queue_latency,omitempty"`
	MeanExecutionLatency    time.Duration       `json:"mean_execution_latency,omitempty"`
	MeanTotalLatency        time.Duration       `json:"mean_total_latency,omitempty"`
	MaxObservedParallelism  int                 `json:"max_observed_parallelism"`
}

type WorkflowTrace struct {
	TaskID              string        `json:"task_id"`
	ParentTaskID        string        `json:"parent_task_id,omitempty"`
	BranchID            string        `json:"branch_id,omitempty"`
	ClusterID           string        `json:"cluster_id,omitempty"`
	Capability          string        `json:"capability,omitempty"`
	WorkerClass         string        `json:"worker_class,omitempty"`
	Status              string        `json:"status,omitempty"`
	ResultStatus        string        `json:"result_status,omitempty"`
	AgentID             string        `json:"agent_id,omitempty"`
	Provider            string        `json:"provider,omitempty"`
	ModelName           string        `json:"model_name,omitempty"`
	Dependencies        []string      `json:"dependencies,omitempty"`
	Files               []string      `json:"files,omitempty"`
	EventKinds          []string      `json:"event_kinds,omitempty"`
	AcceptedAt          time.Time     `json:"accepted_at,omitempty"`
	StartedAt           time.Time     `json:"started_at,omitempty"`
	CompletedAt         time.Time     `json:"completed_at,omitempty"`
	QueueLatency        time.Duration `json:"queue_latency,omitempty"`
	ExecutionLatency    time.Duration `json:"execution_latency,omitempty"`
	TotalLatency        time.Duration `json:"total_latency,omitempty"`
	ResultSummary       string        `json:"result_summary,omitempty"`
	ResultArtifactCount int           `json:"result_artifact_count,omitempty"`
}

type DistributionSummary struct {
	ByCapability map[string]int `json:"by_capability,omitempty"`
	ByAgent      map[string]int `json:"by_agent,omitempty"`
	ByProvider   map[string]int `json:"by_provider,omitempty"`
	ByModel      map[string]int `json:"by_model,omitempty"`
}

type AgentKPI struct {
	AgentID                 string        `json:"agent_id"`
	AgentType               string        `json:"agent_type"`
	Provider                string        `json:"provider"`
	ModelName               string        `json:"model_name"`
	Capabilities            []string      `json:"capabilities,omitempty"`
	ExecutionCount          uint64        `json:"execution_count"`
	SuccessCount            uint64        `json:"success_count"`
	FailureCount            uint64        `json:"failure_count"`
	PeakConcurrency         int64         `json:"peak_concurrency"`
	AverageLatency          time.Duration `json:"average_latency"`
	LastTaskID              string        `json:"last_task_id,omitempty"`
	ObservedFiles           []string      `json:"observed_files,omitempty"`
	ObservedTaskTypes       []string      `json:"observed_task_types,omitempty"`
	SupportsParallelFanout  bool          `json:"supports_parallel_fanout"`
	SupportsAsynchronousUse bool          `json:"supports_asynchronous_use"`
}

type CoordinatorKPI struct {
	RegisteredAgents     int            `json:"registered_agents"`
	RuntimeAgentCount    int            `json:"runtime_agent_count"`
	ModuleCount          int            `json:"module_count"`
	WorkflowCount        int            `json:"workflow_count"`
	RuntimeStreamCount   int            `json:"runtime_stream_count"`
	InventoryStreamCount int            `json:"inventory_stream_count"`
	ProviderCount        int            `json:"provider_count"`
	RoutingWeights       map[string]any `json:"routing_weights,omitempty"`
	ProviderHealth       map[string]any `json:"provider_health,omitempty"`
	DeliverySnapshot     map[string]any `json:"delivery_snapshot,omitempty"`
	ParallelTaskGroups   int            `json:"parallel_task_groups"`
	PlannedTaskCount     int            `json:"planned_task_count"`
	CompletedTaskCount   int            `json:"completed_task_count"`
	ResultArtifactCount  int            `json:"result_artifact_count"`
	ExecutionProfile     map[string]any `json:"execution_profile,omitempty"`
	StateSnapshotDigest  map[string]any `json:"state_snapshot_digest,omitempty"`
	CompletedTaskIDs     []string       `json:"completed_task_ids,omitempty"`
}

type Report struct {
	GeneratedAt      time.Time         `json:"generated_at"`
	Root             string            `json:"root"`
	Environment      EnvironmentInfo   `json:"environment"`
	Steps            []StepResult      `json:"steps"`
	RuntimeProfile   *RuntimeProfile   `json:"runtime_profile,omitempty"`
	RuntimeScenarios []*RuntimeProfile `json:"runtime_scenarios,omitempty"`
	AgentKPIs        []AgentKPI        `json:"agent_kpis,omitempty"`
	CoordinatorKPI   *CoordinatorKPI   `json:"coordinator_kpi,omitempty"`
	Success          bool              `json:"success"`
	Notes            []string          `json:"notes,omitempty"`
}

type step struct {
	name string
	cmd  []string
}

type syntheticScenario struct {
	Name        string
	Level       string
	Description string
	FocusAreas  []string
	Task        domain.Task
}

type runtimeScenarioExecution struct {
	Profile        *RuntimeProfile
	AgentKPIs      []AgentKPI
	CoordinatorKPI *CoordinatorKPI
}

type syntheticAgent struct {
	info             domain.AgentInfo
	delay            time.Duration
	executions       atomic.Uint64
	succeeded        atomic.Uint64
	failed           atomic.Uint64
	active           atomic.Int64
	peakConcurrency  atomic.Int64
	totalLatencyNano atomic.Uint64
	lastTaskID       atomic.Value
	mu               sync.Mutex
	filesHandled     []string
	taskTypes        []string
}

func (a *syntheticAgent) Info() domain.AgentInfo {
	return a.info
}

func (a *syntheticAgent) CanAccept(task domain.Task) bool {
	if task.RequiredCapability == "" {
		return true
	}
	for _, capability := range a.info.Capabilities {
		if capability == task.RequiredCapability {
			return true
		}
	}
	return false
}

func (a *syntheticAgent) Execute(ctx context.Context, task domain.Task) domain.AgentResult {
	started := time.Now()
	a.executions.Add(1)
	active := a.active.Add(1)
	for {
		peak := a.peakConcurrency.Load()
		if active <= peak {
			break
		}
		if a.peakConcurrency.CompareAndSwap(peak, active) {
			break
		}
	}
	defer a.active.Add(-1)
	a.lastTaskID.Store(task.ID)

	a.mu.Lock()
	for _, file := range task.Input.Files {
		if file != "" && !contains(a.filesHandled, file) {
			a.filesHandled = append(a.filesHandled, file)
		}
	}
	taskType := string(task.Type)
	if taskType != "" && !contains(a.taskTypes, taskType) {
		a.taskTypes = append(a.taskTypes, taskType)
	}
	a.mu.Unlock()

	select {
	case <-ctx.Done():
		a.failed.Add(1)
		a.totalLatencyNano.Add(uint64(time.Since(started).Nanoseconds()))
		return domain.AgentResult{
			TaskID:      task.ID,
			Status:      domain.TaskStatusFailed,
			Errors:      []string{ctx.Err().Error()},
			CompletedAt: time.Now(),
		}
	case <-time.After(a.delay):
	}

	a.succeeded.Add(1)
	a.totalLatencyNano.Add(uint64(time.Since(started).Nanoseconds()))
	return domain.AgentResult{
		TaskID: task.ID,
		Status: domain.TaskStatusDone,
		Output: domain.ResultOutput{
			Summary: fmt.Sprintf("handled by %s", a.info.ID),
			Artifacts: map[string]any{
				"agent": a.info.ID,
				"files": append([]string(nil), task.Input.Files...),
			},
		},
		CompletedAt: time.Now(),
		AgentID:     a.info.ID,
		Provider:    a.info.Provider,
		ModelName:   a.info.ModelName,
	}
}

func (a *syntheticAgent) KPI() AgentKPI {
	executions := a.executions.Load()
	averageLatency := time.Duration(0)
	if executions > 0 {
		averageLatency = time.Duration(a.totalLatencyNano.Load() / executions)
	}
	lastTaskID, _ := a.lastTaskID.Load().(string)
	a.mu.Lock()
	files := append([]string(nil), a.filesHandled...)
	taskTypes := append([]string(nil), a.taskTypes...)
	a.mu.Unlock()
	return AgentKPI{
		AgentID:                 a.info.ID,
		AgentType:               a.info.Type,
		Provider:                a.info.Provider,
		ModelName:               a.info.ModelName,
		Capabilities:            append([]string(nil), a.info.Capabilities...),
		ExecutionCount:          executions,
		SuccessCount:            a.succeeded.Load(),
		FailureCount:            a.failed.Load(),
		PeakConcurrency:         a.peakConcurrency.Load(),
		AverageLatency:          averageLatency,
		LastTaskID:              lastTaskID,
		ObservedFiles:           files,
		ObservedTaskTypes:       taskTypes,
		SupportsParallelFanout:  true,
		SupportsAsynchronousUse: true,
	}
}

func main() {
	jsonOutput := flag.Bool("json", false, "emit machine-readable JSON")
	skipRace := flag.Bool("skip-race", false, "skip the Go race detector step")
	skipBench := flag.Bool("skip-bench", false, "skip the benchmark step")
	timeout := flag.Duration("timeout", 4*time.Minute, "overall timeout")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	root, err := os.Getwd()
	if err != nil {
		fatal(err, *jsonOutput)
	}

	report := Report{
		GeneratedAt: time.Now().UTC(),
		Root:        root,
		Environment: EnvironmentInfo{
			GoVersion:    runtime.Version(),
			OS:           runtime.GOOS,
			Arch:         runtime.GOARCH,
			CPUCount:     runtime.NumCPU(),
			GOMAXPROCS:   runtime.GOMAXPROCS(0),
			WorkingDir:   root,
			GoModCache:   os.Getenv("GOMODCACHE"),
			GoBuildCache: os.Getenv("GOCACHE"),
		},
		Success: true,
	}

	steps := []step{
		{name: "typed-compile", cmd: []string{"go", "test", "./internal/kernel", "./internal/api", "./internal/memory", "./cmd/orchestrator", "-run", "^$", "-count=1"}},
		{name: "async-and-routing-tests", cmd: []string{"go", "test", "./internal/kernel", "-run", "TestRunExecutionPlanExecutesParallelBranchesConcurrently|TestRouter", "-count=1"}},
		{name: "delivery-retries-tests", cmd: []string{"go", "test", "./internal/api", "./internal/delivery", "-run", "TestDelivery|TestWorkerPool|TestSupervisor", "-count=1"}},
		{name: "websocket-version-handshake", cmd: []string{"go", "test", "./internal/api", "-run", "TestControlWebSocketEndToEnd", "-count=1"}},
	}
	if !*skipRace {
		steps = append(steps, step{name: "race-detector", cmd: []string{"go", "test", "./internal/kernel", "./internal/api", "-race", "-count=1"}})
	}
	if !*skipBench {
		steps = append(steps, step{name: "parallel-fanout-benchmark", cmd: []string{"go", "test", "./internal/kernel", "-run", "^$", "-bench", "BenchmarkRunExecutionPlanParallelFanout", "-benchmem", "-count=1"}})
	}

	for _, current := range steps {
		result := runCommandStep(ctx, root, current)
		report.Steps = append(report.Steps, result)
		if !result.Success {
			report.Success = false
			report.Notes = append(report.Notes, fmt.Sprintf("step %s failed", current.name))
			printReport(report, *jsonOutput)
			os.Exit(1)
		}
	}

	runtimeStep, runtimeProfile, runtimeScenarios, agentKPIs, coordinatorKPI := runRuntimeProfileStep(ctx)
	report.Steps = append(report.Steps, runtimeStep)
	report.RuntimeProfile = runtimeProfile
	report.RuntimeScenarios = runtimeScenarios
	report.AgentKPIs = agentKPIs
	report.CoordinatorKPI = coordinatorKPI
	if !runtimeStep.Success {
		report.Success = false
		report.Notes = append(report.Notes, "runtime profiling failed")
		printReport(report, *jsonOutput)
		os.Exit(1)
	}

	report.Notes = append(report.Notes,
		"all validation steps passed",
		"runtime profiler exported CPU/memory/goroutine and agent KPI snapshots",
		"runtime scenarios cover basic docs, intermediate research and advanced parallel code execution",
	)
	printReport(report, *jsonOutput)
}

func runCommandStep(ctx context.Context, root string, current step) StepResult {
	startedAt := time.Now()
	result := StepResult{
		Name:      current.name,
		Command:   append([]string(nil), current.cmd...),
		StartedAt: startedAt,
	}

	cmd := exec.CommandContext(ctx, current.cmd[0], current.cmd[1:]...)
	cmd.Dir = root
	cmd.Env = append(os.Environ(),
		"GOMODCACHE="+valueOrDefault(os.Getenv("GOMODCACHE"), filepath.Join(root, ".gocache", "mod")),
		"GOCACHE="+valueOrDefault(os.Getenv("GOCACHE"), filepath.Join(root, ".gocache", "build")),
	)

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	result.EndedAt = time.Now()
	result.Duration = result.EndedAt.Sub(startedAt)
	result.Output = strings.TrimSpace(stdout.String())
	if stderr.Len() > 0 {
		if result.Output != "" {
			result.Output += "\n"
		}
		result.Output += strings.TrimSpace(stderr.String())
	}

	if err == nil {
		result.Success = true
		return result
	}

	result.Success = false
	result.Error = err.Error()
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		result.ExitCode = exitErr.ExitCode()
	}
	if ctx.Err() != nil {
		result.Error = ctx.Err().Error()
	}
	return result
}

func runRuntimeProfileStep(ctx context.Context) (StepResult, *RuntimeProfile, []*RuntimeProfile, []AgentKPI, *CoordinatorKPI) {
	startedAt := time.Now()
	executions, err := runSyntheticRuntimeProfiles(ctx)
	result := StepResult{
		Name:      "runtime-profile",
		StartedAt: startedAt,
		EndedAt:   time.Now(),
	}
	result.Duration = result.EndedAt.Sub(result.StartedAt)
	if err != nil {
		result.Success = false
		result.Error = err.Error()
		return result, nil, nil, nil, nil
	}
	if len(executions) == 0 {
		result.Success = false
		result.Error = "no runtime scenarios executed"
		return result, nil, nil, nil, nil
	}
	scenarios := make([]*RuntimeProfile, 0, len(executions))
	for _, execution := range executions {
		scenarios = append(scenarios, execution.Profile)
	}
	selected := executions[len(executions)-1]
	result.Success = true
	result.Output = fmt.Sprintf("scenarios=%d final=%s peak_goroutines=%d planned=%d completed=%d agents=%d warnings=%d",
		len(executions),
		selected.Profile.Scenario,
		selected.Profile.GoroutinesPeak,
		selected.Profile.PlannedTaskCount,
		selected.Profile.CompletedTaskCount,
		len(selected.AgentKPIs),
		len(selected.Profile.Warnings),
	)
	return result, selected.Profile, scenarios, selected.AgentKPIs, selected.CoordinatorKPI
}

func runSyntheticRuntimeProfile(ctx context.Context) (*RuntimeProfile, []AgentKPI, *CoordinatorKPI, error) {
	executions, err := runSyntheticRuntimeProfiles(ctx)
	if err != nil {
		return nil, nil, nil, err
	}
	if len(executions) == 0 {
		return nil, nil, nil, errors.New("no runtime scenarios executed")
	}
	selected := executions[len(executions)-1]
	return selected.Profile, selected.AgentKPIs, selected.CoordinatorKPI, nil
}

func runSyntheticRuntimeProfiles(ctx context.Context) ([]runtimeScenarioExecution, error) {
	for key, value := range map[string]string{
		"GO_CORE_MESSAGE_BUS_BACKEND":      "memory",
		"GO_CORE_SUBMIT_MODE":              "sync",
		"GO_CORE_MAX_PARALLELISM":          "8",
		"GO_CORE_MAX_CONCURRENT_PER_AGENT": "4",
		"GO_CORE_MAX_CONCURRENT_PER_MODEL": "4",
		"GO_CORE_ROUTE_WEIGHT_HEALTH":      "0.4",
		"GO_CORE_ROUTE_WEIGHT_CAPABILITY":  "0.4",
		"GO_CORE_ROUTE_WEIGHT_LOAD":        "0.2",
	} {
		if os.Getenv(key) == "" {
			if err := os.Setenv(key, value); err != nil {
				return nil, err
			}
		}
	}

	scenarios := syntheticScenarios()
	executions := make([]runtimeScenarioExecution, 0, len(scenarios))
	for _, scenario := range scenarios {
		execution, err := runSyntheticRuntimeScenario(ctx, scenario)
		if err != nil {
			return nil, fmt.Errorf("scenario %s: %w", scenario.Name, err)
		}
		executions = append(executions, execution)
	}
	return executions, nil
}

func syntheticScenarios() []syntheticScenario {
	return []syntheticScenario{
		{
			Name:        "level-1-docs-sequential",
			Level:       "basic",
			Description: "Single-stage documentation task used to baseline latency, typing and sequential routing.",
			FocusAreas:  []string{"single-agent routing", "transport validation", "low-latency baseline"},
			Task: domain.Task{
				ID:               "verify-docs-task",
				SessionID:        "verify-session-docs",
				Type:             domain.TaskTypeDocs,
				Complexity:       domain.ComplexityLow,
				AssignedProvider: "kernel",
				Input: domain.TaskInput{
					Description: "Draft a small API usage note for runtime profiling.",
					Files:       []string{"README.md"},
					AcceptanceCriteria: []string{
						"single docs artifact",
						"no empty output fields",
					},
				},
			},
		},
		{
			Name:        "level-2-research-review",
			Level:       "intermediate",
			Description: "Research-heavy task that should traverse planner, research and review stages without parallel fanout.",
			FocusAreas:  []string{"planner decomposition", "research handoff", "review latency"},
			Task: domain.Task{
				ID:               "verify-research-task",
				SessionID:        "verify-session-research",
				Type:             domain.TaskTypeResearch,
				Complexity:       domain.ComplexityMedium,
				AssignedProvider: "kernel",
				Input: domain.TaskInput{
					Description: "Investigate routing and summarize validation risks before implementation.",
					Files: []string{
						"go-core/internal/kernel/router.go",
						"go-core/internal/kernel/model_selector.go",
					},
					AcceptanceCriteria: []string{
						"research summary",
						"review recommendation",
					},
				},
			},
		},
		{
			Name:        "level-3-code-fanout",
			Level:       "advanced",
			Description: "Critical code task that should fan out across workers, then converge through review and test stages.",
			FocusAreas:  []string{"parallel branch scheduling", "memory pressure", "fanout/fanin correctness", "multistage coordination"},
			Task: domain.Task{
				ID:               "verify-root-task",
				SessionID:        "verify-session",
				Type:             domain.TaskTypeCode,
				Complexity:       domain.ComplexityCritical,
				AssignedProvider: "kernel",
				Input: domain.TaskInput{
					Description: "Split a code task into plan, code, review and test stages.",
					Files: []string{
						"go-core/internal/kernel/orchestrator.go",
						"go-core/internal/kernel/router.go",
						"go-core/internal/kernel/model_selector.go",
					},
					AcceptanceCriteria: []string{
						"parallel branch execution",
						"type-safe results",
					},
				},
			},
		},
	}
}

func runSyntheticRuntimeScenario(ctx context.Context, scenario syntheticScenario) (runtimeScenarioExecution, error) {
	storeDir, err := os.MkdirTemp("", "orchestrator-verify-store-")
	if err != nil {
		return runtimeScenarioExecution{}, err
	}
	defer os.RemoveAll(storeDir)

	store, err := state.NewFileStore(filepath.Join(storeDir, "state"))
	if err != nil {
		return runtimeScenarioExecution{}, err
	}

	registry := kernel.NewRegistry()
	selector := kernel.NewModelSelector(nil)
	planner := kernel.NewPlanner(selector)
	router := kernel.NewRouter(registry, selector)
	runtimeHub := realtime.NewHub("runtime", 64)
	inventoryHub := realtime.NewHub("inventory", 32)
	orchestrator := kernel.NewOrchestrator(registry, planner, router, store, runtimeHub, inventoryHub, nil)

	selection := selector.Select(scenario.Task)
	agentProvider := firstNonEmptyString(strings.TrimSpace(scenario.Task.AssignedProvider), strings.TrimSpace(selection.Provider), "kernel")
	agentModel := firstNonEmptyString(strings.TrimSpace(scenario.Task.AssignedModel), strings.TrimSpace(selection.ModelName), "synthetic-default")
	agents := []*syntheticAgent{
		{info: domain.AgentInfo{ID: "plan-supervisor", Type: "superagent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"plan"}, Status: domain.AgentStatusReady}, delay: 25 * time.Millisecond},
		{info: domain.AgentInfo{ID: "code-worker", Type: "agent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"code"}, Status: domain.AgentStatusReady}, delay: 90 * time.Millisecond},
		{info: domain.AgentInfo{ID: "review-supervisor", Type: "superagent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"review"}, Status: domain.AgentStatusReady}, delay: 30 * time.Millisecond},
		{info: domain.AgentInfo{ID: "test-worker", Type: "agent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"test"}, Status: domain.AgentStatusReady}, delay: 35 * time.Millisecond},
		{info: domain.AgentInfo{ID: "docs-worker", Type: "agent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"docs"}, Status: domain.AgentStatusReady}, delay: 20 * time.Millisecond},
		{info: domain.AgentInfo{ID: "research-worker", Type: "agent", Provider: agentProvider, ModelName: agentModel, Capabilities: []string{"research"}, Status: domain.AgentStatusReady}, delay: 40 * time.Millisecond},
	}
	for _, agent := range agents {
		registry.RegisterAgent(agent)
	}

	runtimeCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	memBefore := readMemStats()
	goroutinesBefore := runtime.NumGoroutine()
	peakGoroutines := goroutinesBefore
	heapPeak := memBefore.HeapAlloc
	stopSampling := make(chan struct{})
	sampled := make(chan struct{})
	go sampleRuntime(runtimeCtx, stopSampling, sampled, &peakGoroutines, &heapPeak)

	startedAt := time.Now()
	run, err := orchestrator.RunExecutionPlan(ctx, scenario.Task)
	close(stopSampling)
	<-sampled
	if err != nil {
		return runtimeScenarioExecution{}, err
	}
	completedAt := time.Now()
	memAfter := readMemStats()
	goroutinesAfter := runtime.NumGoroutine()

	stateSnapshot := orchestrator.StateSnapshot(ctx)
	deliverySnapshot := orchestrator.DeliveryHealthSnapshot()
	executionProfile := orchestrator.ExecutionProfile()

	plannedIDs := plannedTaskIDs(run.PlanArtifact)
	completedIDs := append([]string(nil), run.Checkpoint.CompletedTaskIDs...)
	parallelBranchCount := countParallelBranches(run.PlanArtifact)
	taskArtifactsByID := planTasksByID(run.PlanArtifact)

	profile := &RuntimeProfile{
		Scenario:                scenario.Name,
		Level:                   scenario.Level,
		Description:             scenario.Description,
		FocusAreas:              append([]string(nil), scenario.FocusAreas...),
		RootTaskID:              scenario.Task.ID,
		SessionID:               scenario.Task.SessionID,
		Duration:                completedAt.Sub(startedAt),
		GoroutinesBefore:        goroutinesBefore,
		GoroutinesPeak:          peakGoroutines,
		GoroutinesAfter:         goroutinesAfter,
		HeapAllocBeforeBytes:    memBefore.HeapAlloc,
		HeapAllocPeakBytes:      heapPeak,
		HeapAllocAfterBytes:     memAfter.HeapAlloc,
		TotalAllocDeltaBytes:    saturatingDelta(memAfter.TotalAlloc, memBefore.TotalAlloc),
		MallocDelta:             saturatingDelta(memAfter.Mallocs, memBefore.Mallocs),
		FreeDelta:               saturatingDelta(memAfter.Frees, memBefore.Frees),
		GCDelta:                 memAfter.NumGC - memBefore.NumGC,
		PlannedTaskCount:        len(plannedIDs),
		CompletedTaskCount:      len(completedIDs),
		ParallelBranchCount:     parallelBranchCount,
		RegisteredAgentCount:    len(agents),
		WorkflowCount:           intFromAny(stateSnapshot["workflow_count"]),
		RoutingWeights:          mapFromAny(stateSnapshot["routing_weights"]),
		ExecutionProfile:        executionProfile,
		DeliverySnapshot:        deliverySnapshot,
		RuntimeSnapshot:         stateSnapshot,
		PlanTaskIDs:             plannedIDs,
		CompletedTaskIDs:        completedIDs,
		ResultArtifactCount:     len(run.Checkpoint.ResultsByTaskID),
		AcceptanceCriteriaCount: len(scenario.Task.Input.AcceptanceCriteria),
		RequestedFileCount:      len(scenario.Task.Input.Files),
	}
	taskEvents := orchestrator.RuntimeEventSnapshot("tasks")
	workflowTraces := buildWorkflowTraces(run.PlanArtifact, run.Workflows, taskEvents)
	profile.WorkflowTraces = workflowTraces
	profile.Distribution = summarizeDistribution(workflowTraces)
	profile.TaskEventCount, profile.UnexpectedEventCount, profile.NoisyWorkflowCount = summarizeTaskEvents(workflowTraces)
	profile.MeanQueueLatency = meanWorkflowLatency(workflowTraces, func(trace WorkflowTrace) time.Duration { return trace.QueueLatency })
	profile.MeanExecutionLatency = meanWorkflowLatency(workflowTraces, func(trace WorkflowTrace) time.Duration { return trace.ExecutionLatency })
	profile.MeanTotalLatency = meanWorkflowLatency(workflowTraces, func(trace WorkflowTrace) time.Duration { return trace.TotalLatency })
	profile.MaxObservedParallelism = maxObservedParallelism(workflowTraces)

	agentKPIs := make([]AgentKPI, 0, len(agents))
	for _, agent := range agents {
		agentKPIs = append(agentKPIs, agent.KPI())
	}
	mergeWorkflowRecords(agentKPIs, run.Workflows, taskArtifactsByID)
	mergeCheckpointResults(agentKPIs, run.Checkpoint.ResultsByTaskID, taskArtifactsByID)
	profile.Warnings = detectScenarioWarnings(profile, agentKPIs)

	coordinator := &CoordinatorKPI{
		RegisteredAgents:     len(agents),
		RuntimeAgentCount:    lenFromAny(stateSnapshot["runtime_agents"]),
		ModuleCount:          intFromAny(stateSnapshot["module_count"]),
		WorkflowCount:        intFromAny(stateSnapshot["workflow_count"]),
		RuntimeStreamCount:   topicsCount(stateSnapshot["runtime_streams"]),
		InventoryStreamCount: topicsCount(stateSnapshot["inventory_streams"]),
		ProviderCount:        lenFromAny(stateSnapshot["providers"]),
		RoutingWeights:       mapFromAny(stateSnapshot["routing_weights"]),
		ProviderHealth:       mapFromAny(stateSnapshot["provider_health"]),
		DeliverySnapshot:     deliverySnapshot,
		ParallelTaskGroups:   parallelBranchCount,
		PlannedTaskCount:     len(plannedIDs),
		CompletedTaskCount:   len(completedIDs),
		ResultArtifactCount:  len(run.Checkpoint.ResultsByTaskID),
		ExecutionProfile:     executionProfile,
		StateSnapshotDigest: map[string]any{
			"status":         stateSnapshot["status"],
			"agent_count":    stateSnapshot["agent_count"],
			"workflow_count": stateSnapshot["workflow_count"],
			"delivery":       stateSnapshot["delivery"],
		},
		CompletedTaskIDs: completedIDs,
	}

	return runtimeScenarioExecution{Profile: profile, AgentKPIs: agentKPIs, CoordinatorKPI: coordinator}, nil
}

func detectScenarioWarnings(profile *RuntimeProfile, agentKPIs []AgentKPI) []string {
	warnings := make([]string, 0, 12)
	if profile == nil {
		return warnings
	}
	if profile.CompletedTaskCount < profile.PlannedTaskCount {
		warnings = append(warnings, "planned task count exceeds completed task count")
	}
	if len(profile.WorkflowTraces) != profile.PlannedTaskCount {
		warnings = append(warnings, "workflow trace count does not match planned task count")
	}
	if profile.ResultArtifactCount == 0 {
		warnings = append(warnings, "no result artifacts were produced")
	}
	if profile.ParallelBranchCount > 1 && maxPeakConcurrency(agentKPIs) < 2 {
		warnings = append(warnings, "parallel branches were planned but worker concurrency stayed below 2")
	}
	if profile.ParallelBranchCount > 1 && profile.MaxObservedParallelism < 2 {
		warnings = append(warnings, "parallel branches were planned but workflow traces did not overlap")
	}
	if profile.UnexpectedEventCount > 0 {
		warnings = append(warnings, fmt.Sprintf("unexpected task events detected: count=%d", profile.UnexpectedEventCount))
	}
	if profile.NoisyWorkflowCount > 0 {
		warnings = append(warnings, fmt.Sprintf("workflow event noise threshold exceeded: workflows=%d", profile.NoisyWorkflowCount))
	}
	mallocThreshold := uint64(maxInt(profile.PlannedTaskCount, 2) * 250000)
	if profile.MallocDelta > mallocThreshold {
		warnings = append(warnings, fmt.Sprintf("high allocation pressure detected: malloc_delta=%d threshold=%d", profile.MallocDelta, mallocThreshold))
	}
	gcThreshold := uint32(maxInt(profile.PlannedTaskCount*8, 24))
	if profile.GCDelta > gcThreshold {
		warnings = append(warnings, fmt.Sprintf("elevated GC activity detected: gc_delta=%d threshold=%d", profile.GCDelta, gcThreshold))
	}
	if profile.Level != "advanced" && profile.Duration > 350*time.Millisecond {
		warnings = append(warnings, fmt.Sprintf("unexpected latency for %s scenario: %s", profile.Level, profile.Duration))
	}
	if duplicates := duplicateStrings(profile.CompletedTaskIDs); len(duplicates) > 0 {
		warnings = append(warnings, fmt.Sprintf("duplicate completed task ids detected: %s", strings.Join(duplicates, ",")))
	}
	if duplicates := duplicateStrings(profile.PlanTaskIDs); len(duplicates) > 0 {
		warnings = append(warnings, fmt.Sprintf("duplicate planned task ids detected: %s", strings.Join(duplicates, ",")))
	}
	warnings = append(warnings, missingWorkflowTraceFields(profile.WorkflowTraces)...)
	return warnings
}

func buildWorkflowTraces(plan domain.PlanArtifact, workflows []domain.WorkflowRecord, events []domain.StreamEvent) []WorkflowTrace {
	if len(plan.Tasks) == 0 {
		return nil
	}
	workflowByTaskID := make(map[string]domain.WorkflowRecord, len(workflows))
	for _, workflow := range workflows {
		if workflow.Task.ID != "" {
			workflowByTaskID[workflow.Task.ID] = workflow
		}
	}
	timelines := taskEventTimelines(events)
	traces := make([]WorkflowTrace, 0, len(plan.Tasks))
	for _, task := range plan.Tasks {
		trace := WorkflowTrace{
			TaskID:       task.TaskID,
			BranchID:     task.BranchID,
			ClusterID:    task.ClusterID,
			Capability:   task.Capability,
			WorkerClass:  task.WorkerClass,
			Dependencies: append([]string(nil), task.Dependencies...),
			Files:        append([]string(nil), task.Files...),
		}
		if workflow, ok := workflowByTaskID[task.TaskID]; ok {
			trace.ParentTaskID = workflow.Task.ParentTaskID
			trace.Status = string(workflow.Acceptance.Status)
			trace.AgentID = workflow.Acceptance.AgentID
			trace.Provider = workflow.Acceptance.Provider
			trace.ModelName = workflow.Acceptance.ModelName
			if workflow.Acceptance.Capability != "" {
				trace.Capability = workflow.Acceptance.Capability
			}
			trace.AcceptedAt = workflow.Acceptance.AcceptedAt
			if workflow.Result != nil {
				trace.ResultStatus = string(workflow.Result.Status)
				trace.Provider = firstNonEmptyString(trace.Provider, workflow.Result.Provider)
				trace.ModelName = firstNonEmptyString(trace.ModelName, workflow.Result.ModelName)
				trace.AgentID = firstNonEmptyString(trace.AgentID, workflow.Result.AgentID)
				trace.CompletedAt = workflow.Result.CompletedAt
				trace.ResultSummary = workflow.Result.Output.Summary
				trace.ResultArtifactCount = len(workflow.Result.Output.Artifacts)
			}
			if trace.CompletedAt.IsZero() {
				trace.CompletedAt = workflow.UpdatedAt
			}
		}
		if timeline, ok := timelines[task.TaskID]; ok {
			trace.EventKinds = append([]string(nil), timeline.Kinds...)
			if trace.AcceptedAt.IsZero() {
				trace.AcceptedAt = timeline.Accepted
			}
			trace.StartedAt = timeline.Running
			if trace.CompletedAt.IsZero() {
				trace.CompletedAt = timeline.Completed
			}
			if !timeline.Queued.IsZero() && !timeline.Running.IsZero() {
				trace.QueueLatency = timeline.Running.Sub(timeline.Queued)
			}
			if !timeline.Running.IsZero() && !timeline.Completed.IsZero() {
				trace.ExecutionLatency = timeline.Completed.Sub(timeline.Running)
			}
			switch {
			case !timeline.Queued.IsZero() && !timeline.Completed.IsZero():
				trace.TotalLatency = timeline.Completed.Sub(timeline.Queued)
			case !trace.AcceptedAt.IsZero() && !timeline.Completed.IsZero():
				trace.TotalLatency = timeline.Completed.Sub(trace.AcceptedAt)
			}
		}
		traces = append(traces, trace)
	}
	return traces
}

func summarizeDistribution(traces []WorkflowTrace) DistributionSummary {
	summary := DistributionSummary{
		ByCapability: map[string]int{},
		ByAgent:      map[string]int{},
		ByProvider:   map[string]int{},
		ByModel:      map[string]int{},
	}
	for _, trace := range traces {
		if trace.Capability != "" {
			summary.ByCapability[trace.Capability]++
		}
		if trace.AgentID != "" {
			summary.ByAgent[trace.AgentID]++
		}
		if trace.Provider != "" {
			summary.ByProvider[trace.Provider]++
		}
		if trace.ModelName != "" {
			summary.ByModel[trace.ModelName]++
		}
	}
	return summary
}

func summarizeTaskEvents(traces []WorkflowTrace) (int, int, int) {
	total := 0
	unexpected := 0
	noisy := 0
	for _, trace := range traces {
		total += len(trace.EventKinds)
		if len(trace.EventKinds) > 7 {
			noisy++
		}
		for _, kind := range trace.EventKinds {
			if !isExpectedTaskEvent(kind) {
				unexpected++
			}
		}
	}
	return total, unexpected, noisy
}

func meanWorkflowLatency(traces []WorkflowTrace, pick func(WorkflowTrace) time.Duration) time.Duration {
	var total time.Duration
	count := 0
	for _, trace := range traces {
		latency := pick(trace)
		if latency <= 0 {
			continue
		}
		total += latency
		count++
	}
	if count == 0 {
		return 0
	}
	return total / time.Duration(count)
}

func maxObservedParallelism(traces []WorkflowTrace) int {
	type edge struct {
		at    time.Time
		delta int
	}
	edges := make([]edge, 0, len(traces)*2)
	for _, trace := range traces {
		if trace.StartedAt.IsZero() || trace.CompletedAt.IsZero() || !trace.CompletedAt.After(trace.StartedAt) {
			continue
		}
		edges = append(edges, edge{at: trace.StartedAt, delta: 1})
		edges = append(edges, edge{at: trace.CompletedAt, delta: -1})
	}
	if len(edges) == 0 {
		return 0
	}
	sort.Slice(edges, func(i, j int) bool {
		if edges[i].at.Equal(edges[j].at) {
			return edges[i].delta > edges[j].delta
		}
		return edges[i].at.Before(edges[j].at)
	})
	current := 0
	maxCurrent := 0
	for _, edge := range edges {
		current += edge.delta
		if current > maxCurrent {
			maxCurrent = current
		}
	}
	return maxCurrent
}

type taskEventTimeline struct {
	Accepted  time.Time
	Queued    time.Time
	Running   time.Time
	Completed time.Time
	Kinds     []string
}

func taskEventTimelines(events []domain.StreamEvent) map[string]taskEventTimeline {
	timelines := make(map[string]taskEventTimeline, len(events))
	for _, event := range events {
		if event.EntityID == "" {
			continue
		}
		timeline := timelines[event.EntityID]
		timeline.Kinds = append(timeline.Kinds, event.Kind)
		switch event.Kind {
		case "task.accepted":
			if timeline.Accepted.IsZero() {
				timeline.Accepted = event.Timestamp
			}
		case "task.queued", "task.dequeued":
			if timeline.Queued.IsZero() {
				timeline.Queued = event.Timestamp
			}
		case "task.running":
			if timeline.Running.IsZero() {
				timeline.Running = event.Timestamp
			}
		case "task.completed", "task.result_received":
			if timeline.Completed.IsZero() {
				timeline.Completed = event.Timestamp
			}
		}
		timelines[event.EntityID] = timeline
	}
	return timelines
}

func isExpectedTaskEvent(kind string) bool {
	switch kind {
	case "task.accepted", "task.queued", "task.dequeued", "task.running", "task.result_received", "task.completed", "task.failed", "task.result_failed", "task.rerouted":
		return true
	default:
		return false
	}
}

func duplicateStrings(values []string) []string {
	if len(values) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(values))
	reported := map[string]struct{}{}
	duplicates := make([]string, 0, 2)
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			if _, exists := reported[value]; !exists {
				duplicates = append(duplicates, value)
				reported[value] = struct{}{}
			}
			continue
		}
		seen[value] = struct{}{}
	}
	return duplicates
}

func missingWorkflowTraceFields(traces []WorkflowTrace) []string {
	warnings := make([]string, 0, 4)
	for _, trace := range traces {
		if trace.TaskID == "" {
			warnings = append(warnings, "workflow trace has empty task id")
			continue
		}
		if trace.Capability == "" {
			warnings = append(warnings, fmt.Sprintf("workflow trace %s is missing capability", trace.TaskID))
		}
		if trace.AgentID == "" {
			warnings = append(warnings, fmt.Sprintf("workflow trace %s is missing agent id", trace.TaskID))
		}
		if trace.Status == "" && trace.ResultStatus == "" {
			warnings = append(warnings, fmt.Sprintf("workflow trace %s is missing terminal status", trace.TaskID))
		}
		if len(trace.EventKinds) == 0 {
			warnings = append(warnings, fmt.Sprintf("workflow trace %s did not capture task events", trace.TaskID))
		}
	}
	return warnings
}

func maxPeakConcurrency(agentKPIs []AgentKPI) int64 {
	var max int64
	for _, kpi := range agentKPIs {
		if kpi.PeakConcurrency > max {
			max = kpi.PeakConcurrency
		}
	}
	return max
}

func sampleRuntime(ctx context.Context, stop <-chan struct{}, done chan<- struct{}, peakGoroutines *int, heapPeak *uint64) {
	defer close(done)
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-stop:
			return
		case <-ticker.C:
			current := runtime.NumGoroutine()
			if current > *peakGoroutines {
				*peakGoroutines = current
			}
			stats := readMemStats()
			if stats.HeapAlloc > *heapPeak {
				*heapPeak = stats.HeapAlloc
			}
		}
	}
}

func readMemStats() runtime.MemStats {
	var stats runtime.MemStats
	runtime.ReadMemStats(&stats)
	return stats
}

func plannedTaskIDs(plan domain.PlanArtifact) []string {
	if len(plan.Tasks) == 0 {
		return nil
	}
	ids := make([]string, 0, len(plan.Tasks))
	for _, task := range plan.Tasks {
		if task.TaskID != "" {
			ids = append(ids, task.TaskID)
		}
	}
	return ids
}

func countParallelBranches(plan domain.PlanArtifact) int {
	groups := map[string]int{}
	for _, task := range plan.Tasks {
		capability := task.Capability
		if capability != "code" {
			continue
		}
		key := strings.Join(task.Dependencies, ",")
		groups[key]++
	}
	parallel := 0
	if len(plan.ParallelGroups) > 0 {
		for _, group := range plan.ParallelGroups {
			if len(group) > 1 {
				parallel += len(group)
			}
		}
		if parallel > 0 {
			return parallel
		}
	}
	for _, count := range groups {
		if count > 1 {
			parallel += count
		}
	}
	return parallel
}

func planTasksByID(plan domain.PlanArtifact) map[string]domain.PlanTaskArtifact {
	if len(plan.Tasks) == 0 {
		return map[string]domain.PlanTaskArtifact{}
	}
	byID := make(map[string]domain.PlanTaskArtifact, len(plan.Tasks))
	for _, task := range plan.Tasks {
		if task.TaskID != "" {
			byID[task.TaskID] = task
		}
	}
	return byID
}

func mergeWorkflowRecords(agentKPIs []AgentKPI, workflows []domain.WorkflowRecord, tasksByID map[string]domain.PlanTaskArtifact) {
	if len(agentKPIs) == 0 || len(workflows) == 0 {
		return
	}
	indexByAgentID := make(map[string]int, len(agentKPIs))
	for i, kpi := range agentKPIs {
		indexByAgentID[kpi.AgentID] = i
	}
	for _, workflow := range workflows {
		agentID := strings.TrimSpace(workflow.Acceptance.AgentID)
		if agentID == "" {
			continue
		}
		index, exists := indexByAgentID[agentID]
		if !exists {
			continue
		}
		kpi := &agentKPIs[index]
		kpi.ExecutionCount++
		status := workflow.Acceptance.Status
		if workflow.Result != nil && workflow.Result.Status != "" {
			status = workflow.Result.Status
		}
		switch status {
		case domain.TaskStatusDone, domain.TaskStatusCompleted:
			kpi.SuccessCount++
		case domain.TaskStatusFailed:
			kpi.FailureCount++
		}
		kpi.LastTaskID = workflow.Task.ID
		if task, ok := tasksByID[workflow.Task.ID]; ok {
			if task.Capability != "" && !contains(kpi.ObservedTaskTypes, task.Capability) {
				kpi.ObservedTaskTypes = append(kpi.ObservedTaskTypes, task.Capability)
			}
			for _, file := range task.Files {
				if file != "" && !contains(kpi.ObservedFiles, file) {
					kpi.ObservedFiles = append(kpi.ObservedFiles, file)
				}
			}
		} else {
			if taskType := string(workflow.Task.Type); taskType != "" && !contains(kpi.ObservedTaskTypes, taskType) {
				kpi.ObservedTaskTypes = append(kpi.ObservedTaskTypes, taskType)
			}
			for _, file := range workflow.Task.Input.Files {
				if file != "" && !contains(kpi.ObservedFiles, file) {
					kpi.ObservedFiles = append(kpi.ObservedFiles, file)
				}
			}
		}
	}
}

func mergeCheckpointResults(agentKPIs []AgentKPI, results map[string]any, tasksByID map[string]domain.PlanTaskArtifact) {
	if len(agentKPIs) == 0 || len(results) == 0 {
		return
	}
	indexByAgentID := make(map[string]int, len(agentKPIs))
	for i, kpi := range agentKPIs {
		indexByAgentID[kpi.AgentID] = i
	}
	for taskID, raw := range results {
		summary := workflowSummaryFromAny(raw)
		if len(summary) == 0 {
			continue
		}
		agentID := strings.TrimSpace(stringValue(summary["agent_id"]))
		if agentID == "" {
			continue
		}
		index, exists := indexByAgentID[agentID]
		if !exists {
			continue
		}
		kpi := &agentKPIs[index]
		if kpi.ExecutionCount == 0 {
			kpi.ExecutionCount++
			status := strings.ToLower(strings.TrimSpace(stringValue(summary["result_status"])))
			if status == "" {
				status = strings.ToLower(strings.TrimSpace(stringValue(summary["status"])))
			}
			switch status {
			case "done", "completed", "success", "succeeded":
				kpi.SuccessCount++
			case "failed", "error":
				kpi.FailureCount++
			}
		}
		if kpi.LastTaskID == "" {
			kpi.LastTaskID = taskID
		}
		if task, ok := tasksByID[taskID]; ok {
			if task.Capability != "" && !contains(kpi.ObservedTaskTypes, task.Capability) {
				kpi.ObservedTaskTypes = append(kpi.ObservedTaskTypes, task.Capability)
			}
			for _, file := range task.Files {
				if file != "" && !contains(kpi.ObservedFiles, file) {
					kpi.ObservedFiles = append(kpi.ObservedFiles, file)
				}
			}
		}
	}
}

func workflowSummaryFromAny(value any) map[string]any {
	if value == nil {
		return nil
	}
	if typed, ok := value.(map[string]any); ok {
		return typed
	}
	payload, err := json.Marshal(value)
	if err != nil {
		return nil
	}
	var summary map[string]any
	if err := json.Unmarshal(payload, &summary); err != nil {
		return nil
	}
	return summary
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case fmt.Stringer:
		return typed.String()
	default:
		return ""
	}
}

func stringSliceFromAny(value any) []string {
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if value := strings.TrimSpace(stringValue(item)); value != "" {
				out = append(out, value)
			}
		}
		return out
	default:
		return nil
	}
}

func mapFromAny(value any) map[string]any {
	mapped, ok := value.(map[string]any)
	if !ok || mapped == nil {
		return map[string]any{}
	}
	return mapped
}

func lenFromAny(value any) int {
	if value == nil {
		return 0
	}
	rv := reflect.ValueOf(value)
	switch rv.Kind() {
	case reflect.Array, reflect.Slice, reflect.Map, reflect.String:
		return rv.Len()
	default:
		return 0
	}
}

func topicsCount(value any) int {
	mapped := mapFromAny(value)
	if len(mapped) == 0 {
		return 0
	}
	return lenFromAny(mapped["topics"])
}

func intFromAny(value any) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int32:
		return int(typed)
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		parsed, _ := typed.Int64()
		return int(parsed)
	case string:
		parsed, _ := strconv.Atoi(typed)
		return parsed
	default:
		return 0
	}
}

func saturatingDelta(after, before uint64) uint64 {
	if after < before {
		return 0
	}
	return after - before
}

func valueOrDefault(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func contains(items []string, target string) bool {
	for _, item := range items {
		if item == target {
			return true
		}
	}
	return false
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func maxInt(left, right int) int {
	if left > right {
		return left
	}
	return right
}

func printReport(report Report, jsonOutput bool) {
	if jsonOutput {
		encoder := json.NewEncoder(os.Stdout)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(report); err != nil {
			fatal(err, true)
		}
		return
	}

	fmt.Printf("verify-orchestrator %s\n", report.GeneratedAt.Format(time.RFC3339))
	fmt.Printf("environment: go=%s os=%s arch=%s cpu=%d gomaxprocs=%d\n",
		report.Environment.GoVersion,
		report.Environment.OS,
		report.Environment.Arch,
		report.Environment.CPUCount,
		report.Environment.GOMAXPROCS,
	)
	for _, step := range report.Steps {
		status := "PASS"
		if !step.Success {
			status = "FAIL"
		}
		fmt.Printf("[%s] %s (%s)\n", status, step.Name, step.Duration)
		if step.Output != "" {
			fmt.Println(indent(step.Output, "  "))
		}
		if step.Error != "" {
			fmt.Println(indent(step.Error, "  error: "))
		}
	}
	if report.RuntimeProfile != nil {
		fmt.Printf("runtime-profile: duration=%s goroutines(before=%d peak=%d after=%d) heap_peak=%dB planned=%d completed=%d parallel_branches=%d observed_parallel=%d mean(queue=%s exec=%s total=%s) warnings=%d\n",
			report.RuntimeProfile.Duration,
			report.RuntimeProfile.GoroutinesBefore,
			report.RuntimeProfile.GoroutinesPeak,
			report.RuntimeProfile.GoroutinesAfter,
			report.RuntimeProfile.HeapAllocPeakBytes,
			report.RuntimeProfile.PlannedTaskCount,
			report.RuntimeProfile.CompletedTaskCount,
			report.RuntimeProfile.ParallelBranchCount,
			report.RuntimeProfile.MaxObservedParallelism,
			report.RuntimeProfile.MeanQueueLatency,
			report.RuntimeProfile.MeanExecutionLatency,
			report.RuntimeProfile.MeanTotalLatency,
			len(report.RuntimeProfile.Warnings),
		)
		if len(report.RuntimeProfile.WorkflowTraces) > 0 {
			fmt.Println("workflow-traces:")
			for _, trace := range report.RuntimeProfile.WorkflowTraces {
				fmt.Printf("  - task=%s capability=%s agent=%s status=%s result=%s queue=%s exec=%s total=%s deps=%d files=%d branch=%s cluster=%s events=%s\n",
					trace.TaskID,
					trace.Capability,
					trace.AgentID,
					trace.Status,
					trace.ResultStatus,
					trace.QueueLatency,
					trace.ExecutionLatency,
					trace.TotalLatency,
					len(trace.Dependencies),
					len(trace.Files),
					trace.BranchID,
					trace.ClusterID,
					strings.Join(trace.EventKinds, ","),
				)
			}
		}
		if len(report.RuntimeProfile.Distribution.ByCapability) > 0 || len(report.RuntimeProfile.Distribution.ByAgent) > 0 {
			fmt.Printf("distribution: capabilities=%v agents=%v providers=%v models=%v\n",
				report.RuntimeProfile.Distribution.ByCapability,
				report.RuntimeProfile.Distribution.ByAgent,
				report.RuntimeProfile.Distribution.ByProvider,
				report.RuntimeProfile.Distribution.ByModel,
			)
		}
		for _, warning := range report.RuntimeProfile.Warnings {
			fmt.Printf("runtime-warning: %s\n", warning)
		}
	}
	if len(report.AgentKPIs) > 0 {
		fmt.Println("agent-kpis:")
		for _, kpi := range report.AgentKPIs {
			fmt.Printf("  - %s type=%s exec=%d success=%d fail=%d peak=%d avg=%s caps=%s\n",
				kpi.AgentID,
				kpi.AgentType,
				kpi.ExecutionCount,
				kpi.SuccessCount,
				kpi.FailureCount,
				kpi.PeakConcurrency,
				kpi.AverageLatency,
				strings.Join(kpi.Capabilities, ","),
			)
		}
	}
	if report.CoordinatorKPI != nil {
		fmt.Printf("coordinator-kpi: agents=%d runtime_agents=%d workflows=%d runtime_streams=%d inventory_streams=%d\n",
			report.CoordinatorKPI.RegisteredAgents,
			report.CoordinatorKPI.RuntimeAgentCount,
			report.CoordinatorKPI.WorkflowCount,
			report.CoordinatorKPI.RuntimeStreamCount,
			report.CoordinatorKPI.InventoryStreamCount,
		)
	}
	for _, note := range report.Notes {
		fmt.Printf("note: %s\n", note)
	}
}

func indent(input, prefix string) string {
	lines := strings.Split(strings.TrimSpace(input), "\n")
	for i, line := range lines {
		lines[i] = prefix + line
	}
	return strings.Join(lines, "\n")
}

func fatal(err error, jsonOutput bool) {
	report := Report{
		GeneratedAt: time.Now().UTC(),
		Success:     false,
		Notes:       []string{err.Error()},
	}
	printReport(report, jsonOutput)
	os.Exit(1)
}
