package kernel

import (
	"os"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/localmodels"
	"sourcevcode-orchestrator/go-core/internal/modules"
	"sourcevcode-orchestrator/go-core/internal/realtime"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func NewDefault(statePath string) (*Orchestrator, error) {
	store, err := state.OpenStore(statePath)
	if err != nil {
		return nil, err
	}
	return NewWithStore(store), nil
}

func NewWithStore(store state.Store) *Orchestrator {
	registry := NewRegistry()
	configs := agents.LoadOpenAICompatibleConfigs()
	providerRegistry := NewProviderModelRegistry(configs)
	selector := NewModelSelector(providerRegistry)
	planner := NewPlanner(selector)
	router := NewRouter(registry, selector)
	runtimeHub := realtime.NewHub("runtime", 128)
	inventoryHub := realtime.NewHub("inventory", 64)
	orchestrator := NewOrchestrator(registry, planner, router, store, runtimeHub, inventoryHub, providerRegistry)
	selector.AttachMemoryManager(orchestrator.memory)
	router.AttachMemoryManager(orchestrator.memory)
	router.AttachLiveRealtimeMetrics(orchestrator.liveRealtime)

	registerModule := func(name, kind, summary string) {
		registry.RegisterModule(modules.NewBasicModule(name, kind, map[string]any{
			"summary": summary,
		}))
	}

	registerModule("domain_contracts", "contracts", "Go contracts derived from core/core/models.py")
	registerModule("planner", "planning", "Execution planning and task decomposition")
	registerModule("router", "routing", "Agent routing and capability resolution")
	registerModule("model_selector", "policy", "Provider and model selection policy")
	registerModule("agent_registry", "registry", "Agent inventory and capability registry")
	registerModule("state_store", "state", "Persistent workflow, memory, routing, prompts, and VFS storage in PostgreSQL")
	registerModule("runtime_events", "realtime", "Runtime lifecycle event stream")
	registerModule("inventory_events", "realtime", "Agent and module inventory stream")
	registerModule("message_bus", "delivery", "Agent mailbox transport and ack history (in-memory or RabbitMQ)")
	registerModule("delivery_supervisor", "delivery", "Delivery handshake, mailbox fetch and timeout supervision ported from core/core/delivery_supervisor.py")
	registerModule("transport_api", "transport", "HTTP and SSE transport surface")
	registerModule("script_runtime", "bootstrap", "Native Go bootstrap, runtime preflight, AI-kernel service management, and DB inspection commands")
	registerModule("ws_session", "transport", "Transport-agnostic websocket session semantics ported from core/core/orchestrator_ws_session.py")
	registerModule("ws_dispatcher", "transport", "WebSocket action dispatcher, timeout, ack and stream lifecycle")
	registerModule("code_task_analyzer", "automation", "Native Go analyzer ported from core/core/code_automation/code_task_analyzer.py")
	registerModule("patch_planner", "automation", "Native Go patch planning stub ported from core/core/code_automation/patch_planner.py")
	registerModule("patch_validator", "automation", "Native Go patch validation stub ported from core/core/code_automation/patch_validator.py")
	registerModule("apply_patch_tool", "automation", "Native Go patch apply stub ported from core/core/code_automation/apply_patch_tool.py")
	registerModule("test_runner", "automation", "Native Go test summary stub ported from core/core/code_automation/test_runner.py")
	registerModule("code_review_agent", "automation", "Native Go review stub ported from core/core/code_automation/code_review_agent.py")
	registerModule("memory_control", "memory", "Native Go session memory persistence and runtime context ported from Python memory control flows")
	registerModule("validation_memory_gate", "validation", "Native Go cache guard, invalidation log and validation context ported from Python validation gate")

	localModelRuntime := localmodels.NewRuntime(localmodels.ConfigFromEnv())
	localModelManager := localmodels.NewManager(localModelRuntime)
	registry.RegisterModule(localModelManager)
	registry.RegisterModule(modules.NewCodeAutomationModule())
	orchestrator.AttachLocalModelManager(localModelManager)
	registerDefaultAgents(registry, configs)
	attachDefaultCodingRuntime(orchestrator)

	return orchestrator
}

func registerDefaultAgents(registry *Registry, configs map[string]agents.OpenAICompatibleConfig) {
	coordinatorConfig := configs["ai_kernel"]
	if !coordinatorConfig.Configured() {
		coordinatorConfig = configs["local"]
	}
	registerProviderAgent := func(descriptor agents.AgentDescriptor, config agents.OpenAICompatibleConfig) {
		registry.RegisterAgent(agents.NewOpenAICompatibleAgent(descriptor, config))
	}
	cloudProvider := agents.PreferredCloudProvider(configs)
	cloudConfig := configs[cloudProvider]

	registerProviderAgent(agents.AgentDescriptor{
		ID: "orchestrator", Type: "orchestration",
		Capabilities: []string{"sourcecraft", "plan", "code", "fix", "review", "test", "docs", "research"},
	}, coordinatorConfig)
	registerProviderAgent(agents.AgentDescriptor{ID: "planner-local", Type: "planning", Capabilities: []string{"plan"}}, configs["local"])
	registerProviderAgent(agents.AgentDescriptor{ID: "planner-ai-kernel", Type: "planning", Capabilities: []string{"plan", "review"}}, configs["ai_kernel"])
	registerProviderAgent(agents.AgentDescriptor{ID: "planner-mistral", Type: "planning", Capabilities: []string{"plan", "review"}}, configs["mistral"])
	registerProviderAgent(agents.AgentDescriptor{ID: "coder-local", Type: "coding", Capabilities: []string{"code", "fix", "test"}}, configs["local"])
	registerProviderAgent(agents.AgentDescriptor{ID: "coder-ai-kernel", Type: "coding", Capabilities: []string{"code", "fix", "review", "test"}}, configs["ai_kernel"])
	registerProviderAgent(agents.AgentDescriptor{ID: "coder-openai", Type: "coding", Capabilities: []string{"code", "fix", "review", "test"}}, cloudConfig)
	registerProviderAgent(agents.AgentDescriptor{ID: "reviewer-local", Type: "review", Capabilities: []string{"review"}}, configs["local"])
	registerProviderAgent(agents.AgentDescriptor{ID: "reviewer-ai-kernel", Type: "review", Capabilities: []string{"review", "security"}}, configs["ai_kernel"])
	registerProviderAgent(agents.AgentDescriptor{ID: "reviewer-openai", Type: "review", Capabilities: []string{"review", "security"}}, cloudConfig)
	registerProviderAgent(agents.AgentDescriptor{ID: "tester-local", Type: "testing", Capabilities: []string{"test"}}, configs["local"])
	registerProviderAgent(agents.AgentDescriptor{ID: "tester-ai-kernel", Type: "testing", Capabilities: []string{"test"}}, configs["ai_kernel"])
	registerProviderAgent(agents.AgentDescriptor{ID: "docs-local", Type: "documentation", Capabilities: []string{"docs"}}, configs["local"])
	registerProviderAgent(agents.AgentDescriptor{ID: "docs-ai-kernel", Type: "documentation", Capabilities: []string{"docs"}}, configs["ai_kernel"])
	registerProviderAgent(agents.AgentDescriptor{ID: "research-mistral", Type: "research", Capabilities: []string{"research", "docs"}}, configs["mistral"])
	registerProviderAgent(agents.AgentDescriptor{ID: "research-mimo", Type: "research", Capabilities: []string{"research", "docs"}}, configs["mimo"])
	registerProviderAgent(agents.AgentDescriptor{ID: "coder-antigravity", Type: "coding", Capabilities: []string{"code", "fix", "review", "test"}}, configs["antigravity"])
	registerProviderAgent(agents.AgentDescriptor{ID: "reviewer-antigravity", Type: "review", Capabilities: []string{"review", "security"}}, configs["antigravity"])
}

func attachDefaultCodingRuntime(orchestrator *Orchestrator) {
	if orchestrator == nil || !codingRuntimeEnabledFromEnv() {
		return
	}
	backend := strings.ToLower(strings.TrimSpace(os.Getenv("GO_CORE_CODING_RUNTIME_BACKEND")))
	if backend == "" {
		backend = "managed"
	}
	switch backend {
	case "managed", "internal", "realtime":
		name := strings.TrimSpace(os.Getenv("GO_CORE_CODING_RUNTIME_NAME"))
		orchestrator.AttachExternalCodingRuntime(newManagedCodingRuntime(orchestrator, managedCodingRuntimeConfig{
			Name:             name,
			Backend:          backend,
			AllowedProviders: parseEnvList(os.Getenv("GO_CORE_CODING_RUNTIME_ALLOWED_PROVIDERS")),
			PlannerTimeout:   envRuntimeDuration("GO_CORE_CODING_RUNTIME_PLANNER_TIMEOUT", 45*time.Second),
			CoderTimeout:     envRuntimeDuration("GO_CORE_CODING_RUNTIME_CODER_TIMEOUT", 120*time.Second),
			ReviewerTimeout:  envRuntimeDuration("GO_CORE_CODING_RUNTIME_REVIEWER_TIMEOUT", 60*time.Second),
			TesterTimeout:    envRuntimeDuration("GO_CORE_CODING_RUNTIME_TESTER_TIMEOUT", 90*time.Second),
			RetrievalTimeout: envRuntimeDuration("GO_CORE_CODING_RUNTIME_RETRIEVAL_TIMEOUT", 30*time.Second),
		}))
	}
}

func codingRuntimeEnabledFromEnv() bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv("GO_CORE_CODING_RUNTIME_ENABLED")))
	switch value {
	case "", "1", "true", "yes", "on", "enabled":
		return true
	case "0", "false", "no", "off", "disabled":
		return false
	default:
		return true
	}
}

func parseEnvList(value string) []string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	parts := strings.Split(value, ",")
	items := make([]string, 0, len(parts))
	for _, part := range parts {
		trimmed := strings.TrimSpace(part)
		if trimmed != "" {
			items = append(items, trimmed)
		}
	}
	return items
}

func envRuntimeDuration(name string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	duration, err := time.ParseDuration(value)
	if err != nil || duration <= 0 {
		return fallback
	}
	return duration
}
