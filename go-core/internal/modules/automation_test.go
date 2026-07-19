package modules

import "testing"

func TestCodeAutomationModuleRegistersMetadata(t *testing.T) {
	module := NewCodeAutomationModule()

	if module.Info().Name != "code_automation" {
		t.Fatalf("Name = %q", module.Info().Name)
	}
	if module.Info().Kind != "automation" {
		t.Fatalf("Kind = %q", module.Info().Kind)
	}

	summary, ok := module.Info().Metadata["summary"].(string)
	if !ok {
		t.Fatalf("summary metadata missing or wrong type: %#v", module.Info().Metadata["summary"])
	}
	if summary != "Native Go port of core/core/code_automation Python stubs" {
		t.Fatalf("summary = %q", summary)
	}

	helpers, ok := module.Info().Metadata["helpers"].([]string)
	if !ok {
		t.Fatalf("helpers metadata missing or wrong type: %#v", module.Info().Metadata["helpers"])
	}
	want := []string{
		"analyze_task",
		"plan_patch",
		"validate_diff",
		"apply_patch_plan",
		"test_summary",
		"review_patch",
	}
	if len(helpers) != len(want) {
		t.Fatalf("helpers len = %d, want %d (%#v)", len(helpers), len(want), helpers)
	}
	for i := range want {
		if helpers[i] != want[i] {
			t.Fatalf("helpers[%d] = %q, want %q", i, helpers[i], want[i])
		}
	}
}
