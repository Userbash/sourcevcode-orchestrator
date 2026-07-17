package api

import (
	"testing"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestBuildProviderResourcePoolsRequireConfirmedModels(t *testing.T) {
	pools := buildProviderResourcePools("openai", []domain.ProviderModelStatus{
		{
			Provider:           "openai",
			ModelName:          "gpt-5.5",
			Available:          true,
			Status:             "ready",
			VerificationStatus: "confirmed",
			Metadata: map[string]any{
				"model_family":   "gpt",
				"resource_pools": []string{"gpt"},
			},
		},
		{
			Provider:           "openai",
			ModelName:          "gpt-5.5-preview",
			Available:          true,
			Status:             "verification_pending",
			VerificationStatus: "verifying",
			Metadata: map[string]any{
				"model_family":   "gpt",
				"resource_pools": []string{"gpt"},
			},
		},
	})
	if len(pools) != 1 {
		t.Fatalf("expected one pool, got %d: %#v", len(pools), pools)
	}
	pool := pools[0]
	if pool["pool"] != "gpt" {
		t.Fatalf("unexpected pool: %#v", pool)
	}
	if pool["eligible"] != true {
		t.Fatalf("expected pool to be eligible: %#v", pool)
	}
	eligibleModels, ok := pool["eligible_models"].([]string)
	if !ok {
		t.Fatalf("eligible_models missing: %#v", pool)
	}
	if len(eligibleModels) != 1 || eligibleModels[0] != "gpt-5.5" {
		t.Fatalf("expected only confirmed model eligible, got %#v", eligibleModels)
	}
	models, ok := pool["models"].([]string)
	if !ok || len(models) != 2 {
		t.Fatalf("expected both models to stay visible in inventory pool, got %#v", pool)
	}
}
