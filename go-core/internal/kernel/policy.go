package kernel

import (
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const policyVersion = "go-core-policy-v1"

func preflightPolicy(task domain.Task) domain.PolicyDecision {
	description := strings.TrimSpace(task.Input.Description)
	switch {
	case task.Type == "":
		return domain.PolicyDecision{
			Decision:      "reject",
			Severity:      "error",
			Reasons:       []string{"task type is required"},
			PolicyVersion: policyVersion,
			NextAction:    "fix_request",
		}
	case description == "":
		return domain.PolicyDecision{
			Decision:      "reject",
			Severity:      "error",
			Reasons:       []string{"task description is required"},
			PolicyVersion: policyVersion,
			NextAction:    "fix_request",
		}
	default:
		return domain.PolicyDecision{
			Decision:      "allow",
			Severity:      "info",
			PolicyVersion: policyVersion,
			NextAction:    "route_task",
		}
	}
}

func assignmentPolicy(task domain.Task, acceptance domain.TaskAcceptance, info domain.AgentInfo, state domain.AgentRuntimeState) domain.PolicyDecision {
	reasons := make([]string, 0)
	if state.SuppressedUntil != nil && state.SuppressedUntil.After(acceptance.AcceptedAt) {
		reasons = append(reasons, "agent lane is currently suppressed")
	}
	if state.Status == domain.AgentStatusOffline || state.Status == domain.AgentStatusMaintenance {
		reasons = append(reasons, "agent is not routable in current runtime state")
	}
	if task.AssignedProvider != "" && !strings.EqualFold(task.AssignedProvider, info.Provider) {
		reasons = append(reasons, "assigned provider does not match routed agent")
	}
	if len(reasons) > 0 {
		return domain.PolicyDecision{
			Decision:      "reject",
			Severity:      "error",
			Reasons:       reasons,
			PolicyVersion: policyVersion,
			NextAction:    "reroute_or_recover",
			AgentID:       info.ID,
			Evidence: map[string]any{
				"runtime_state": state,
				"provider":      info.Provider,
			},
		}
	}
	return domain.PolicyDecision{
		Decision:      "allow",
		Severity:      "info",
		PolicyVersion: policyVersion,
		NextAction:    "execute",
		AgentID:       info.ID,
	}
}

func decisionBlocks(decision domain.PolicyDecision) bool {
	return strings.EqualFold(decision.Decision, "reject") || strings.EqualFold(decision.Decision, "block")
}
