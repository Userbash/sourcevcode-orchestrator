package kernel

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type workflowCountOnlyStore struct {
	state.Store
	countCalls int
	listCalls  int
}

func (s *workflowCountOnlyStore) WorkflowCount(ctx context.Context) (int, error) {
	s.countCalls++
	return s.Store.WorkflowCount(ctx)
}

func (s *workflowCountOnlyStore) ListWorkflows(ctx context.Context) ([]domain.WorkflowRecord, error) {
	s.listCalls++
	return s.Store.ListWorkflows(ctx)
}

func TestStateSnapshotSyncsRuntimeStateFromProviderHealth(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	now := time.Now().UTC()
	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          false,
				Status:             "unavailable",
				Error:              "service busy",
				ObservedAt:         now,
				RefreshIntervalSec: 300,
				Models: []domain.ProviderModelStatus{{
					Provider:           "mistral",
					ModelName:          "mistral-large-latest",
					Available:          true,
					Status:             "ready",
					VerificationStatus: "confirmed",
					TransportStatus:    "transport_verified",
					ObservedAt:         now,
				}},
			},
		},
	}
	orchestrator := NewOrchestrator(registry, planner, router, store, realtime.NewHub("runtime", 32), realtime.NewHub("inventory", 16), providerRegistry)
	defer orchestrator.Close()

	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "reviewer-mistral",
		Type:         "review",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"review"},
		Status:       domain.AgentStatusReady,
	}})

	snapshot := orchestrator.StateSnapshot(context.Background())
	agents, ok := snapshot["agents"].([]domain.AgentInfo)
	if !ok || len(agents) != 1 {
		t.Fatalf("agents = %#v, want one typed agent entry", snapshot["agents"])
	}
	if agents[0].Status != domain.AgentStatusDegraded {
		t.Fatalf("agents[0].Status = %s, want degraded", agents[0].Status)
	}
	runtimeAgents, ok := snapshot["runtime_agents"].([]domain.AgentRuntimeState)
	if !ok || len(runtimeAgents) != 1 {
		t.Fatalf("runtime_agents = %#v, want one typed runtime state", snapshot["runtime_agents"])
	}
	if runtimeAgents[0].Status != domain.AgentStatusDegraded {
		t.Fatalf("runtimeAgents[0].Status = %s, want degraded", runtimeAgents[0].Status)
	}
	providerHealth, ok := snapshot["provider_health"].(map[string]domain.ProviderHealth)
	if !ok {
		t.Fatalf("provider_health = %#v, want typed map", snapshot["provider_health"])
	}
	if providerHealth["mistral"].Status != "unavailable" {
		t.Fatalf("providerHealth[mistral].Status = %q, want unavailable", providerHealth["mistral"].Status)
	}
	modelCapabilities, ok := snapshot["model_capabilities"].(map[string]map[string]domain.ModelCapabilities)
	if !ok {
		t.Fatalf("model_capabilities = %#v, want typed map", snapshot["model_capabilities"])
	}
	if !modelCapabilities["mistral"]["mistral-large-latest"].Streaming {
		t.Fatalf("expected mistral-large-latest streaming capabilities, got %#v", modelCapabilities["mistral"]["mistral-large-latest"])
	}
}

type healthReportingTestAgent struct {
	*budgetTestAgent
	reporter *stubHealthReporter
}

func (a *healthReportingTestAgent) Probe(ctx context.Context) domain.ProviderHealth {
	return a.reporter.Probe(ctx)
}

func TestStateSnapshotKeepsConfiguredSyntheticProviderReadyWithoutProbe(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	orchestrator := NewOrchestrator(registry, planner, router, store, realtime.NewHub("runtime", 32), realtime.NewHub("inventory", 16), nil)
	defer orchestrator.Close()

	registry.RegisterAgent(&budgetTestAgent{info: domain.AgentInfo{
		ID:           "planner-kernel",
		Type:         "plan",
		Provider:     "kernel",
		ModelName:    "synthetic-default",
		Capabilities: []string{"plan"},
		Status:       domain.AgentStatusReady,
	}})

	snapshot := orchestrator.StateSnapshot(context.Background())
	providerHealth, ok := snapshot["provider_health"].(map[string]domain.ProviderHealth)
	if !ok {
		t.Fatalf("provider_health = %#v, want typed map", snapshot["provider_health"])
	}
	kernelHealth := providerHealth["kernel"]
	if !kernelHealth.Configured || !kernelHealth.Available || kernelHealth.Status != "ready" {
		t.Fatalf("kernel health = %#v, want configured available ready", kernelHealth)
	}
	runtimeAgents, ok := snapshot["runtime_agents"].([]domain.AgentRuntimeState)
	if !ok || len(runtimeAgents) != 1 {
		t.Fatalf("runtime_agents = %#v, want one typed runtime state", snapshot["runtime_agents"])
	}
	if runtimeAgents[0].Status != domain.AgentStatusReady {
		t.Fatalf("runtimeAgents[0].Status = %s, want ready", runtimeAgents[0].Status)
	}
	if runtimeAgents[0].DisabledReason != "" {
		t.Fatalf("runtimeAgents[0].DisabledReason = %q, want empty", runtimeAgents[0].DisabledReason)
	}
}

