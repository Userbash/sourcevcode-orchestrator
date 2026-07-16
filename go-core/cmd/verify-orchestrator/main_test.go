package main

import (
	"context"
	"testing"
	"time"
)

func TestRunSyntheticRuntimeProfileCapturesAgentKPIs(t *testing.T) {
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	t.Setenv("GO_CORE_MAX_PARALLELISM", "8")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_AGENT", "4")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_MODEL", "4")

	profile, agentKPIs, coordinator, err := runSyntheticRuntimeProfile(context.Background())
	if err != nil {
		t.Fatalf("runSyntheticRuntimeProfile() error = %v", err)
	}
	if profile == nil {
		t.Fatal("expected runtime profile")
	}
	if coordinator == nil {
		t.Fatal("expected coordinator KPI")
	}
	if len(agentKPIs) != 4 {
		t.Fatalf("expected 4 agent KPIs, got %d", len(agentKPIs))
	}

	totalExecutions := uint64(0)
	nonZeroAgents := 0
	for _, kpi := range agentKPIs {
		t.Logf("agent=%s executions=%d success=%d failure=%d peak=%d latency=%s last_task=%s types=%v files=%v",
			kpi.AgentID,
			kpi.ExecutionCount,
			kpi.SuccessCount,
			kpi.FailureCount,
			kpi.PeakConcurrency,
			kpi.AverageLatency,
			kpi.LastTaskID,
			kpi.ObservedTaskTypes,
			kpi.ObservedFiles,
		)
		totalExecutions += kpi.ExecutionCount
		if kpi.ExecutionCount > 0 {
			nonZeroAgents++
		}
	}

	t.Logf("profile_duration=%s planned=%d completed=%d workflows=%d coordinator_workflows=%d completed_ids=%v",
		profile.Duration,
		profile.PlannedTaskCount,
		profile.CompletedTaskCount,
		profile.WorkflowCount,
		coordinator.WorkflowCount,
		coordinator.CompletedTaskIDs,
	)

	if totalExecutions == 0 {
		t.Fatalf("expected non-zero agent executions, got %d", totalExecutions)
	}
	if nonZeroAgents < 3 {
		t.Fatalf("expected at least 3 active agents, got %d", nonZeroAgents)
	}
	if profile.Duration < 20*time.Millisecond {
		t.Fatalf("expected runtime profile to include real execution latency, got %s", profile.Duration)
	}
}
