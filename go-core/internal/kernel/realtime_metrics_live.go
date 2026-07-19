package kernel

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

var liveRealtimeHistogramBoundsMS = []int64{10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000}

type RealtimeHistogramBucket struct {
	Le    int64 `json:"le"`
	Count int64 `json:"count"`
}

type LiveRealtimeHistogramSnapshot struct {
	Count   int64                     `json:"count"`
	Sum     int64                     `json:"sum"`
	Buckets []RealtimeHistogramBucket `json:"buckets"`
}

type LiveRealtimeModelMetricsSnapshot struct {
	Provider               string                        `json:"provider"`
	ModelName              string                        `json:"model_name"`
	ActiveSessions         int64                         `json:"active_sessions"`
	SessionsStarted        int64                         `json:"sessions_started"`
	SessionsCompleted      int64                         `json:"sessions_completed"`
	SessionsFailed         int64                         `json:"sessions_failed"`
	NativeStreamSessions   int64                         `json:"native_stream_sessions"`
	PseudoRealtimeSessions int64                         `json:"pseudo_realtime_sessions"`
	BufferedSessions       int64                         `json:"buffered_sessions"`
	FailureRate            float64                       `json:"failure_rate"`
	AvgTimeToFirstTokenMS  int64                         `json:"avg_time_to_first_token_ms"`
	AvgTotalCompletionMS   int64                         `json:"avg_total_completion_ms"`
	TokensStreamedTotal    int64                         `json:"tokens_streamed_total"`
	ToolsStartedTotal      int64                         `json:"tools_started_total"`
	ToolsFinishedTotal     int64                         `json:"tools_finished_total"`
	PatchesPreviewedTotal  int64                         `json:"patches_previewed_total"`
	PatchesAppliedTotal    int64                         `json:"patches_applied_total"`
	TestsStartedTotal      int64                         `json:"tests_started_total"`
	TestsFinishedTotal     int64                         `json:"tests_finished_total"`
	TimeToFirstTokenMS     LiveRealtimeHistogramSnapshot `json:"time_to_first_token_ms"`
	TimeToFirstToolMS      LiveRealtimeHistogramSnapshot `json:"time_to_first_tool_ms"`
	TimeToFirstPatchMS     LiveRealtimeHistogramSnapshot `json:"time_to_first_patch_ms"`
	TimeToFirstResultMS    LiveRealtimeHistogramSnapshot `json:"time_to_first_result_ms"`
	TimeToFirstTestMS      LiveRealtimeHistogramSnapshot `json:"time_to_first_test_ms"`
	TotalCompletionMS      LiveRealtimeHistogramSnapshot `json:"total_completion_ms"`
}

type LiveRealtimeMetricsSnapshot struct {
	ActiveSessions  int64                              `json:"active_sessions"`
	TrackedSessions int64                              `json:"tracked_sessions"`
	Models          []LiveRealtimeModelMetricsSnapshot `json:"models"`
}

type LiveRealtimeModelSummary struct {
	Provider               string
	ModelName              string
	FailureRate            float64
	NativeStreamSessions   int64
	PseudoRealtimeSessions int64
	BufferedSessions       int64
	AvgTimeToFirstTokenMS  int64
	AvgTotalCompletionMS   int64
}

type liveRealtimeHistogram struct {
	Bounds []int64
	Counts []int64
	Sum    int64
	Count  int64
}

func newLiveRealtimeHistogram() *liveRealtimeHistogram {
	return &liveRealtimeHistogram{
		Bounds: append([]int64(nil), liveRealtimeHistogramBoundsMS...),
		Counts: make([]int64, len(liveRealtimeHistogramBoundsMS)),
	}
}

func (h *liveRealtimeHistogram) Observe(value int64) {
	if h == nil || value < 0 {
		return
	}
	h.Sum += value
	h.Count++
	for i, bound := range h.Bounds {
		if value <= bound {
			h.Counts[i]++
		}
	}
}

