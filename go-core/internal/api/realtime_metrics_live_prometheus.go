package api

import (
	"fmt"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
)

func formatLiveRealtimeMetricsPrometheus(snapshot kernel.LiveRealtimeMetricsSnapshot) string {
	lines := []string{
		"# HELP go_core_realtime_live_models_total Total provider/model pairs currently tracked by live realtime monitoring.",
		"# TYPE go_core_realtime_live_models_total gauge",
		fmt.Sprintf("go_core_realtime_live_models_total %d", len(snapshot.Models)),
		"# HELP go_core_realtime_live_tracked_sessions Total sessions currently tracked by live realtime monitoring.",
		"# TYPE go_core_realtime_live_tracked_sessions gauge",
		fmt.Sprintf("go_core_realtime_live_tracked_sessions %d", snapshot.TrackedSessions),
		"# HELP go_core_realtime_live_active_sessions_total Total active realtime sessions across all models.",
		"# TYPE go_core_realtime_live_active_sessions_total gauge",
		fmt.Sprintf("go_core_realtime_live_active_sessions_total %d", snapshot.ActiveSessions),
	}

	appendMetricHeader := func(name string, help string, metricType string) {
		lines = append(lines,
			"# HELP "+name+" "+help,
			"# TYPE "+name+" "+metricType,
		)
	}

	appendMetricHeader("go_core_realtime_live_active_sessions", "Active realtime sessions for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_sessions_started_total", "Realtime sessions started for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_sessions_completed_total", "Realtime sessions completed for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_sessions_failed_total", "Realtime sessions failed for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_tokens_streamed_total", "Tokens streamed for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_tools_started_total", "Tools started for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_tools_finished_total", "Tools finished for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_patches_previewed_total", "Patch previews emitted for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_patches_applied_total", "Patches applied for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_tests_started_total", "Tests started for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_tests_finished_total", "Tests finished for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_failure_rate", "Failure rate for a provider/model from live runtime paths.", "gauge")
	appendMetricHeader("go_core_realtime_live_avg_time_to_first_token_ms", "Average live time to first token in milliseconds for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_avg_total_completion_ms", "Average live total completion time in milliseconds for a provider/model.", "gauge")
	appendMetricHeader("go_core_realtime_live_transport_sessions_total", "Live realtime sessions grouped by transport mode for a provider/model.", "gauge")

	appendHistogramHeader := func(name string, help string) {
		lines = append(lines,
			"# HELP "+name+" "+help,
			"# TYPE "+name+" histogram",
		)
	}

	appendHistogramHeader("go_core_realtime_live_time_to_first_token_ms", "Live histogram of time to first token in milliseconds for a provider/model.")
	appendHistogramHeader("go_core_realtime_live_time_to_first_tool_ms", "Live histogram of time to first tool event in milliseconds for a provider/model.")
	appendHistogramHeader("go_core_realtime_live_time_to_first_patch_ms", "Live histogram of time to first patch event in milliseconds for a provider/model.")
	appendHistogramHeader("go_core_realtime_live_time_to_first_result_ms", "Live histogram of time to first result event in milliseconds for a provider/model.")
	appendHistogramHeader("go_core_realtime_live_time_to_first_test_ms", "Live histogram of time to first test event in milliseconds for a provider/model.")
	appendHistogramHeader("go_core_realtime_live_total_completion_ms", "Live histogram of total completion time in milliseconds for a provider/model.")

	for _, model := range snapshot.Models {
		labels := prometheusLabels(model.Provider, model.ModelName)
		lines = append(lines,
			fmt.Sprintf("go_core_realtime_live_active_sessions%s %d", labels, model.ActiveSessions),
			fmt.Sprintf("go_core_realtime_live_sessions_started_total%s %d", labels, model.SessionsStarted),
			fmt.Sprintf("go_core_realtime_live_sessions_completed_total%s %d", labels, model.SessionsCompleted),
			fmt.Sprintf("go_core_realtime_live_sessions_failed_total%s %d", labels, model.SessionsFailed),
			fmt.Sprintf("go_core_realtime_live_tokens_streamed_total%s %d", labels, model.TokensStreamedTotal),
			fmt.Sprintf("go_core_realtime_live_tools_started_total%s %d", labels, model.ToolsStartedTotal),
			fmt.Sprintf("go_core_realtime_live_tools_finished_total%s %d", labels, model.ToolsFinishedTotal),
			fmt.Sprintf("go_core_realtime_live_patches_previewed_total%s %d", labels, model.PatchesPreviewedTotal),
			fmt.Sprintf("go_core_realtime_live_patches_applied_total%s %d", labels, model.PatchesAppliedTotal),
			fmt.Sprintf("go_core_realtime_live_tests_started_total%s %d", labels, model.TestsStartedTotal),
			fmt.Sprintf("go_core_realtime_live_tests_finished_total%s %d", labels, model.TestsFinishedTotal),
			fmt.Sprintf("go_core_realtime_live_failure_rate%s %g", labels, model.FailureRate),
			fmt.Sprintf("go_core_realtime_live_avg_time_to_first_token_ms%s %d", labels, model.AvgTimeToFirstTokenMS),
			fmt.Sprintf("go_core_realtime_live_avg_total_completion_ms%s %d", labels, model.AvgTotalCompletionMS),
			fmt.Sprintf("go_core_realtime_live_transport_sessions_total%s %d", prometheusLiveTransportLabels(model.Provider, model.ModelName, string(domain.RuntimeTransportNativeStream)), model.NativeStreamSessions),
			fmt.Sprintf("go_core_realtime_live_transport_sessions_total%s %d", prometheusLiveTransportLabels(model.Provider, model.ModelName, string(domain.RuntimeTransportPseudoRealtime)), model.PseudoRealtimeSessions),
			fmt.Sprintf("go_core_realtime_live_transport_sessions_total%s %d", prometheusLiveTransportLabels(model.Provider, model.ModelName, string(domain.RuntimeTransportBuffered)), model.BufferedSessions),
		)
		appendLiveHistogram(&lines, "go_core_realtime_live_time_to_first_token_ms", model.Provider, model.ModelName, model.TimeToFirstTokenMS)
		appendLiveHistogram(&lines, "go_core_realtime_live_time_to_first_tool_ms", model.Provider, model.ModelName, model.TimeToFirstToolMS)
		appendLiveHistogram(&lines, "go_core_realtime_live_time_to_first_patch_ms", model.Provider, model.ModelName, model.TimeToFirstPatchMS)
		appendLiveHistogram(&lines, "go_core_realtime_live_time_to_first_result_ms", model.Provider, model.ModelName, model.TimeToFirstResultMS)
		appendLiveHistogram(&lines, "go_core_realtime_live_time_to_first_test_ms", model.Provider, model.ModelName, model.TimeToFirstTestMS)
		appendLiveHistogram(&lines, "go_core_realtime_live_total_completion_ms", model.Provider, model.ModelName, model.TotalCompletionMS)
	}

	return strings.Join(lines, "\n") + "\n"
}

