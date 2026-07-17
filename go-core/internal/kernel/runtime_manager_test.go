package kernel

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestRuntimeManagerProbeProviderRuntimeRequiresConfirmedModel(t *testing.T) {
	now := time.Now().UTC()
	registry := NewRegistry()
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "reviewer-mistral",
		Type:         "review",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"review"},
		Status:       domain.AgentStatusReady,
	}})

	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          false,
				Status:             "verifying",
				ObservedAt:         now,
				RefreshIntervalSec: 300,
				Models: []domain.ProviderModelStatus{{
					Provider:           "mistral",
					ModelName:          "mistral-large-latest",
					Status:             "verification_pending",
					VerificationStatus: "verifying",
					TransportStatus:    "transport_pending",
					Reason:             "model discovered in provider inventory and awaiting transport verification",
					ObservedAt:         now,
				}},
			},
		},
	}

	runtime := NewRuntimeManager(registry, providerRegistry, func(context.Context, bool) map[string]domain.ProviderHealth {
		return map[string]domain.ProviderHealth{
			"mistral": {
				Provider:   "mistral",
				Configured: true,
				Available:  true,
				Status:     "ready",
				ObservedAt: time.Now().UTC(),
			},
		}
	})

	payload := runtime.ProbeProviderRuntime(context.Background(), "mistral")
	if payload["status"] != "ok" {
		t.Fatalf("unexpected payload status: %#v", payload)
	}
	state, ok := runtime.State("reviewer-mistral")
	if !ok {
		t.Fatal("expected runtime state")
	}
	if state.Status != domain.AgentStatusDegraded {
		t.Fatalf("state.Status = %s, want degraded", state.Status)
	}
	if state.DisabledReason == "" {
		t.Fatalf("expected disabled reason, got empty state: %#v", state)
	}
}

func TestRuntimeManagerProbeProviderRuntimeMarksConfirmedModelReady(t *testing.T) {
	registry := NewRegistry()
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "planner-mistral",
		Type:         "plan",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"plan"},
		Status:       domain.AgentStatusReady,
	}})

	now := time.Now().UTC()
	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          true,
				Status:             "ready",
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

	runtime := NewRuntimeManager(registry, providerRegistry, func(context.Context, bool) map[string]domain.ProviderHealth {
		return map[string]domain.ProviderHealth{
			"mistral": {
				Provider:   "mistral",
				Configured: true,
				Available:  true,
				Status:     "ready",
				ObservedAt: now,
			},
		}
	})

	runtime.ProbeProviderRuntime(context.Background(), "mistral")
	state, ok := runtime.State("planner-mistral")
	if !ok {
		t.Fatal("expected runtime state")
	}
	if state.Status != domain.AgentStatusReady {
		t.Fatalf("state.Status = %s, want ready", state.Status)
	}
}

func TestRuntimeManagerKeepsProviderAgentReadyWhenConfirmedAssignedModelExists(t *testing.T) {
	registry := NewRegistry()
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "planner-mistral",
		Type:         "plan",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"plan"},
		Status:       domain.AgentStatusReady,
	}, supportsAssignedModelOverride: true})

	now := time.Now().UTC()
	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          true,
				Status:             "ready",
				ObservedAt:         now,
				RefreshIntervalSec: 300,
				Models: []domain.ProviderModelStatus{
					{
						Provider:           "mistral",
						ModelName:          "mistral-large-latest",
						Available:          false,
						Status:             "verification_pending",
						VerificationStatus: "verifying",
						TransportStatus:    "transport_pending",
						Reason:             "model verification is still in progress",
						ObservedAt:         now,
					},
					{
						Provider:           "mistral",
						ModelName:          "codestral-latest",
						Available:          true,
						Status:             "ready",
						VerificationStatus: "confirmed",
						TransportStatus:    "transport_verified",
						ObservedAt:         now,
					},
				},
			},
		},
	}

	runtime := NewRuntimeManager(registry, providerRegistry, func(context.Context, bool) map[string]domain.ProviderHealth {
		return map[string]domain.ProviderHealth{
			"mistral": {
				Provider:   "mistral",
				Configured: true,
				Available:  true,
				Status:     "ready",
				ObservedAt: now,
			},
		}
	})

	runtime.ProbeProviderRuntime(context.Background(), "mistral")
	state, ok := runtime.State("planner-mistral")
	if !ok {
		t.Fatal("expected runtime state")
	}
	if state.Status != domain.AgentStatusReady {
		t.Fatalf("state.Status = %s, want ready", state.Status)
	}
}