func liveHistogramAverage(snapshot LiveRealtimeHistogramSnapshot) int64 {
	if snapshot.Count <= 0 {
		return 0
	}
	return snapshot.Sum / snapshot.Count
}

func liveMetadataString(metadata map[string]any, key string) string {
	if len(metadata) == 0 {
		return ""
	}
	value, ok := metadata[key]
	if !ok || value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	case []byte:
		return string(typed)
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func liveMetadataBool(metadata map[string]any, key string) bool {
	if len(metadata) == 0 {
		return false
	}
	value, ok := metadata[key]
	if !ok || value == nil {
		return false
	}
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "1", "true", "yes", "on":
			return true
		}
	}
	return false
}

func liveFailureRate(started, failed int64) float64 {
	if started <= 0 || failed <= 0 {
		return 0
	}
	rate := float64(failed) / float64(started)
	if rate < 0 {
		return 0
	}
	if rate > 1 {
		return 1
	}
	return rate
}

func liveTransportMode(delta domain.AgentDelta) domain.RuntimeTransportMode {
	if delta.Metadata != nil {
		if raw := strings.TrimSpace(strings.ToLower(liveMetadataString(delta.Metadata, "transport"))); raw != "" {
			switch raw {
			case string(domain.RuntimeTransportNativeStream):
				return domain.RuntimeTransportNativeStream
			case string(domain.RuntimeTransportPseudoRealtime):
				return domain.RuntimeTransportPseudoRealtime
			default:
				return domain.RuntimeTransportBuffered
			}
		}
		if liveMetadataBool(delta.Metadata, "native_streaming") {
			return domain.RuntimeTransportNativeStream
		}
		if liveMetadataBool(delta.Metadata, "pseudo_realtime") {
			return domain.RuntimeTransportPseudoRealtime
		}
	}
	return domain.RuntimeTransportBuffered
}

func (h *liveRealtimeHistogram) Snapshot() LiveRealtimeHistogramSnapshot {
	if h == nil {
		return LiveRealtimeHistogramSnapshot{}
	}
	buckets := make([]RealtimeHistogramBucket, 0, len(h.Bounds))
	for i, bound := range h.Bounds {
		buckets = append(buckets, RealtimeHistogramBucket{Le: bound, Count: h.Counts[i]})
	}
	return LiveRealtimeHistogramSnapshot{
		Count:   h.Count,
		Sum:     h.Sum,
		Buckets: buckets,
	}
}

type liveRealtimeModelMetrics struct {
	Provider               string
	ModelName              string
	ActiveSessions         int64
	SessionsStarted        int64
	SessionsCompleted      int64
	SessionsFailed         int64
	NativeStreamSessions   int64
	PseudoRealtimeSessions int64
	BufferedSessions       int64
	TokensStreamedTotal    int64
	ToolsStartedTotal      int64
	ToolsFinishedTotal     int64
	PatchesPreviewedTotal  int64
	PatchesAppliedTotal    int64
	TestsStartedTotal      int64
	TestsFinishedTotal     int64
	TimeToFirstTokenMS     *liveRealtimeHistogram
	TimeToFirstToolMS      *liveRealtimeHistogram
	TimeToFirstPatchMS     *liveRealtimeHistogram
	TimeToFirstResultMS    *liveRealtimeHistogram
	TimeToFirstTestMS      *liveRealtimeHistogram
	TotalCompletionMS      *liveRealtimeHistogram
}

func newLiveRealtimeModelMetrics(provider string, modelName string) *liveRealtimeModelMetrics {
	return &liveRealtimeModelMetrics{
		Provider:            provider,
		ModelName:           modelName,
		TimeToFirstTokenMS:  newLiveRealtimeHistogram(),
		TimeToFirstToolMS:   newLiveRealtimeHistogram(),
		TimeToFirstPatchMS:  newLiveRealtimeHistogram(),
		TimeToFirstResultMS: newLiveRealtimeHistogram(),
		TimeToFirstTestMS:   newLiveRealtimeHistogram(),
		TotalCompletionMS:   newLiveRealtimeHistogram(),
	}
}

