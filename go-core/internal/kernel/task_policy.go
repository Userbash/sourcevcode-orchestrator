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
