package orchestrator

import (
	"context"
	"errors"
	"path"
	"strings"
	"sync"
)

// Step is a unit of work in a plan. Dependencies must finish successfully
// before the step can start; equal normalized conflict keys serialize work.
type Step struct {
	ID                      string
	DependsOn, ConflictKeys []string
}

// Plan is a directed acyclic graph of steps.
type Plan struct {
	ID    string
	Steps []Step
}

// Validate rejects duplicate step IDs, unknown dependencies, and cycles.
func (p Plan) Validate() error {
	known := map[string]bool{}
	for _, step := range p.Steps {
		if step.ID == "" || known[step.ID] {
			return errors.New("step ids must be unique and non-empty")
		}
		known[step.ID] = true
	}
	for _, step := range p.Steps {
		for _, dependency := range step.DependsOn {
			if !known[dependency] {
				return errors.New("unknown dependency")
			}
		}
	}
	visiting, visited := map[string]bool{}, map[string]bool{}
	var visit func(string) error
	visit = func(id string) error {
		if visiting[id] {
			return errors.New("plan contains cycle")
		}
		if visited[id] {
			return nil
		}
		visiting[id] = true
		for _, step := range p.Steps {
			if step.ID == id {
				for _, d := range step.DependsOn {
					if err := visit(d); err != nil {
						return err
					}
				}
			}
		}
		visiting[id] = false
		visited[id] = true
		return nil
	}
	for _, step := range p.Steps {
		if err := visit(step.ID); err != nil {
			return err
		}
	}
	return nil
}

// StepResult identifies the step an Executor has finished.
type StepResult struct{ StepID string }

// Executor executes a single step and must return after its context is cancelled.
type Executor interface {
	Execute(context.Context, Step) (StepResult, error)
}

// ExecutorFunc adapts a function to Executor.
type ExecutorFunc func(context.Context, Step) (StepResult, error)

// Execute calls f for the supplied step.
func (f ExecutorFunc) Execute(ctx context.Context, s Step) (StepResult, error) { return f(ctx, s) }

// RecordingExecutor is a deterministic test executor that records starts and
// returns configured failures. Production code should provide a real Executor.
type RecordingExecutor struct {
	mu       sync.Mutex
	failures map[string]error
	starts   map[string]int
}

// NewRecordingExecutor creates a test executor with failures keyed by step ID.
func NewRecordingExecutor(failures map[string]error) *RecordingExecutor {
	return &RecordingExecutor{failures: failures, starts: map[string]int{}}
}

// Execute records the step and returns its configured result.
func (e *RecordingExecutor) Execute(_ context.Context, step Step) (StepResult, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.starts[step.ID]++
	return StepResult{StepID: step.ID}, e.failures[step.ID]
}

// WasStarted reports whether Execute has been called for id.
func (e *RecordingExecutor) WasStarted(id string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.starts[id] > 0
}

// StartCount returns how many times Execute has been called for id.
func (e *RecordingExecutor) StartCount(id string) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.starts[id]
}

// ClearFailure removes the configured error for id so a plan can be resumed.
func (e *RecordingExecutor) ClearFailure(id string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.failures, id)
}

// SchedulerConfig controls how a Scheduler runs work.
type SchedulerConfig struct {
	MaxParallelism int
}

// Scheduler runs validated plans while preserving dependencies and conflicts.
type Scheduler struct {
	mu        sync.Mutex
	executor  Executor
	config    SchedulerConfig
	plans     map[string]Plan
	completed map[string]map[string]bool
}

// NewScheduler creates a scheduler. A non-positive parallelism limit becomes one.
func NewScheduler(executor Executor, config SchedulerConfig) (*Scheduler, error) {
	if executor == nil {
		return nil, errors.New("executor is required")
	}
	if config.MaxParallelism < 1 {
		config.MaxParallelism = 1
	}
	return &Scheduler{executor: executor, config: config, plans: map[string]Plan{}, completed: map[string]map[string]bool{}}, nil
}

