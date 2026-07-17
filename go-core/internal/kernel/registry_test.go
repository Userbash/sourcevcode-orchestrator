package kernel

import (
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/modules"
)

func TestRegistryRegisterAgentInitializesLookupsAndRuntimeState(t *testing.T) {
	registry := NewRegistry()
	agent := &routerTestAgent{info: domain.AgentInfo{
		ID:        "docs-openai",
		Type:      "docs",
		Provider:  "openai",
		ModelName: "gpt-5.5",
		Status:    domain.AgentStatusReady,
	}}

	registry.RegisterAgent(agent)

	gotAgent, ok := registry.AgentByID("docs-openai")
	if !ok {
		t.Fatal("AgentByID() = false, want true")
	}
	if gotAgent.Info().ID != "docs-openai" {
		t.Fatalf("AgentByID().Info().ID = %q, want docs-openai", gotAgent.Info().ID)
	}

	infos := registry.AgentInfos()
	if len(infos) != 1 {
		t.Fatalf("len(AgentInfos()) = %d, want 1", len(infos))
	}
	if infos[0].Status != domain.AgentStatusReady {
		t.Fatalf("AgentInfos()[0].Status = %q, want %q", infos[0].Status, domain.AgentStatusReady)
	}

	state, ok := registry.RuntimeState("docs-openai")
	if !ok {
		t.Fatal("RuntimeState() = false, want true")
	}
	if state.Provider != "openai" {
		t.Fatalf("RuntimeState().Provider = %q, want openai", state.Provider)
	}
	if state.Status != domain.AgentStatusReady {
		t.Fatalf("RuntimeState().Status = %q, want %q", state.Status, domain.AgentStatusReady)
	}
	if state.PriorityScore != 1 {
		t.Fatalf("RuntimeState().PriorityScore = %v, want 1", state.PriorityScore)
	}
	if state.UpdatedAt.IsZero() {
		t.Fatal("RuntimeState().UpdatedAt is zero")
	}
}

func TestRegistryRegisterAgentPreservesExistingRuntimeStateFields(t *testing.T) {
	registry := NewRegistry()
	registry.SetRuntimeState(domain.AgentRuntimeState{
		AgentID:       "coder-local",
		Provider:      "local",
		Status:        domain.AgentStatusBusy,
		PriorityScore: 3.5,
		UpdatedAt:     time.Now().UTC().Add(-time.Minute),
	})

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:       "coder-local",
		Type:     "coder",
		Provider: "ollama",
		Status:   domain.AgentStatusReady,
	}})

	state, ok := registry.RuntimeState("coder-local")
	if !ok {
		t.Fatal("RuntimeState() = false, want true")
	}
	if state.Provider != "ollama" {
		t.Fatalf("RuntimeState().Provider = %q, want ollama", state.Provider)
	}
	if state.Status != domain.AgentStatusBusy {
		t.Fatalf("RuntimeState().Status = %q, want preserved %q", state.Status, domain.AgentStatusBusy)
	}
	if state.PriorityScore != 3.5 {
		t.Fatalf("RuntimeState().PriorityScore = %v, want 3.5", state.PriorityScore)
	}
}

func TestRegistryOnAgentRegisteredInvokesListenersAfterRegistration(t *testing.T) {
	registry := NewRegistry()
	seen := 0

	registry.OnAgentRegistered(nil)
	registry.OnAgentRegistered(func(agent agents.Agent) {
		seen++
		got, ok := registry.AgentByID(agent.Info().ID)
		if !ok {
			t.Fatalf("AgentByID(%q) = false inside listener, want true", agent.Info().ID)
		}
		if got.Info().ID != agent.Info().ID {
			t.Fatalf("listener observed agent %q, want %q", got.Info().ID, agent.Info().ID)
		}
	})

	registry.RegisterAgent(&routerTestAgent{info: domain.AgentInfo{
		ID:       "reviewer",
		Type:     "review",
		Provider: "mistral",
		Status:   domain.AgentStatusReady,
	}})

	if seen != 1 {
		t.Fatalf("listener calls = %d, want 1", seen)
	}
}

func TestRegistrySetRuntimeStateMergesExistingValuesAndIgnoresEmptyAgentID(t *testing.T) {
	registry := NewRegistry()
	registry.SetRuntimeState(domain.AgentRuntimeState{})
	if len(registry.RuntimeStates()) != 0 {
		t.Fatalf("len(RuntimeStates()) = %d, want 0 after empty AgentID", len(registry.RuntimeStates()))
	}

	registry.SetRuntimeState(domain.AgentRuntimeState{
		AgentID:       "planner",
		Provider:      "openai",
		Status:        domain.AgentStatusReady,
		PriorityScore: 2,
		UpdatedAt:     time.Now().UTC().Add(-time.Minute),
	})
	registry.SetRuntimeState(domain.AgentRuntimeState{
		AgentID:       "planner",
		PriorityScore: 4,
	})

	state, ok := registry.RuntimeState("planner")
	if !ok {
		t.Fatal("RuntimeState() = false, want true")
	}
	if state.Provider != "openai" {
		t.Fatalf("RuntimeState().Provider = %q, want openai", state.Provider)
	}
	if state.Status != domain.AgentStatusReady {
		t.Fatalf("RuntimeState().Status = %q, want %q", state.Status, domain.AgentStatusReady)
	}
	if state.PriorityScore != 4 {
		t.Fatalf("RuntimeState().PriorityScore = %v, want 4", state.PriorityScore)
	}
	if state.UpdatedAt.IsZero() {
		t.Fatal("RuntimeState().UpdatedAt is zero")
	}
}

func TestRegistryRegisterModuleAndExposeSortedInfos(t *testing.T) {
	registry := NewRegistry()

	registry.RegisterModule(modules.NewBasicModule("zeta", "catalog", map[string]any{"rank": 2}))
	registry.RegisterModule(modules.NewBasicModule("alpha", "router", map[string]any{"rank": 1}))

	moduleItems := registry.Modules()
	if len(moduleItems) != 2 {
		t.Fatalf("len(Modules()) = %d, want 2", len(moduleItems))
	}
	if moduleItems[0].Info().Name != "alpha" || moduleItems[1].Info().Name != "zeta" {
		t.Fatalf("Modules() order = [%q %q], want [alpha zeta]", moduleItems[0].Info().Name, moduleItems[1].Info().Name)
	}

	infos := registry.ModuleInfos()
	if len(infos) != 2 {
		t.Fatalf("len(ModuleInfos()) = %d, want 2", len(infos))
	}
	if infos[0].Name != "alpha" || infos[1].Name != "zeta" {
		t.Fatalf("ModuleInfos() order = [%q %q], want [alpha zeta]", infos[0].Name, infos[1].Name)
	}
	if infos[0].Metadata["rank"] != 1 {
		t.Fatalf("ModuleInfos()[0].Metadata[rank] = %v, want 1", infos[0].Metadata["rank"])
	}
}