func appendLiveHistogram(lines *[]string, name string, provider string, model string, snapshot kernel.LiveRealtimeHistogramSnapshot) {
	for _, bucket := range snapshot.Buckets {
		*lines = append(*lines, fmt.Sprintf("%s_bucket%s %d", name, prometheusHistogramLabels(provider, model, bucket.Le), bucket.Count))
	}
	*lines = append(*lines,
		fmt.Sprintf("%s_sum%s %d", name, prometheusLabels(provider, model), snapshot.Sum),
		fmt.Sprintf("%s_count%s %d", name, prometheusLabels(provider, model), snapshot.Count),
	)
}

func prometheusHistogramLabels(provider string, model string, le int64) string {
	return "{" + strings.Join([]string{
		fmt.Sprintf("le=%q", fmt.Sprintf("%d", le)),
		fmt.Sprintf("model=%q", prometheusEscape(model)),
		fmt.Sprintf("provider=%q", prometheusEscape(provider)),
	}, ",") + "}"
}

func prometheusLiveTransportLabels(provider string, model string, transport string) string {
	return "{" + strings.Join([]string{
		fmt.Sprintf("model=%q", prometheusEscape(model)),
		fmt.Sprintf("provider=%q", prometheusEscape(provider)),
		fmt.Sprintf("transport=%q", prometheusEscape(transport)),
	}, ",") + "}"
}
