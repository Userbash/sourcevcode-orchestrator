package api

import (
	"fmt"
	"sort"
	"strings"
)

func formatRealtimeMetricsPrometheus(report realtimeMetricsReport) string {
	lines := []string{
		"# HELP go_core_realtime_workflows_total Total workflow records scanned for realtime metrics.",
		"# TYPE go_core_realtime_workflows_total gauge",
		fmt.Sprintf("go_core_realtime_workflows_total %d", report.WorkflowCount),
		"# HELP go_core_realtime_samples_collected_total Total realtime metric samples collected from workflows.",
		"# TYPE go_core_realtime_samples_collected_total gauge",
		fmt.Sprintf("go_core_realtime_samples_collected_total %d", report.SamplesCollected),
		"# HELP go_core_realtime_provider_models_total Total provider/model pairs with realtime metrics.",
		"# TYPE go_core_realtime_provider_models_total gauge",
		fmt.Sprintf("go_core_realtime_provider_models_total %d", len(report.Accumulators)),
	}

	appendMetricHeader := func(name, help string) {
		lines = append(lines,
			"# HELP "+name+" "+help,
			"# TYPE "+name+" gauge",
		)
	}

	appendMetricHeader("go_core_realtime_samples_total", "Realtime metric samples aggregated for a provider/model.")
	appendMetricHeader("go_core_realtime_streaming_samples_total", "Realtime samples that used streaming transport for a provider/model.")
	appendMetricHeader("go_core_realtime_native_streaming_samples_total", "Realtime samples that used native streaming transport for a provider/model.")
	appendMetricHeader("go_core_realtime_pseudo_realtime_samples_total", "Realtime samples that used pseudo-realtime fallback for a provider/model.")
	appendMetricHeader("go_core_realtime_buffered_samples_total", "Realtime samples that used buffered transport for a provider/model.")
	appendMetricHeader("go_core_realtime_transport_samples_total", "Realtime samples grouped by exact transport mode for a provider/model.")
	appendMetricHeader("go_core_realtime_time_to_first_token_ms", "Average time to first token in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_time_to_first_tool_ms", "Average time to first tool call in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_time_to_first_patch_ms", "Average time to first patch in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_time_to_first_result_ms", "Average time to first result in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_time_to_first_test_ms", "Average time to first test event in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_total_completion_ms", "Average total completion time in milliseconds for a provider/model.")
	appendMetricHeader("go_core_realtime_tokens_streamed_avg", "Average streamed token count for a provider/model.")
	appendMetricHeader("go_core_realtime_tools_executed_avg", "Average tools executed for a provider/model.")
	appendMetricHeader("go_core_realtime_patches_applied_avg", "Average patches applied for a provider/model.")
	appendMetricHeader("go_core_realtime_tests_executed_avg", "Average tests executed for a provider/model.")

	for _, acc := range sortedRealtimeAccumulators(report.Accumulators) {
		labels := prometheusLabels(acc.Provider, acc.ModelName)
		lines = append(lines,
			fmt.Sprintf("go_core_realtime_samples_total%s %d", labels, acc.SampleCount),
			fmt.Sprintf("go_core_realtime_streaming_samples_total%s %d", labels, acc.StreamingSampleCount),
			fmt.Sprintf("go_core_realtime_native_streaming_samples_total%s %d", labels, acc.NativeStreamingSampleCount),
			fmt.Sprintf("go_core_realtime_pseudo_realtime_samples_total%s %d", labels, acc.PseudoRealtimeSampleCount),
			fmt.Sprintf("go_core_realtime_buffered_samples_total%s %d", labels, acc.BufferedSampleCount),
			fmt.Sprintf("go_core_realtime_time_to_first_token_ms%s %d", labels, safeAverage(acc.SumTimeToFirstTokenMS, acc.CountTimeToFirstTokenMS)),
			fmt.Sprintf("go_core_realtime_time_to_first_tool_ms%s %d", labels, safeAverage(acc.SumTimeToFirstToolMS, acc.CountTimeToFirstToolMS)),
			fmt.Sprintf("go_core_realtime_time_to_first_patch_ms%s %d", labels, safeAverage(acc.SumTimeToFirstPatchMS, acc.CountTimeToFirstPatchMS)),
			fmt.Sprintf("go_core_realtime_time_to_first_result_ms%s %d", labels, safeAverage(acc.SumTimeToFirstResultMS, acc.CountTimeToFirstResultMS)),
			fmt.Sprintf("go_core_realtime_time_to_first_test_ms%s %d", labels, safeAverage(acc.SumTimeToFirstTestMS, acc.CountTimeToFirstTestMS)),
			fmt.Sprintf("go_core_realtime_total_completion_ms%s %d", labels, safeAverage(acc.SumTotalCompletionMS, acc.CountTotalCompletionMS)),
			fmt.Sprintf("go_core_realtime_tokens_streamed_avg%s %d", labels, safeAverage(acc.SumTokensStreamed, acc.SampleCount)),
			fmt.Sprintf("go_core_realtime_tools_executed_avg%s %d", labels, safeAverage(acc.SumToolsExecuted, acc.SampleCount)),
			fmt.Sprintf("go_core_realtime_patches_applied_avg%s %d", labels, safeAverage(acc.SumPatchesApplied, acc.SampleCount)),
			fmt.Sprintf("go_core_realtime_tests_executed_avg%s %d", labels, safeAverage(acc.SumTestsExecuted, acc.SampleCount)),
		)
		for transport, count := range acc.TransportCounts {
			lines = append(lines, fmt.Sprintf("go_core_realtime_transport_samples_total%s %d", prometheusTransportLabels(acc.Provider, acc.ModelName, transport), count))
		}
	}

	return strings.Join(lines, "\n") + "\n"
}

func prometheusLabels(provider string, model string) string {
	labels := []string{
		fmt.Sprintf("provider=%q", prometheusEscape(provider)),
		fmt.Sprintf("model=%q", prometheusEscape(model)),
	}
	sort.Strings(labels)
	return "{" + strings.Join(labels, ",") + "}"
}

func prometheusEscape(value string) string {
	value = strings.ReplaceAll(value, `\\`, `\\\\`)
	value = strings.ReplaceAll(value, `"`, `\\"`)
	value = strings.ReplaceAll(value, "\n", `\\n`)
	return value
}

func prometheusTransportLabels(provider string, model string, transport string) string {
	labels := []string{
		fmt.Sprintf("provider=%q", prometheusEscape(provider)),
		fmt.Sprintf("model=%q", prometheusEscape(model)),
		fmt.Sprintf("transport=%q", prometheusEscape(transport)),
	}
	sort.Strings(labels)
	return "{" + strings.Join(labels, ",") + "}"
}
