package realtasks

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type fakeAgent struct {
	mu            sync.Mutex
	info          domain.AgentInfo
	delay         time.Duration
	executedTasks []domain.Task
	result        domain.AgentResult
}

func (a *fakeAgent) Info() domain.AgentInfo {
	return a.info
}

func (a *fakeAgent) CanAccept(domain.Task) bool {
	return true
}

func (a *fakeAgent) Execute(_ context.Context, task domain.Task) domain.AgentResult {
	if a.delay > 0 {
		time.Sleep(a.delay)
	}
	a.mu.Lock()
	a.executedTasks = append(a.executedTasks, task)
	a.mu.Unlock()
	result := a.result
	result.TaskID = task.ID
	result.AgentID = a.info.ID
	result.Provider = a.info.Provider
	result.ModelName = a.info.ModelName
	if result.Status == "" {
		result.Status = domain.TaskStatusCompleted
	}
	if result.CompletedAt.IsZero() {
		result.CompletedAt = time.Now().UTC()
	}
	if result.Output.Artifacts == nil {
		result.Output.Artifacts = map[string]any{}
	}
	return result
}

func newIntegrationOrchestrator(t *testing.T, preRegisteredAgents ...agents.Agent) (*kernel.Orchestrator, state.Store, *kernel.Registry) {
	t.Helper()
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	if strings.TrimSpace(os.Getenv("GO_CORE_SUBMIT_MODE")) == "" {
		t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	}
	t.Setenv("GO_CORE_SUBMIT_WORKERS", "4")
	t.Setenv("GO_CORE_RESULT_WORKERS", "4")
	t.Setenv("GO_CORE_AGENT_WORKERS", "4")
	t.Setenv("GO_CORE_MAX_CONCURRENT_TASKS", "16")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_AGENT", "8")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_MODEL", "8")
	t.Setenv("GO_CORE_AGENT_POLL_INTERVAL_MS", "50")

	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	registry := kernel.NewRegistry()
	for _, agent := range preRegisteredAgents {
		registry.RegisterAgent(agent)
	}
	selector := kernel.NewModelSelector(nil)
	planner := kernel.NewPlanner(selector)
	router := kernel.NewRouter(registry, selector)
	orchestrator := kernel.NewOrchestrator(
		registry,
		planner,
		router,
		store,
		realtime.NewHub("runtime", 64),
		realtime.NewHub("inventory", 32),
		nil,
	)
	return orchestrator, store, registry
}

type realTaskThresholds struct {
	MaxTotalLatency         time.Duration
	MinThroughputPerSecond  float64
	MinParallelWidth        int
	MaxMeanExecutionLatency time.Duration
}

type realTaskMetrics struct {
	Duration             time.Duration
	WorkflowCount        int
	ParallelWidth        int
	ThroughputPerSecond  float64
	MeanQueueLatency     time.Duration
	MeanExecutionLatency time.Duration
}

type taskEventKind string

const (
	taskEventAccepted       taskEventKind = "task.accepted"
	taskEventQueued         taskEventKind = "task.queued"
	taskEventDequeued       taskEventKind = "task.dequeued"
	taskEventRunning        taskEventKind = "task.running"
	taskEventResultReceived taskEventKind = "task.result_received"
	taskEventCompleted      taskEventKind = "task.completed"
	taskEventFailed         taskEventKind = "task.failed"
)

type taskEventTimeline struct {
	Accepted  time.Time
	Queued    time.Time
	Running   time.Time
	Completed time.Time
	Kinds     []taskEventKind
}