func (m *liveRealtimeModelMetrics) Snapshot() LiveRealtimeModelMetricsSnapshot {
	if m == nil {
		return LiveRealtimeModelMetricsSnapshot{}
	}
	ttft := m.TimeToFirstTokenMS.Snapshot()
	totalCompletion := m.TotalCompletionMS.Snapshot()
	return LiveRealtimeModelMetricsSnapshot{
		Provider:               m.Provider,
		ModelName:              m.ModelName,
		ActiveSessions:         m.ActiveSessions,
		SessionsStarted:        m.SessionsStarted,
		SessionsCompleted:      m.SessionsCompleted,
		SessionsFailed:         m.SessionsFailed,
		NativeStreamSessions:   m.NativeStreamSessions,
		PseudoRealtimeSessions: m.PseudoRealtimeSessions,
		BufferedSessions:       m.BufferedSessions,
		FailureRate:            liveFailureRate(m.SessionsStarted, m.SessionsFailed),
		AvgTimeToFirstTokenMS:  liveHistogramAverage(ttft),
		AvgTotalCompletionMS:   liveHistogramAverage(totalCompletion),
		TokensStreamedTotal:    m.TokensStreamedTotal,
		ToolsStartedTotal:      m.ToolsStartedTotal,
		ToolsFinishedTotal:     m.ToolsFinishedTotal,
		PatchesPreviewedTotal:  m.PatchesPreviewedTotal,
		PatchesAppliedTotal:    m.PatchesAppliedTotal,
		TestsStartedTotal:      m.TestsStartedTotal,
		TestsFinishedTotal:     m.TestsFinishedTotal,
		TimeToFirstTokenMS:     ttft,
		TimeToFirstToolMS:      m.TimeToFirstToolMS.Snapshot(),
		TimeToFirstPatchMS:     m.TimeToFirstPatchMS.Snapshot(),
		TimeToFirstResultMS:    m.TimeToFirstResultMS.Snapshot(),
		TimeToFirstTestMS:      m.TimeToFirstTestMS.Snapshot(),
		TotalCompletionMS:      totalCompletion,
	}
}

type liveRealtimeSession struct {
	SessionID        string
	Provider         string
	ModelName        string
	StartedAt        time.Time
	Active           bool
	Completed        bool
	HasToken         bool
	HasTool          bool
	HasPatch         bool
	HasResult        bool
	HasTest          bool
	TransportMode    domain.RuntimeTransportMode
	TransportCounted bool
}

type LiveRealtimeMetricsCollector struct {
	mu       sync.RWMutex
	sessions map[string]*liveRealtimeSession
	models   map[string]*liveRealtimeModelMetrics
}

func NewLiveRealtimeMetricsCollector() *LiveRealtimeMetricsCollector {
	return &LiveRealtimeMetricsCollector{
		sessions: map[string]*liveRealtimeSession{},
		models:   map[string]*liveRealtimeModelMetrics{},
	}
}

func liveRealtimeModelKey(provider string, modelName string) string {
	return strings.ToLower(strings.TrimSpace(provider)) + "::" + strings.TrimSpace(modelName)
}

func (c *LiveRealtimeMetricsCollector) ensureModel(provider string, modelName string) *liveRealtimeModelMetrics {
	key := liveRealtimeModelKey(provider, modelName)
	metrics, ok := c.models[key]
	if !ok {
		metrics = newLiveRealtimeModelMetrics(provider, modelName)
		c.models[key] = metrics
	}
	return metrics
}

