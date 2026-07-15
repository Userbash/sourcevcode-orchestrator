package domain

import "time"

type DegradationSample struct {
	TaskID             string     `json:"task_id"`
	ParentTaskID       string     `json:"parent_task_id,omitempty"`
	AgentID            string     `json:"agent_id,omitempty"`
	Status             TaskStatus `json:"status"`
	QueueLatencyMS     int64      `json:"queue_latency_ms,omitempty"`
	ExecutionLatencyMS int64      `json:"execution_latency_ms,omitempty"`
	TotalLatencyMS     int64      `json:"total_latency_ms,omitempty"`
	EventKinds         []string   `json:"event_kinds,omitempty"`
}

type DegradationTrace struct {
	TraceID                string              `json:"trace_id"`
	SuiteID                string              `json:"suite_id,omitempty"`
	Subject                string              `json:"subject,omitempty"`
	SessionID              string              `json:"session_id"`
	Branch                 string              `json:"branch,omitempty"`
	Scenario               string              `json:"scenario,omitempty"`
	TaskType               TaskType            `json:"task_type,omitempty"`
	WorkflowCount          int                 `json:"workflow_count"`
	CompletedCount         int                 `json:"completed_count,omitempty"`
	FailedCount            int                 `json:"failed_count,omitempty"`
	DeadLetteredCount      int                 `json:"dead_lettered_count,omitempty"`
	ParallelWidth          int                 `json:"parallel_width,omitempty"`
	TotalLatencyMS         int64               `json:"total_latency_ms,omitempty"`
	MeanQueueLatencyMS     int64               `json:"mean_queue_latency_ms,omitempty"`
	MeanExecutionLatencyMS int64               `json:"mean_execution_latency_ms,omitempty"`
	ThroughputPerSecond    float64             `json:"throughput_per_second,omitempty"`
	Samples                []DegradationSample `json:"samples,omitempty"`
	Metadata               map[string]any      `json:"metadata,omitempty"`
	CollectedAt            time.Time           `json:"collected_at"`
}