func collectMetrics(run domain.ExecutionPlanRun, events []domain.StreamEvent) realTaskMetrics {
	metrics := realTaskMetrics{
		WorkflowCount: len(run.Workflows),
		ParallelWidth: parallelWidthFromRun(run),
	}
	if !run.StartedAt.IsZero() && !run.CompletedAt.IsZero() {
		metrics.Duration = run.CompletedAt.Sub(run.StartedAt)
		if seconds := metrics.Duration.Seconds(); seconds > 0 {
			metrics.ThroughputPerSecond = float64(metrics.WorkflowCount) / seconds
		}
	}

	timelines := taskEventTimelines(events)
	var queueSum time.Duration
	var executionSum time.Duration
	var queueCount int
	var executionCount int
	for _, workflow := range run.Workflows {
		timeline := timelines[workflow.Task.ID]
		if !timeline.Queued.IsZero() && !timeline.Running.IsZero() {
			queueSum += timeline.Running.Sub(timeline.Queued)
			queueCount++
		}
		if !timeline.Running.IsZero() && !timeline.Completed.IsZero() {
			executionSum += timeline.Completed.Sub(timeline.Running)
			executionCount++
		}
	}
	if queueCount > 0 {
		metrics.MeanQueueLatency = queueSum / time.Duration(queueCount)
	}
	if executionCount > 0 {
		metrics.MeanExecutionLatency = executionSum / time.Duration(executionCount)
	}
	return metrics
}

func taskEventTimelines(events []domain.StreamEvent) map[string]taskEventTimeline {
	timelines := make(map[string]taskEventTimeline, len(events))
	for _, event := range events {
		if strings.TrimSpace(event.EntityID) == "" {
			continue
		}
		timeline := timelines[event.EntityID]
		kind := taskEventKind(event.Kind)
		timeline.Kinds = append(timeline.Kinds, kind)
		switch kind {
		case taskEventAccepted:
			if timeline.Accepted.IsZero() {
				timeline.Accepted = event.Timestamp
			}
		case taskEventQueued, taskEventDequeued:
			if timeline.Queued.IsZero() {
				timeline.Queued = event.Timestamp
			}
		case taskEventRunning:
			if timeline.Running.IsZero() {
				timeline.Running = event.Timestamp
			}
		case taskEventCompleted, taskEventFailed:
			if timeline.Completed.IsZero() {
				timeline.Completed = event.Timestamp
			}
		}
		timelines[event.EntityID] = timeline
	}
	return timelines
}

func parallelWidthFromRun(run domain.ExecutionPlanRun) int {
	maxWidth := 1
	for _, group := range run.PlanArtifact.ParallelGroups {
		if len(group) > maxWidth {
			maxWidth = len(group)
		}
	}
	return maxWidth
}

func overrideDurationFromEnv(name string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	ms, err := strconv.Atoi(raw)
	if err != nil || ms <= 0 {
		return fallback
	}
	return time.Duration(ms) * time.Millisecond
}

func overrideFloatFromEnv(name string, fallback float64) float64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func assertPerformanceThresholds(t *testing.T, metrics realTaskMetrics, thresholds realTaskThresholds) {
	t.Helper()
	if metrics.Duration <= 0 {
		t.Fatalf("Duration = %s, want > 0", metrics.Duration)
	}
	if metrics.WorkflowCount < 1 {
		t.Fatalf("WorkflowCount = %d, want >= 1", metrics.WorkflowCount)
	}
	if metrics.ParallelWidth < thresholds.MinParallelWidth {
		t.Fatalf("ParallelWidth = %d, want >= %d", metrics.ParallelWidth, thresholds.MinParallelWidth)
	}
	if metrics.Duration > thresholds.MaxTotalLatency {
		t.Fatalf("Duration = %s, want <= %s", metrics.Duration, thresholds.MaxTotalLatency)
	}
	if metrics.ThroughputPerSecond < thresholds.MinThroughputPerSecond {
		t.Fatalf("ThroughputPerSecond = %.2f, want >= %.2f", metrics.ThroughputPerSecond, thresholds.MinThroughputPerSecond)
	}
	if metrics.MeanExecutionLatency > thresholds.MaxMeanExecutionLatency {
		t.Fatalf("MeanExecutionLatency = %s, want <= %s", metrics.MeanExecutionLatency, thresholds.MaxMeanExecutionLatency)
	}
}

var _ agents.Agent = (*fakeAgent)(nil)
