package api

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type realtimeMetricsAccumulator struct {
	Provider                   string
	ModelName                  string
	SampleCount                int
	StreamingSampleCount       int
	NativeStreamingSampleCount int
	PseudoRealtimeSampleCount  int
	BufferedSampleCount        int
	TransportCounts            map[string]int
	SumTimeToFirstTokenMS      int64
	CountTimeToFirstTokenMS    int
	SumTimeToFirstToolMS       int64
	CountTimeToFirstToolMS     int
	SumTimeToFirstPatchMS      int64
	CountTimeToFirstPatchMS    int
	SumTimeToFirstResultMS     int64
	CountTimeToFirstResultMS   int
	SumTimeToFirstTestMS       int64
	CountTimeToFirstTestMS     int
	SumTotalCompletionMS       int64
	CountTotalCompletionMS     int
	SumTokensStreamed          int64
	SumToolsExecuted           int64
	SumPatchesApplied          int64
	SumTestsExecuted           int64
	MinTotalCompletionMS       int64
	MaxTotalCompletionMS       int64
	LastObservedAt             time.Time
}

type realtimeRankingEntry struct {
	Provider              string `json:"provider"`
	ModelName             string `json:"model_name"`
	SampleCount           int    `json:"sample_count"`
	AvgTimeToFirstTokenMS int64  `json:"avg_time_to_first_token_ms,omitempty"`
	AvgTotalCompletionMS  int64  `json:"avg_total_completion_ms,omitempty"`
}

type realtimeMetricsReport struct {
	GeneratedAt      time.Time
	WorkflowCount    int
	SamplesCollected int
	Accumulators     map[string]*realtimeMetricsAccumulator
}

func aggregateRealtimeMetrics(workflows []domain.WorkflowRecord) map[string]any {
	report := buildRealtimeMetricsReport(workflows)
	providerModels := map[string]map[string]any{}
	firstTokenRankings := make([]realtimeRankingEntry, 0, len(report.Accumulators))
	completionRankings := make([]realtimeRankingEntry, 0, len(report.Accumulators))

	for _, acc := range sortedRealtimeAccumulators(report.Accumulators) {
		providerBucket := providerModels[acc.Provider]
		if providerBucket == nil {
			providerBucket = map[string]any{"models": map[string]any{}}
			providerModels[acc.Provider] = providerBucket
		}
		modelsBucket := providerBucket["models"].(map[string]any)
		modelsBucket[acc.ModelName] = realtimeAccumulatorSummary(acc)

		firstToken := safeAverage(acc.SumTimeToFirstTokenMS, acc.CountTimeToFirstTokenMS)
		if firstToken > 0 {
			firstTokenRankings = append(firstTokenRankings, realtimeRankingEntry{
				Provider:              acc.Provider,
				ModelName:             acc.ModelName,
				SampleCount:           acc.SampleCount,
				AvgTimeToFirstTokenMS: firstToken,
			})
		}
		completion := safeAverage(acc.SumTotalCompletionMS, acc.CountTotalCompletionMS)
		if completion > 0 {
			completionRankings = append(completionRankings, realtimeRankingEntry{
				Provider:             acc.Provider,
				ModelName:            acc.ModelName,
				SampleCount:          acc.SampleCount,
				AvgTotalCompletionMS: completion,
			})
		}
	}

	sort.Slice(firstTokenRankings, func(i, j int) bool {
		if firstTokenRankings[i].AvgTimeToFirstTokenMS == firstTokenRankings[j].AvgTimeToFirstTokenMS {
			return firstTokenRankings[i].ModelName < firstTokenRankings[j].ModelName
		}
		return firstTokenRankings[i].AvgTimeToFirstTokenMS < firstTokenRankings[j].AvgTimeToFirstTokenMS
	})
	sort.Slice(completionRankings, func(i, j int) bool {
		if completionRankings[i].AvgTotalCompletionMS == completionRankings[j].AvgTotalCompletionMS {
			return completionRankings[i].ModelName < completionRankings[j].ModelName
		}
		return completionRankings[i].AvgTotalCompletionMS < completionRankings[j].AvgTotalCompletionMS
	})

	return map[string]any{
		"generated_at": report.GeneratedAt.UTC().Format(time.RFC3339),
		"totals": map[string]any{
			"workflow_count":       report.WorkflowCount,
			"provider_model_count": len(report.Accumulators),
			"samples_collected":    report.SamplesCollected,
		},
		"providers": providerModels,
		"rankings": map[string]any{
			"fastest_first_token": firstTokenRankings,
			"fastest_completion":  completionRankings,
		},
	}
}