// RunResult reports the terminal status of a scheduler invocation.
type RunResult struct{ Status WorkflowStatus }

// Run validates and executes plan, retaining successful checkpoints for Resume.
func (s *Scheduler) Run(ctx context.Context, plan Plan) (RunResult, error) {
	if !s.mu.TryLock() {
		return RunResult{Status: WorkflowFailed}, errors.New("scheduler is already running a plan")
	}
	defer s.mu.Unlock()
	if err := plan.Validate(); err != nil {
		return RunResult{Status: WorkflowFailed}, err
	}
	s.plans[plan.ID] = plan
	if s.completed[plan.ID] == nil {
		s.completed[plan.ID] = map[string]bool{}
	}
	return s.execute(ctx, plan)
}

// Resume continues a previously run plan without rerunning completed steps.
func (s *Scheduler) Resume(ctx context.Context, id string) (RunResult, error) {
	if !s.mu.TryLock() {
		return RunResult{Status: WorkflowFailed}, errors.New("scheduler is already running a plan")
	}
	defer s.mu.Unlock()
	plan, ok := s.plans[id]
	if !ok {
		return RunResult{Status: WorkflowFailed}, errors.New("unknown plan")
	}
	return s.execute(ctx, plan)
}

// execute runs ready non-conflicting steps until completion or the first failure.
func (s *Scheduler) execute(ctx context.Context, plan Plan) (RunResult, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	pending := map[string]Step{}
	for _, step := range plan.Steps {
		if !s.completed[plan.ID][step.ID] {
			pending[step.ID] = step
		}
	}
	activeKeys := map[string]bool{}
	type done struct {
		step Step
		err  error
	}
	finished := make(chan done, len(plan.Steps))
	running := 0
	for len(pending) > 0 || running > 0 {
		launched := false
		for id, step := range pending {
			if running >= s.config.MaxParallelism || !dependenciesDone(step, s.completed[plan.ID]) || overlaps(step.ConflictKeys, activeKeys) {
				continue
			}
			for _, key := range normalizedConflictKeys(step.ConflictKeys) {
				activeKeys[key] = true
			}
			delete(pending, id)
			running++
			launched = true
			go func(step Step) { _, err := s.executor.Execute(ctx, step); finished <- done{step, err} }(step)
		}
		if !launched || running >= s.config.MaxParallelism {
			result := <-finished
			running--
			for _, key := range normalizedConflictKeys(result.step.ConflictKeys) {
				delete(activeKeys, key)
			}
			if result.err != nil {
				cancel()
				for running > 0 {
					other := <-finished
					running--
					for _, key := range normalizedConflictKeys(other.step.ConflictKeys) {
						delete(activeKeys, key)
					}
					if other.err == nil {
						s.completed[plan.ID][other.step.ID] = true
					}
				}
				return RunResult{Status: WorkflowFailed}, result.err
			}
			s.completed[plan.ID][result.step.ID] = true
		}
	}
	return RunResult{Status: WorkflowCompleted}, nil
}

// dependenciesDone reports whether every prerequisite has completed successfully.
func dependenciesDone(step Step, completed map[string]bool) bool {
	for _, id := range step.DependsOn {
		if !completed[id] {
			return false
		}
	}
	return true
}

// overlaps reports whether keys share any normalized key with an active step.
func overlaps(keys []string, active map[string]bool) bool {
	for _, key := range normalizedConflictKeys(keys) {
		if active[key] {
			return true
		}
	}
	return false
}

// normalizedConflictKeys canonicalizes paths and removes duplicate empty keys.
func normalizedConflictKeys(keys []string) []string {
	normalized := make([]string, 0, len(keys))
	seen := map[string]bool{}
	for _, key := range keys {
		key = path.Clean(strings.ReplaceAll(strings.TrimSpace(key), "\\", "/"))
		if key != "." && key != "" && !seen[key] {
			seen[key] = true
			normalized = append(normalized, key)
		}
	}
	return normalized
}
