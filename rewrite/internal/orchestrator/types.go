package orchestrator

import (
	"context"
	"errors"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// CandidateStatus reports whether a provider model is usable for a new task.
type CandidateStatus string

const (
	// CandidateReady marks a model that may receive work.
	CandidateReady CandidateStatus = "ready"
	// CandidateStale marks a model with outdated availability information.
	CandidateStale CandidateStatus = "stale"
	// CandidateDegraded marks a model that is currently unreliable.
	CandidateDegraded CandidateStatus = "degraded"
)

// ModelCandidate is a model considered by Selector.
type ModelCandidate struct {
	Provider, Model string
	Status          CandidateStatus
	Capabilities    []string
	Score           int
}

// SelectionRequest describes the capability needed for a routing decision.
type SelectionRequest struct{ Capability string }

// Selection preserves the provider and model chosen for a task.
type Selection struct{ Provider, Model string }

// Selector deterministically selects the best eligible model.
type Selector struct{}

// NewSelector creates a selector with no external dependencies.
func NewSelector() *Selector { return &Selector{} }

// Select returns the highest-scoring ready model supporting the requested capability.
func (s *Selector) Select(_ context.Context, request SelectionRequest, candidates []ModelCandidate) (Selection, error) {
	eligible := make([]ModelCandidate, 0, len(candidates))
	for _, c := range candidates {
		if c.Status == CandidateReady && contains(c.Capabilities, request.Capability) {
			eligible = append(eligible, c)
		}
	}
	if len(eligible) == 0 {
		return Selection{}, errors.New("no ready compatible model")
	}
	sort.Slice(eligible, func(i, j int) bool {
		if eligible[i].Score != eligible[j].Score {
			return eligible[i].Score > eligible[j].Score
		}
		if eligible[i].Provider != eligible[j].Provider {
			return eligible[i].Provider < eligible[j].Provider
		}
		return eligible[i].Model < eligible[j].Model
	})
	return Selection{Provider: eligible[0].Provider, Model: eligible[0].Model}, nil
}

// contains reports whether values contains want.
func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

// Agent is an executable worker registered for a provider/model pair.
type Agent struct {
	ID, Provider, Model string
	Capabilities        []string
	Ready               bool
}

// InMemoryRegistry holds the agents available to a Router.
type InMemoryRegistry struct{ agents []Agent }

// NewInMemoryRegistry creates a registry from the provided agents.
func NewInMemoryRegistry(agents ...Agent) *InMemoryRegistry { return &InMemoryRegistry{agents: agents} }

// RouteRequest identifies the selected model and capability that must be routed.
type RouteRequest struct {
	Capability string
	Selection  Selection
}

// Route identifies the agent selected to execute work.
type Route struct{ AgentID, Provider, Model string }

// Router maps a Selection to a compatible ready agent.
type Router struct{ registry *InMemoryRegistry }

// NewRouter creates a router backed by registry.
func NewRouter(registry *InMemoryRegistry) (*Router, error) {
	if registry == nil {
		return nil, errors.New("agent registry is required")
	}
	return &Router{registry: registry}, nil
}

// Route returns a ready agent matching both parts of the selected provider/model pair.
func (r *Router) Route(request RouteRequest) (Route, error) {
	for _, agent := range r.registry.agents {
		if agent.Ready && agent.Provider == request.Selection.Provider && agent.Model == request.Selection.Model && contains(agent.Capabilities, request.Capability) {
			return Route{AgentID: agent.ID, Provider: agent.Provider, Model: agent.Model}, nil
		}
	}
	return Route{}, errors.New("no agent matches selection")
}

// WorkflowStatus is the lifecycle state of a submitted task.
type WorkflowStatus string

const (
	// WorkflowQueued is the initial state before work starts.
	WorkflowQueued WorkflowStatus = "queued"
	// WorkflowRunning marks a workflow currently being executed.
	WorkflowRunning WorkflowStatus = "running"
	// WorkflowCompleted marks successful terminal execution.
	WorkflowCompleted WorkflowStatus = "completed"
	// WorkflowFailed marks failed terminal execution.
	WorkflowFailed WorkflowStatus = "failed"
)

// AuditEvent records an append-only workflow decision or transition.
type AuditEvent struct{ Kind, Reason string }

// Workflow holds task identity, state, selection, and audit history.
type Workflow struct {
	mu                     sync.RWMutex
	TaskID, IdempotencyKey string
	status                 WorkflowStatus
	selection              Selection
	events                 []AuditEvent
}

// NewWorkflow creates a queued workflow with stable task and idempotency IDs.
func NewWorkflow(taskID, key string) *Workflow {
	return &Workflow{TaskID: taskID, IdempotencyKey: key, status: WorkflowQueued}
}

// Assign records the provider/model selection for the workflow.
func (w *Workflow) Assign(selection Selection) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.selection = selection
	w.events = append(w.events, AuditEvent{Kind: "assigned"})
}

