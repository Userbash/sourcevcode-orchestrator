package modules

type CodeAutomationModule struct {
	*BasicModule
}

func NewCodeAutomationModule() *CodeAutomationModule {
	return &CodeAutomationModule{
		BasicModule: NewBasicModule("code_automation", "automation", map[string]any{
			"summary": "Native Go port of core/core/code_automation Python stubs",
			"helpers": []string{
				"analyze_task",
				"plan_patch",
				"validate_diff",
				"apply_patch_plan",
				"test_summary",
				"review_patch",
			},
		}),
	}
}
