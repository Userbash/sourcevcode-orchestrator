package main

import (
	"context"
	"testing"
	"time"
)

func configureSyntheticRuntimeEnv(t *testing.T) {
	t.Helper()
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("GO_CORE_SUBMIT_MODE", "sync")
	t.Setenv("GO_CORE_MAX_PARALLELISM", "8")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_AGENT", "4")
	t.Setenv("GO_CORE_MAX_CONCURRENT_PER_MODEL", "4")
}

func TestRunSyntheticRuntimeProfileCapturesAgentKPIs(t *testing.T) {
	configureSyntheticRuntimeEnv(t)

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
	if len(agentKPIs) != 6 {
		t.Fatalf("expected 6 agent KPIs, got %d", len(agentKPIs))
	}
	if profile.Level != "advanced" {
		t.Fatalf("expected advanced profile wrapper, got %q", profile.Level)
	}
	if profile.Scenario != "level-3-code-fanout" {
		t.Fatalf("expected advanced fanout scenario, got %q", profile.Scenario)
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

	t.Logf("profile_duration=%s planned=%d completed=%d workflows=%d coordinator_workflows=%d completed_ids=%v warnings=%v observed_parallel=%d mean_total=%s",
		profile.Duration,
		profile.PlannedTaskCount,
		profile.CompletedTaskCount,
		profile.WorkflowCount,
		coordinator.WorkflowCount,
		coordinator.CompletedTaskIDs,
		profile.Warnings,
		profile.MaxObservedParallelism,
		profile.MeanTotalLatency,
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
	if profile.ParallelBranchCount < 2 {
		t.Fatalf("expected advanced scenario to expose parallel branches, got %d", profile.ParallelBranchCount)
	}
	if profile.MaxObservedParallelism < 2 {
		t.Fatalf("expected advanced scenario to overlap running workflows, got %d", profile.MaxObservedParallelism)
	}
	if profile.UnexpectedEventCount != 0 {
		t.Fatalf("expected no unexpected task events, got %d", profile.UnexpectedEventCount)
	}
	if len(profile.WorkflowTraces) != profile.PlannedTaskCount {
		t.Fatalf("expected workflow trace per planned task, got traces=%d planned=%d", len(profile.WorkflowTraces), profile.PlannedTaskCount)
	}
}

func TestRunSyntheticRuntimeProfilesCoverProgressiveComplexity(t *testing.T) {
	configureSyntheticRuntimeEnv(t)

	executions, err := runSyntheticRuntimeProfiles(context.Background())
	if err != nil {
		t.Fatalf("runSyntheticRuntimeProfiles() error = %v", err)
	}
	if len(executions) != 3 {
		t.Fatalf("expected 3 scenarios, got %d", len(executions))
	}

	expected := []struct {
		name               string
		level              string
		parallelAtMost     int
		parallelAtLeast    int
		observedAtMost     int
		observedAtLeast    int
		capabilityCounts   map[string]int
		requiredTraceKinds []string
	}{
		{name: "level-1-docs-sequential", level: "basic", parallelAtMost: 1, observedAtMost: 1, capabilityCounts: map[string]int{"plan": 2, "research": 1, "docs": 1, "review": 1}, requiredTraceKinds: []string{"task.accepted", "task.running", "task.completed"}},
		{name: "level-2-research-review", level: "intermediate", parallelAtMost: 1, observedAtMost: 1, capabilityCounts: map[string]int{"plan": 1, "research": 2, "review": 1}, requiredTraceKinds: []string{"task.accepted", "task.running", "task.completed"}},
		{name: "level-3-code-fanout", level: "advanced", parallelAtLeast: 2, observedAtLeast: 2, capabilityCounts: map[string]int{"plan": 1, "code": 3, "review": 1, "test": 1}, requiredTraceKinds: []string{"task.accepted", "task.running", "task.completed"}},
	}

	for i, execution := range executions {
		profile := execution.Profile
		if profile == nil {
			t.Fatalf("scenario %d returned nil profile", i)
		}
		if profile.Scenario != expected[i].name {
			t.Fatalf("scenario %d name mismatch: got %q want %q", i, profile.Scenario, expected[i].name)
		}
		if profile.Level != expected[i].level {
			t.Fatalf("scenario %d level mismatch: got %q want %q", i, profile.Level, expected[i].level)
		}
		if expected[i].parallelAtMost > 0 && profile.ParallelBranchCount > expected[i].parallelAtMost {
			t.Fatalf("scenario %s expected sequential or near-sequential behavior, got parallel=%d", profile.Scenario, profile.ParallelBranchCount)
		}
		if expected[i].parallelAtLeast > 0 && profile.ParallelBranchCount < expected[i].parallelAtLeast {
			t.Fatalf("scenario %s expected parallel fanout, got parallel=%d", profile.Scenario, profile.ParallelBranchCount)
		}
		if expected[i].observedAtMost > 0 && profile.MaxObservedParallelism > expected[i].observedAtMost {
			t.Fatalf("scenario %s expected sequential execution, got observed_parallel=%d", profile.Scenario, profile.MaxObservedParallelism)
		}
		if expected[i].observedAtLeast > 0 && profile.MaxObservedParallelism < expected[i].observedAtLeast {
			t.Fatalf("scenario %s expected overlapping execution, got observed_parallel=%d", profile.Scenario, profile.MaxObservedParallelism)
		}
		if len(profile.FocusAreas) == 0 {
			t.Fatalf("scenario %s expected focus areas", profile.Scenario)
		}
		assertProfileSmokeCoverage(t, profile, expected[i].capabilityCounts, expected[i].requiredTraceKinds)
	}
}

func TestRunSyntheticRuntimeProfilesCollectTraceDistributionAndStableValues(t *testing.T) {
	configureSyntheticRuntimeEnv(t)

	executions, err := runSyntheticRuntimeProfiles(context.Background())
	if err != nil {
		t.Fatalf("runSyntheticRuntimeProfiles() error = %v", err)
	}

	for _, execution := range executions {
		profile := execution.Profile
		if profile == nil {
			t.Fatal("expected runtime profile")
		}
		if profile.TaskEventCount < len(profile.WorkflowTraces)*3 {
			t.Fatalf("scenario %s expected dense task event trace, got events=%d traces=%d", profile.Scenario, profile.TaskEventCount, len(profile.WorkflowTraces))
		}
		if profile.UnexpectedEventCount != 0 {
			t.Fatalf("scenario %s captured unexpected events: %d", profile.Scenario, profile.UnexpectedEventCount)
		}
		if profile.NoisyWorkflowCount != 0 {
			t.Fatalf("scenario %s exceeded workflow noise threshold: %d", profile.Scenario, profile.NoisyWorkflowCount)
		}
		if len(profile.Warnings) != 0 {
			t.Fatalf("scenario %s reported warnings: %v", profile.Scenario, profile.Warnings)
		}
		if profile.ResultArtifactCount != profile.CompletedTaskCount {
			t.Fatalf("scenario %s expected result artifacts to match completed tasks, got artifacts=%d completed=%d", profile.Scenario, profile.ResultArtifactCount, profile.CompletedTaskCount)
		}
		if profile.MeanExecutionLatency <= 0 {
			t.Fatalf("scenario %s expected positive mean execution latency, got %s", profile.Scenario, profile.MeanExecutionLatency)
		}
		if profile.MeanTotalLatency <= 0 {
			t.Fatalf("scenario %s expected positive mean total latency, got %s", profile.Scenario, profile.MeanTotalLatency)
		}
		if len(profile.Distribution.ByCapability) == 0 || len(profile.Distribution.ByAgent) == 0 || len(profile.Distribution.ByProvider) == 0 || len(profile.Distribution.ByModel) == 0 {
			t.Fatalf("scenario %s expected non-empty distribution summary, got %+v", profile.Scenario, profile.Distribution)
		}
	}
}

func assertProfileSmokeCoverage(t *testing.T, profile *RuntimeProfile, capabilityCounts map[string]int, requiredTraceKinds []string) {
	t.Helper()
	if profile.PlannedTaskCount == 0 {
		t.Fatalf("scenario %s expected planned tasks", profile.Scenario)
	}
	if profile.CompletedTaskCount != profile.PlannedTaskCount {
		t.Fatalf("scenario %s expected completed tasks to match planned tasks, got planned=%d completed=%d", profile.Scenario, profile.PlannedTaskCount, profile.CompletedTaskCount)
	}
	if len(profile.PlanTaskIDs) != profile.PlannedTaskCount {
		t.Fatalf("scenario %s expected plan task ids for each planned task, got ids=%d planned=%d", profile.Scenario, len(profile.PlanTaskIDs), profile.PlannedTaskCount)
	}
	if len(profile.CompletedTaskIDs) != profile.CompletedTaskCount {
		t.Fatalf("scenario %s expected completed ids for each completed task, got ids=%d completed=%d", profile.Scenario, len(profile.CompletedTaskIDs), profile.CompletedTaskCount)
	}
	if len(profile.WorkflowTraces) != profile.PlannedTaskCount {
		t.Fatalf("scenario %s expected workflow traces to align with planned tasks, got traces=%d planned=%d", profile.Scenario, len(profile.WorkflowTraces), profile.PlannedTaskCount)
	}

	seenKinds := map[string]bool{}
	traceCountByCapability := map[string]int{}
	for _, trace := range profile.WorkflowTraces {
		if trace.TaskID == "" {
			t.Fatalf("scenario %s contains workflow trace with empty task id", profile.Scenario)
		}
		if trace.Capability == "" {
			t.Fatalf("scenario %s trace %s missing capability", profile.Scenario, trace.TaskID)
		}
		if trace.AgentID == "" {
			t.Fatalf("scenario %s trace %s missing agent id", profile.Scenario, trace.TaskID)
		}
		if trace.ResultStatus == "" {
			t.Fatalf("scenario %s trace %s missing result status", profile.Scenario, trace.TaskID)
		}
		if len(trace.EventKinds) == 0 {
			t.Fatalf("scenario %s trace %s missing event kinds", profile.Scenario, trace.TaskID)
		}
		if trace.ExecutionLatency <= 0 {
			t.Fatalf("scenario %s trace %s missing execution latency", profile.Scenario, trace.TaskID)
		}
		if trace.TotalLatency <= 0 {
			t.Fatalf("scenario %s trace %s missing total latency", profile.Scenario, trace.TaskID)
		}
		traceCountByCapability[trace.Capability]++
		for _, kind := range trace.EventKinds {
			seenKinds[kind] = true
		}
	}

	for capability, expectedCount := range capabilityCounts {
		if traceCountByCapability[capability] != expectedCount {
			t.Fatalf("scenario %s capability %s count mismatch: got %d want %d", profile.Scenario, capability, traceCountByCapability[capability], expectedCount)
		}
	}
	for _, kind := range requiredTraceKinds {
		if !seenKinds[kind] {
			t.Fatalf("scenario %s expected to observe event kind %s", profile.Scenario, kind)
		}
	}
}