func normalizeRealtimeTransport(raw string, native bool, pseudo bool) string {
	transport := strings.TrimSpace(strings.ToLower(raw))
	switch transport {
	case string(domain.RuntimeTransportNativeStream):
		return string(domain.RuntimeTransportNativeStream)
	case string(domain.RuntimeTransportPseudoRealtime):
		return string(domain.RuntimeTransportPseudoRealtime)
	case string(domain.RuntimeTransportBuffered):
		return string(domain.RuntimeTransportBuffered)
	}
	if native {
		return string(domain.RuntimeTransportNativeStream)
	}
	if pseudo {
		return string(domain.RuntimeTransportPseudoRealtime)
	}
	return string(domain.RuntimeTransportBuffered)
}

func buildRealtimeMetricsReport(workflows []domain.WorkflowRecord) realtimeMetricsReport {
	report := realtimeMetricsReport{
		GeneratedAt:   time.Now().UTC(),
		WorkflowCount: len(workflows),
		Accumulators:  map[string]*realtimeMetricsAccumulator{},
	}
	for _, workflow := range workflows {
		provider, modelName, metrics, observedAt, ok := extractRealtimeMetrics(workflow)
		if !ok {
			continue
		}
		report.SamplesCollected++
		key := provider + "::" + modelName
		acc := report.Accumulators[key]
		if acc == nil {
			acc = &realtimeMetricsAccumulator{Provider: provider, ModelName: modelName, TransportCounts: map[string]int{}}
			report.Accumulators[key] = acc
		}
		acc.SampleCount++
		if observedAt.After(acc.LastObservedAt) {
			acc.LastObservedAt = observedAt
		}
		transport := normalizeRealtimeTransport(metricString(metrics, "transport"), metricBool(metrics, "native_streaming"), metricBool(metrics, "pseudo_realtime"))
		acc.TransportCounts[transport]++
		if transport != string(domain.RuntimeTransportBuffered) {
			acc.StreamingSampleCount++
		}
		switch transport {
		case string(domain.RuntimeTransportNativeStream):
			acc.NativeStreamingSampleCount++
		case string(domain.RuntimeTransportPseudoRealtime):
			acc.PseudoRealtimeSampleCount++
		default:
			acc.BufferedSampleCount++
		}
		addTimingMetric(&acc.SumTimeToFirstTokenMS, &acc.CountTimeToFirstTokenMS, metricInt64(metrics, "time_to_first_token_ms"))
		addTimingMetric(&acc.SumTimeToFirstToolMS, &acc.CountTimeToFirstToolMS, metricInt64(metrics, "time_to_first_tool_ms"))
		addTimingMetric(&acc.SumTimeToFirstPatchMS, &acc.CountTimeToFirstPatchMS, metricInt64(metrics, "time_to_first_patch_ms"))
		addTimingMetric(&acc.SumTimeToFirstResultMS, &acc.CountTimeToFirstResultMS, metricInt64(metrics, "time_to_first_result_ms"))
		addTimingMetric(&acc.SumTimeToFirstTestMS, &acc.CountTimeToFirstTestMS, metricInt64(metrics, "time_to_first_test_ms"))
		completion := metricInt64(metrics, "total_completion_ms")
		addTimingMetric(&acc.SumTotalCompletionMS, &acc.CountTotalCompletionMS, completion)
		if completion > 0 {
			if acc.MinTotalCompletionMS == 0 || completion < acc.MinTotalCompletionMS {
				acc.MinTotalCompletionMS = completion
			}
			if completion > acc.MaxTotalCompletionMS {
				acc.MaxTotalCompletionMS = completion
			}
		}
		acc.SumTokensStreamed += metricInt64(metrics, "tokens_streamed")
		acc.SumToolsExecuted += metricInt64(metrics, "tools_executed")
		acc.SumPatchesApplied += metricInt64(metrics, "patches_applied")
		acc.SumTestsExecuted += metricInt64(metrics, "tests_executed")
	}
	return report
}

func sortedRealtimeAccumulators(accumulators map[string]*realtimeMetricsAccumulator) []*realtimeMetricsAccumulator {
	keys := make([]string, 0, len(accumulators))
	for key := range accumulators {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]*realtimeMetricsAccumulator, 0, len(keys))
	for _, key := range keys {
		result = append(result, accumulators[key])
	}
	return result
}