// Fallback atomically replaces the selection and records why it changed.
func (w *Workflow) Fallback(selection Selection, reason string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if selection.Provider == "" || selection.Model == "" {
		return errors.New("fallback requires provider and model")
	}
	w.selection = selection
	w.events = append(w.events, AuditEvent{Kind: "fallback", Reason: reason})
	return nil
}

// Selection returns the workflow's current provider/model choice.
func (w *Workflow) Selection() Selection {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.selection
}

// Events returns a copy of the workflow audit history.
func (w *Workflow) Events() []AuditEvent {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return append([]AuditEvent(nil), w.events...)
}

// Transition moves a non-terminal workflow into a valid next state.
func (w *Workflow) Transition(next WorkflowStatus) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.status == WorkflowCompleted || w.status == WorkflowFailed {
		return errors.New("terminal workflow cannot transition")
	}
	if next != WorkflowRunning && next != WorkflowCompleted && next != WorkflowFailed {
		return errors.New("invalid transition")
	}
	w.status = next
	w.events = append(w.events, AuditEvent{Kind: string(next)})
	return nil
}

// Status returns the workflow's current lifecycle state.
func (w *Workflow) Status() WorkflowStatus {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.status
}

// SubmitRequest is the JSON-compatible input for creating a workflow.
type SubmitRequest struct {
	IdempotencyKey string `json:"idempotency_key"`
	Description    string `json:"description"`
	Capability     string `json:"capability"`
}

// SubmitResult reports the workflow created or replayed by a submission.
type SubmitResult struct {
	WorkflowID string         `json:"workflow_id"`
	Replayed   bool           `json:"replayed"`
	Status     WorkflowStatus `json:"status"`
}

// MemoryStore is a concurrency-safe in-memory workflow repository.
type MemoryStore struct {
	mu        sync.Mutex
	workflows map[string]*Workflow
	keys      map[string]string
}

// NewMemoryStore creates an empty workflow repository.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{workflows: map[string]*Workflow{}, keys: map[string]string{}}
}

// Count returns the number of stored workflows.
func (s *MemoryStore) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.workflows)
}

// Save stores workflow and indexes it by its idempotency key.
func (s *MemoryStore) Save(w *Workflow) {
	if w == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.workflows[w.TaskID] = w.clone()
	s.keys[w.IdempotencyKey] = w.TaskID
}

// ByKey returns the workflow for key, or nil when no workflow has that key.
func (s *MemoryStore) ByKey(key string) *Workflow {
	s.mu.Lock()
	defer s.mu.Unlock()
	workflow := s.workflows[s.keys[key]]
	return workflow.clone()
}

// Get returns the workflow for id, or nil when it does not exist.
func (s *MemoryStore) Get(id string) *Workflow {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.workflows[id].clone()
}

// clone returns an independent snapshot suitable for callers outside the store.
func (w *Workflow) clone() *Workflow {
	if w == nil {
		return nil
	}
	w.mu.RLock()
	defer w.mu.RUnlock()
	return &Workflow{TaskID: w.TaskID, IdempotencyKey: w.IdempotencyKey, status: w.status, selection: w.selection, events: append([]AuditEvent(nil), w.events...)}
}

// Service accepts task submissions and enforces idempotency.
type Service struct {
	store *MemoryStore
	ids   int
}

// NewService creates a submission service backed by store.
func NewService(store *MemoryStore) (*Service, error) {
	if store == nil {
		return nil, errors.New("workflow store is required")
	}
	return &Service{store: store}, nil
}

// Submit validates and persists a workflow, or replays the workflow for an existing key.
func (s *Service) Submit(_ context.Context, request SubmitRequest) (SubmitResult, error) {
	if strings.TrimSpace(request.Description) == "" {
		return SubmitResult{}, errors.New("description is required")
	}
	request.IdempotencyKey = strings.TrimSpace(request.IdempotencyKey)
	if request.IdempotencyKey == "" {
		return SubmitResult{}, errors.New("idempotency_key is required")
	}
	s.store.mu.Lock()
	defer s.store.mu.Unlock()
	if id := s.store.keys[request.IdempotencyKey]; id != "" {
		w := s.store.workflows[id]
		return SubmitResult{WorkflowID: id, Replayed: true, Status: w.Status()}, nil
	}
	for {
		s.ids++
		id := "workflow-" + strconvItoa(s.ids)
		if s.store.workflows[id] == nil {
			w := NewWorkflow(id, request.IdempotencyKey)
			s.store.workflows[id] = w
			s.store.keys[request.IdempotencyKey] = id
			return SubmitResult{WorkflowID: id, Status: w.status}, nil
		}
	}
}

// strconvItoa formats the sequential in-memory workflow number.
func strconvItoa(value int) string { return strconv.Itoa(value) }