func (c *LiveRealtimeMetricsCollector) ObserveSession(sessionID string, provider string, modelName string, startedAt time.Time) {
	if c == nil || strings.TrimSpace(sessionID) == "" || strings.TrimSpace(provider) == "" || strings.TrimSpace(modelName) == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	session, ok := c.sessions[sessionID]
	newSession := !ok
	if !ok {
		session = &liveRealtimeSession{
			SessionID: sessionID,
			StartedAt: startedAt.UTC(),
			Active:    true,
		}
		c.sessions[sessionID] = session
	}
	if session.StartedAt.IsZero() {
		session.StartedAt = startedAt.UTC()
	}
	if !session.Active && !session.Completed {
		session.Active = true
	}
	providerChanged := session.Provider != "" && session.Provider != provider
	modelChanged := session.ModelName != "" && session.ModelName != modelName
	if providerChanged || modelChanged {
		if previous := c.ensureModel(session.Provider, session.ModelName); previous.ActiveSessions > 0 {
			previous.ActiveSessions--
		}
		if !session.HasToken && !session.HasTool && !session.HasPatch && !session.HasResult && !session.HasTest {
			if previous := c.ensureModel(session.Provider, session.ModelName); previous.SessionsStarted > 0 {
				previous.SessionsStarted--
			}
		}
	}
	if session.Provider == provider && session.ModelName == modelName {
		if newSession {
			model := c.ensureModel(provider, modelName)
			model.ActiveSessions++
			model.SessionsStarted++
		}
		return
	}
	session.Provider = provider
	session.ModelName = modelName
	model := c.ensureModel(provider, modelName)
	if !newSession && !providerChanged && !modelChanged && session.Active {
		return
	}
	model.ActiveSessions++
	model.SessionsStarted++
}

func (c *LiveRealtimeMetricsCollector) ObserveDelta(delta domain.AgentDelta) {
	if c == nil || strings.TrimSpace(delta.SessionID) == "" || strings.TrimSpace(delta.Provider) == "" || strings.TrimSpace(delta.ModelName) == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	session, ok := c.sessions[delta.SessionID]
	if !ok {
		session = &liveRealtimeSession{
			SessionID: delta.SessionID,
			Provider:  delta.Provider,
			ModelName: delta.ModelName,
			StartedAt: delta.Timestamp.UTC(),
			Active:    true,
		}
		c.sessions[delta.SessionID] = session
		model := c.ensureModel(delta.Provider, delta.ModelName)
		model.ActiveSessions++
		model.SessionsStarted++
	}
	if session.Provider == "" || session.ModelName == "" {
		session.Provider = delta.Provider
		session.ModelName = delta.ModelName
		model := c.ensureModel(delta.Provider, delta.ModelName)
		model.ActiveSessions++
		model.SessionsStarted++
	}
	model := c.ensureModel(session.Provider, session.ModelName)
	if !session.TransportCounted {
		session.TransportMode = liveTransportMode(delta)
		switch session.TransportMode {
		case domain.RuntimeTransportNativeStream:
			model.NativeStreamSessions++
		case domain.RuntimeTransportPseudoRealtime:
			model.PseudoRealtimeSessions++
		default:
			model.BufferedSessions++
		}
		session.TransportCounted = true
	}
	if session.StartedAt.IsZero() {
		session.StartedAt = delta.Timestamp.UTC()
	}
	latencyMS := func() int64 {
		if session.StartedAt.IsZero() || delta.Timestamp.IsZero() {
			return 0
		}
		return delta.Timestamp.Sub(session.StartedAt).Milliseconds()
	}
	switch delta.Kind {
	case domain.AgentDeltaToken:
		model.TokensStreamedTotal++
		if !session.HasToken {
			session.HasToken = true
			model.TimeToFirstTokenMS.Observe(latencyMS())
		}
	case domain.AgentDeltaToolStarted:
		model.ToolsStartedTotal++
		if !session.HasTool {
			session.HasTool = true
			model.TimeToFirstToolMS.Observe(latencyMS())
		}
	case domain.AgentDeltaToolFinished:
		model.ToolsFinishedTotal++
	case domain.AgentDeltaPatchPreview:
		model.PatchesPreviewedTotal++
		if !session.HasPatch {
			session.HasPatch = true
			model.TimeToFirstPatchMS.Observe(latencyMS())
		}
	case domain.AgentDeltaPatchApplyFinish:
		model.PatchesAppliedTotal++
	case domain.AgentDeltaPartialResult, domain.AgentDeltaFinalResult:
		if !session.HasResult {
			session.HasResult = true
			model.TimeToFirstResultMS.Observe(latencyMS())
		}
	case domain.AgentDeltaTestStarted:
		model.TestsStartedTotal++
		if !session.HasTest {
			session.HasTest = true
			model.TimeToFirstTestMS.Observe(latencyMS())
		}
	case domain.AgentDeltaTestFinished:
		model.TestsFinishedTotal++
	}
}

