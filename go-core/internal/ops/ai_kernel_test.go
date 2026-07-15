package ops

import (
	"slices"
	"testing"
)

func TestAIKernelServerEnvUsesDedicatedAPIKey(t *testing.T) {
	baseEnv := []string{
		"PATH=/usr/bin",
		"AI_KERNEL_API_KEY=local",
		"API_KEY=wrong",
	}

	got := aiKernelServerEnv(baseEnv)

	if !slices.Contains(got, "API_KEY=local") {
		t.Fatalf("expected normalized API_KEY entry, got %v", got)
	}

	if slices.Contains(got, "API_KEY=wrong") {
		t.Fatalf("unexpected inherited API_KEY in %v", got)
	}
}

func TestAIKernelServerEnvDropsAmbientAPIKeyWithoutDedicatedValue(t *testing.T) {
	baseEnv := []string{
		"PATH=/usr/bin",
		"API_KEY=wrong",
	}

	got := aiKernelServerEnv(baseEnv)

	if slices.Contains(got, "API_KEY=wrong") {
		t.Fatalf("unexpected inherited API_KEY in %v", got)
	}
}
