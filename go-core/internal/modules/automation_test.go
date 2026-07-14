package modules

import (
	"reflect"
	"testing"
)

func TestCodeAutomationModuleMirrorsPythonStubs(t *testing.T) {
	module := NewCodeAutomationModule()

	if got := module.AnalyzeTask("rename field"); !reflect.DeepEqual(got, map[string]any{
		"intent": "code_change",
		"text":   "rename field",
	}) {
		t.Fatalf("AnalyzeTask() = %#v", got)
	}

	if got := module.PlanPatch([]string{"a.go", "b.go"}); !reflect.DeepEqual(got, map[string]any{
		"files": []string{"a.go", "b.go"},
		"risk":  "medium",
	}) {
		t.Fatalf("PlanPatch() = %#v", got)
	}

	if !module.ValidateDiff(" diff ") {
		t.Fatal("ValidateDiff() expected true for non-empty diff")
	}
	if module.ValidateDiff(" \n\t ") {
		t.Fatal("ValidateDiff() expected false for whitespace-only diff")
	}

	plan := map[string]any{"files": []string{"a.go"}}
	if got := module.ApplyPatch(plan); !reflect.DeepEqual(got, map[string]any{
		"applied": true,
		"plan":    plan,
	}) {
		t.Fatalf("ApplyPatch() = %#v", got)
	}

	if got := module.TestSummary(5, 2); !reflect.DeepEqual(got, map[string]any{
		"passed": 5,
		"failed": 2,
	}) {
		t.Fatalf("TestSummary() = %#v", got)
	}

	if got := module.ReviewPatch("patch"); !reflect.DeepEqual(got, map[string]any{
		"ok":    true,
		"notes": []any{},
	}) {
		t.Fatalf("ReviewPatch() = %#v", got)
	}
}