func (c *LiveRealtimeMetricsCollector) CompleteSession(sessionID string, provider string, modelName string, finishedAt time.Time, failed bool) {
	if c == nil || strings.TrimSpace(sessionID) == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	session, ok := c.sessions[sessionID]
	if !ok {
		session = &liveRealtimeSession{
			SessionID: sessionID,
			Provider:  provider,
			ModelName: modelName,
			StartedAt: finishedAt.UTC(),
		}
		c.sessions[sessionID] = session
	}
	if session.Provider == "" {
		session.Provider = provider
	}
	if session.ModelName == "" {
		session.ModelName = modelName
	}
	if strings.TrimSpace(session.Provider) == "" || strings.TrimSpace(session.ModelName) == "" {
		return
	}
	model := c.ensureModel(session.Provider, session.ModelName)
	if session.Active && model.ActiveSessions > 0 {
		model.ActiveSessions--
	}
	session.Active = false
	if session.Completed {
		return
	}
	session.Completed = true
	model.SessionsCompleted++
	if failed {
		model.SessionsFailed++
	}
	if !session.StartedAt.IsZero() && !finishedAt.IsZero() {
		model.TotalCompletionMS.Observe(finishedAt.Sub(session.StartedAt).Milliseconds())
	}
}

func (c *LiveRealtimeMetricsCollector) Snapshot() LiveRealtimeMetricsSnapshot {
	if c == nil {
		return LiveRealtimeMetricsSnapshot{}
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	models := make([]LiveRealtimeModelMetricsSnapshot, 0, len(c.models))
	var active int64
	for _, metrics := range c.models {
		active += metrics.ActiveSessions
		models = append(models, metrics.Snapshot())
	}
	sort.Slice(models, func(i, j int) bool {
		if models[i].Provider == models[j].Provider {
			return models[i].ModelName < models[j].ModelName
		}
		return models[i].Provider < models[j].Provider
	})
	return LiveRealtimeMetricsSnapshot{
		ActiveSessions:  active,
		TrackedSessions: int64(len(c.sessions)),
		Models:          models,
	}
}

func (c *LiveRealtimeMetricsCollector) ModelSummary(provider, model string) (LiveRealtimeModelSummary, bool) {
	if c == nil {
		return LiveRealtimeModelSummary{}, false
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	metrics, ok := c.models[liveRealtimeModelKey(provider, model)]
	if !ok || metrics == nil {
		return LiveRealtimeModelSummary{}, false
	}
	snapshot := metrics.Snapshot()
	return LiveRealtimeModelSummary{
		Provider:               snapshot.Provider,
		ModelName:              snapshot.ModelName,
		FailureRate:            snapshot.FailureRate,
		NativeStreamSessions:   snapshot.NativeStreamSessions,
		PseudoRealtimeSessions: snapshot.PseudoRealtimeSessions,
		BufferedSessions:       snapshot.BufferedSessions,
		AvgTimeToFirstTokenMS:  snapshot.AvgTimeToFirstTokenMS,
		AvgTotalCompletionMS:   snapshot.AvgTotalCompletionMS,
	}, true
}
