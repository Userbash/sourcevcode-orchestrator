package kernel

import (
	"testing"

	"sourcevcode-orchestrator/go-core/internal/agents"
)

func TestRegisterDefaultAgentsIncludesOptionalProvidersWhenConfigured(t *testing.T) {
	registry := NewRegistry()
	configs := map[string]agents.OpenAICompatibleConfig{
		"local":       {Provider: "local", DefaultModel: "local-model", BaseURL: "http://127.0.0.1:11434/v1"},
		"ai_kernel":   {Provider: "ai_kernel", DefaultModel: "kernel-model", BaseURL: "http://127.0.0.1:8012/v1"},
		"openai":      {Provider: "openai", DefaultModel: "gpt-5.5", BaseURL: "https://api.openai.com/v1", APIKey: "secret", RequireKey: true},
		"mistral":     {Provider: "mistral", DefaultModel: "mistral-large-latest", BaseURL: "https://api.mistral.ai/v1", APIKey: "secret", RequireKey: true},
		"mimo":        {Provider: "mimo", DefaultModel: "mimo-reasoner", BaseURL: "https://mimo.example.test/v1", APIKey: "secret", RequireKey: true},
		"antigravity": {Provider: "antigravity", DefaultModel: "antigravity-coder", BaseURL: "https://antigravity.example.test/v1", APIKey: "secret", RequireKey: true},
	}

	registerDefaultAgents(registry, configs)

	providers := map[string]string{}
	for _, agent := range registry.AgentInfos() {
		providers[agent.ID] = agent.Provider
	}

	if providers["research-mimo"] != "mimo" {
		t.Fatalf("research-mimo provider=%q want %q", providers["research-mimo"], "mimo")
	}
	if providers["coder-antigravity"] != "antigravity" {
		t.Fatalf("coder-antigravity provider=%q want %q", providers["coder-antigravity"], "antigravity")
	}
	if providers["reviewer-antigravity"] != "antigravity" {
		t.Fatalf("reviewer-antigravity provider=%q want %q", providers["reviewer-antigravity"], "antigravity")
	}
}
