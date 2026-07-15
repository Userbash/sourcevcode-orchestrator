package kernel

import (
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

var sourcecraftKeywords = []string{"sourcecraft", "src ", " src", "repo", "repository", "pull request", "pr ", " pr", "issue", "release", "branch", "tag", "changelog", "quota", "status"}

var sourcecraftCapabilities = map[string]struct{}{
	"sourcecraft":       {},
	"repo_ops":          {},
	"pr_flow":           {},
	"release_flow":      {},
	"issue_flow":        {},
	"branch_governance": {},
}

var sourcecraftTaskFamilies = []string{
	"repo_ops",
	"pr_flow",
	"release_flow",
	"issue_flow",
	"branch_governance",
}

var sourcecraftSafeActionsByFamily = map[string][]string{
	"repo_ops":          {"repo_summary", "status", "current_branch"},
	"pr_flow":           {"repo_summary", "status", "pr_checks"},
	"release_flow":      {"repo_summary", "status", "repo_governance_report"},
	"issue_flow":        {"repo_summary", "status"},
	"branch_governance": {"current_branch", "status", "repo_governance_report"},
}

var sourcecraftRoutableTaskTypes = map[domain.TaskType]struct{}{
	domain.TaskTypePlan:     {},
	domain.TaskTypeDocs:     {},
	domain.TaskTypeResearch: {},
}

var capabilityByTaskType = map[domain.TaskType]string{
	domain.TaskTypePlan:     "plan",
	domain.TaskTypeCode:     "code",
	domain.TaskTypeReview:   "review",
	domain.TaskTypeTest:     "test",
	domain.TaskTypeDocs:     "docs",
	domain.TaskTypeFix:      "fix",
	domain.TaskTypeResearch: "research",
}

func inferCapability(task domain.Task) string {
	if strings.TrimSpace(task.RequiredCapability) != "" {
		return strings.TrimSpace(task.RequiredCapability)
	}
	if isSourcecraftWork(task) {
		return "sourcecraft"
	}
	if capability, ok := capabilityByTaskType[task.Type]; ok {
		return capability
	}
	return "general"
}

func resolvedCapability(task domain.Task) string {
	return inferCapability(task)
}

func SourcecraftTaskFamilies() []string {
	return append([]string(nil), sourcecraftTaskFamilies...)
}

func SourcecraftSafeActions() []string {
	return []string{
		"repo_summary",
		"status",
		"current_branch",
		"pr_checks",
		"repo_governance_report",
	}
}

func SourcecraftTaskFamily(task domain.Task) string {
	capability := strings.TrimSpace(strings.ToLower(task.RequiredCapability))
	if _, ok := sourcecraftCapabilities[capability]; ok && capability != "" && capability != "sourcecraft" {
		return capability
	}
	text := taskText(task)
	switch {
	case strings.Contains(text, "pull request") || strings.Contains(text, " pr") || strings.Contains(text, "pr "):
		return "pr_flow"
	case strings.Contains(text, "release") || strings.Contains(text, "tag") || strings.Contains(text, "changelog"):
		return "release_flow"
	case strings.Contains(text, "issue"):
		return "issue_flow"
	case strings.Contains(text, "branch"):
		return "branch_governance"
	default:
		return "repo_ops"
	}
}

func SourcecraftRecommendedActions(task domain.Task) []string {
	family := SourcecraftTaskFamily(task)
	actions := sourcecraftSafeActionsByFamily[family]
	if len(actions) == 0 {
		actions = sourcecraftSafeActionsByFamily["repo_ops"]
	}
	return append([]string(nil), actions...)
}

func isSourcecraftWork(task domain.Task) bool {
	capability := strings.TrimSpace(strings.ToLower(task.RequiredCapability))
	if _, ok := sourcecraftCapabilities[capability]; ok {
		return true
	}
	if _, ok := sourcecraftRoutableTaskTypes[task.Type]; !ok {
		return false
	}
	text := taskText(task)
	for _, keyword := range sourcecraftKeywords {
		if strings.Contains(text, keyword) {
			return true
		}
	}
	return false
}

func taskText(task domain.Task) string {
	parts := []string{task.Input.Description}
	parts = append(parts, compactStrings(task.Input.Files)...)
	parts = append(parts, compactStrings(task.Input.Constraints)...)
	parts = append(parts, compactStrings(task.Input.AcceptanceCriteria)...)
	return strings.ToLower(strings.Join(parts, " "))
}

func compactStrings(values []string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			out = append(out, value)
		}
	}
	return out
}

func cloneHints(hints map[string]any) map[string]any {
	if len(hints) == 0 {
		return map[string]any{}
	}
	out := make(map[string]any, len(hints))
	for key, value := range hints {
		out[key] = value
	}
	return out
}

func supportsCapability(capabilities []string, required string) bool {
	for _, capability := range capabilities {
		if capability == required || capability == "*" {
			return true
		}
	}
	return false
}