func TestProviderHealthReturnsCachedProbeStateWithoutTriggeringProbe(t *testing.T) {
	t.Setenv("GO_CORE_PROVIDER_HEALTH_WORKERS", "1")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE", "4")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_TTL", "100ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_COOLDOWN", "20ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN", "40ms")

	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	registry := NewRegistry()
	selector := NewModelSelector(nil)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	orchestrator := NewOrchestrator(registry, planner, router, store, realtime.NewHub("runtime", 32), realtime.NewHub("inventory", 16), nil)
	defer orchestrator.Close()

	reporter := &stubHealthReporter{health: domain.ProviderHealth{
		Provider:   "openai",
		Configured: true,
		Available:  false,
		Status:     "degraded",
		Error:      "service busy",
	}}
	registry.RegisterAgent(&healthReportingTestAgent{
		budgetTestAgent: &budgetTestAgent{info: domain.AgentInfo{
			ID:           "coder-openai",
			Type:         "coding",
			Provider:     "openai",
			ModelName:    "gpt-5.6-sol",
			Capabilities: []string{"code"},
			Status:       domain.AgentStatusReady,
		}},
		reporter: reporter,
	})

	_ = orchestrator.ProviderHealth(context.Background(), true)
	waitForCondition(t, 300*time.Millisecond, func() bool { return reporter.count.Load() == 1 })

	providerHealth := orchestrator.ProviderHealth(context.Background(), false)
	openai := providerHealth["openai"]
	if openai.Status != "degraded" || openai.Error != "service busy" || openai.Available {
		t.Fatalf("openai health = %#v, want cached degraded unavailable service busy", openai)
	}
	if reporter.count.Load() != 1 {
		t.Fatalf("probe count = %d, want cached read without new probe", reporter.count.Load())
	}
}

func TestStateSnapshotUsesWorkflowCountWithoutListingWorkflows(t *testing.T) {
	baseStore, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	if err := baseStore.SaveWorkflow(context.Background(), domain.WorkflowRecord{
		Task:      domain.Task{ID: "snapshot-workflow"},
		UpdatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("SaveWorkflow() error = %v", err)
	}
	store := &workflowCountOnlyStore{Store: baseStore}
	orchestrator := NewWithStore(store)
	defer orchestrator.Close()

	snapshot := orchestrator.StateSnapshot(context.Background())
	if got := snapshot["workflow_count"]; got != 1 {
		t.Fatalf("workflow_count = %#v, want 1", got)
	}
	if store.countCalls == 0 {
		t.Fatalf("WorkflowCount() was not called")
	}
	if store.listCalls != 0 {
		t.Fatalf("ListWorkflows() called %d times, want 0", store.listCalls)
	}
}

func TestRefreshInventoryPublishesWorkflowSummaryWithoutListingWorkflows(t *testing.T) {
	baseStore, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	if err := baseStore.SaveWorkflow(context.Background(), domain.WorkflowRecord{
		Task:      domain.Task{ID: "inventory-workflow"},
		UpdatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("SaveWorkflow() error = %v", err)
	}
	store := &workflowCountOnlyStore{Store: baseStore}
	orchestrator := NewWithStore(store)
	defer orchestrator.Close()

	orchestrator.RefreshInventory(context.Background())
	events := orchestrator.InventoryEventSnapshot("workflows")
	if len(events) == 0 {
		t.Fatalf("expected workflow inventory event")
	}
	payload := events[len(events)-1].Payload
	if got := payload["count"]; got != 1 {
		t.Fatalf("workflow summary count = %#v, want 1", got)
	}
	if payload["truncated"] != true {
		t.Fatalf("workflow summary truncated = %#v, want true", payload["truncated"])
	}
	if _, ok := payload["items"]; ok {
		t.Fatalf("unexpected workflow items payload: %#v", payload)
	}
	if store.countCalls == 0 {
		t.Fatalf("WorkflowCount() was not called")
	}
	if store.listCalls != 0 {
		t.Fatalf("ListWorkflows() called %d times, want 0", store.listCalls)
	}
}