func TestRuntimeManagerProviderModelReadyStatusRejectsStaleSnapshot(t *testing.T) {
	registry := NewRegistry()
	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          true,
				Status:             "ready",
				ObservedAt:         time.Now().UTC().Add(-15 * time.Minute),
				RefreshIntervalSec: 300,
				Models: []domain.ProviderModelStatus{{
					Provider:           "mistral",
					ModelName:          "mistral-large-latest",
					Available:          true,
					Status:             "ready",
					VerificationStatus: "confirmed",
					TransportStatus:    "transport_verified",
					ObservedAt:         time.Now().UTC().Add(-15 * time.Minute),
				}},
			},
		},
	}

	runtime := NewRuntimeManager(registry, providerRegistry, nil)
	ready, known := runtime.ProviderModelReadyStatus("mistral", "mistral-large-latest")
	if known || ready {
		t.Fatalf("ProviderModelReadyStatus() = (%t, %t), want (false, false) for stale snapshot", ready, known)
	}
	if runtime.SupportsAssignedModel(&routerTestAgent{info: domain.AgentInfo{Provider: "mistral", ModelName: "mistral-large-latest"}}, "mistral-large-latest") {
		t.Fatal("SupportsAssignedModel() = true, want false for stale snapshot")
	}
}

func TestRuntimeManagerSyncProviderHealthMarksUnavailableProviderDegraded(t *testing.T) {
	registry := NewRegistry()
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "reviewer-mistral",
		Type:         "review",
		Provider:     "mistral",
		ModelName:    "mistral-large-latest",
		Capabilities: []string{"review"},
		Status:       domain.AgentStatusReady,
	}})

	now := time.Now().UTC()
	providerRegistry := &ProviderModelRegistry{
		snapshots: map[string]domain.ProviderCatalogSnapshot{
			"mistral": {
				Provider:           "mistral",
				Configured:         true,
				Available:          true,
				Status:             "ready",
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

	runtime := NewRuntimeManager(registry, providerRegistry, nil)
	states := runtime.SyncProviderHealth(map[string]domain.ProviderHealth{
		"mistral": {
			Provider:   "mistral",
			Configured: true,
			Available:  false,
			Status:     "unavailable",
			Error:      "service busy",
			ObservedAt: now,
		},
	})
	if len(states) != 1 {
		t.Fatalf("len(states) = %d, want 1", len(states))
	}
	if states[0].Status != domain.AgentStatusDegraded {
		t.Fatalf("states[0].Status = %s, want degraded", states[0].Status)
	}
	if states[0].DisabledReason != "service busy" {
		t.Fatalf("DisabledReason = %q, want service busy", states[0].DisabledReason)
	}
	weights := runtime.RoutingWeights()
	if got := weights["reviewer-mistral"]; got >= 1 {
		t.Fatalf("routing weight = %v, want degraded weight below 1", got)
	}
}

func TestRuntimeManagerCapacityPressureTracksProviderLoad(t *testing.T) {
	registry := NewRegistry()
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-a",
		Type:         "coder",
		Provider:     "local",
		ModelName:    "gemma",
		Capabilities: []string{"code"},
		Status:       domain.AgentStatusReady,
	}})
	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:           "coder-local-b",
		Type:         "coder",
		Provider:     "local",
		ModelName:    "gemma",
		Capabilities: []string{"code"},
		Status:       domain.AgentStatusReady,
	}})
	runtime := NewRuntimeManager(registry, nil, nil)
	if _, ok := runtime.UpdateCapacitySnapshot("coder-local-a", CapacitySnapshot{InFlight: 3, AgentSlotUsage: 1.0, ModelSlotUsage: 1.0, GlobalSlotUsage: 0.8}); !ok {
		t.Fatal("expected snapshot update for coder-local-a")
	}
	if _, ok := runtime.UpdateCapacitySnapshot("coder-local-b", CapacitySnapshot{InFlight: 1, AgentSlotUsage: 0.25, ModelSlotUsage: 0.25, GlobalSlotUsage: 0.2}); !ok {
		t.Fatal("expected snapshot update for coder-local-b")
	}
	pressure := runtime.CapacityPressure(domain.AgentInfo{ID: "coder-local-b", Type: "coder", Provider: "local"}, "code")
	if pressure < 0.55 {
		t.Fatalf("CapacityPressure() = %0.2f, want >= 0.55 with saturated local provider", pressure)
	}
	state, ok := runtime.State("coder-local-a")
	if !ok {
		t.Fatal("expected runtime state")
	}
	if state.PriorityScore >= 0.5 {
		t.Fatalf("PriorityScore = %0.2f, want degraded under heavy load", state.PriorityScore)
	}
}