func realtimeAccumulatorSummary(acc *realtimeMetricsAccumulator) map[string]any {
	return map[string]any{
		"provider":                      acc.Provider,
		"model_name":                    acc.ModelName,
		"sample_count":                  acc.SampleCount,
		"streaming_sample_count":        acc.StreamingSampleCount,
		"native_streaming_sample_count": acc.NativeStreamingSampleCount,
		"pseudo_realtime_sample_count":  acc.PseudoRealtimeSampleCount,
		"buffered_sample_count":         acc.BufferedSampleCount,
		"transport_counts":              acc.TransportCounts,
		"avg_time_to_first_token_ms":    safeAverage(acc.SumTimeToFirstTokenMS, acc.CountTimeToFirstTokenMS),
		"avg_time_to_first_tool_ms":     safeAverage(acc.SumTimeToFirstToolMS, acc.CountTimeToFirstToolMS),
		"avg_time_to_first_patch_ms":    safeAverage(acc.SumTimeToFirstPatchMS, acc.CountTimeToFirstPatchMS),
		"avg_time_to_first_result_ms":   safeAverage(acc.SumTimeToFirstResultMS, acc.CountTimeToFirstResultMS),
		"avg_time_to_first_test_ms":     safeAverage(acc.SumTimeToFirstTestMS, acc.CountTimeToFirstTestMS),
		"avg_total_completion_ms":       safeAverage(acc.SumTotalCompletionMS, acc.CountTotalCompletionMS),
		"avg_tokens_streamed":           safeAverage(acc.SumTokensStreamed, acc.SampleCount),
		"avg_tools_executed":            safeAverage(acc.SumToolsExecuted, acc.SampleCount),
		"avg_patches_applied":           safeAverage(acc.SumPatchesApplied, acc.SampleCount),
		"avg_tests_executed":            safeAverage(acc.SumTestsExecuted, acc.SampleCount),
		"min_total_completion_ms":       acc.MinTotalCompletionMS,
		"max_total_completion_ms":       acc.MaxTotalCompletionMS,
		"last_observed_at":              acc.LastObservedAt.UTC().Format(time.RFC3339),
	}
}

func extractRealtimeMetrics(workflow domain.WorkflowRecord) (string, string, map[string]any, time.Time, bool) {
	if workflow.Result == nil {
		return "", "", nil, time.Time{}, false
	}
	metrics, ok := workflow.Result.Output.Artifacts["realtime_metrics"]
	if !ok {
		return "", "", nil, time.Time{}, false
	}
	metricMap, ok := metrics.(map[string]any)
	if !ok {
		return "", "", nil, time.Time{}, false
	}
	provider := strings.TrimSpace(workflow.Result.Provider)
	modelName := strings.TrimSpace(workflow.Result.ModelName)
	if provider == "" && workflow.Acceptance.Provider != "" {
		provider = workflow.Acceptance.Provider
	}
	if modelName == "" && workflow.Acceptance.ModelName != "" {
		modelName = workflow.Acceptance.ModelName
	}
	if provider == "" {
		provider = "unknown"
	}
	if modelName == "" {
		modelName = "unknown"
	}
	observedAt := workflow.UpdatedAt
	if observedAt.IsZero() {
		observedAt = time.Now().UTC()
	}
	return provider, modelName, metricMap, observedAt, true
}

func addTimingMetric(sum *int64, count *int, value int64) {
	if value <= 0 {
		return
	}
	*sum += value
	*count = *count + 1
}

func safeAverage(sum int64, count int) int64 {
	if count <= 0 {
		return 0
	}
	return sum / int64(count)
}

func metricInt64(metrics map[string]any, key string) int64 {
	raw, ok := metrics[key]
	if !ok {
		return 0
	}
	switch v := raw.(type) {
	case int:
		return int64(v)
	case int32:
		return int64(v)
	case int64:
		return v
	case float32:
		return int64(v)
	case float64:
		return int64(v)
	case string:
		var parsed int64
		_, err := fmt.Sscan(v, &parsed)
		if err == nil {
			return parsed
		}
	}
	return 0
}

func metricBool(metrics map[string]any, key string) bool {
	raw, ok := metrics[key]
	if !ok {
		return false
	}
	switch v := raw.(type) {
	case bool:
		return v
	case string:
		return strings.EqualFold(v, "true")
	}
	return false
}

func metricString(metrics map[string]any, key string) string {
	raw, ok := metrics[key]
	if !ok {
		return ""
	}
	if value, ok := raw.(string); ok {
		return strings.TrimSpace(value)
	}
	return ""
}
