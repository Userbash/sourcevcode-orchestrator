package domain

import "time"

type AdaptiveExecutionMode string

const (
	AdaptiveExecutionModeBalanced       AdaptiveExecutionMode = "balanced"
	AdaptiveExecutionModeThroughput     AdaptiveExecutionMode = "throughput"
	AdaptiveExecutionModeLatencyGuarded AdaptiveExecutionMode = "latency_guarded"
	AdaptiveExecutionModeRecovery       AdaptiveExecutionMode = "recovery"
)

type AdaptiveAgentSignal struct {
	AgentID       string      `json:"agent_id"`
	Status        AgentStatus `json:"status"`
	ErrorRate     float64     `json:"error_rate"`
	RoutingWeight float64     `json:"routing_weight"`
	Issue         string      `json:"issue,omitempty"`
	Action        string      `json:"action,omitempty"`
}

type AdaptiveDiagnostics struct {
	HealthyAgents    int                   `json:"healthy_agents"`
	DegradedAgents   int                   `json:"degraded_agents"`
	SuppressedAgents int                   `json:"suppressed_agents"`
	OfflineAgents    int                   `json:"offline_agents"`
	AverageErrorRate float64               `json:"average_error_rate"`
	TopSignals       []AdaptiveAgentSignal `json:"top_signals,omitempty"`
	ObservedAt       time.Time             `json:"observed_at"`
}

type AdaptiveDecision struct {
	Mode               AdaptiveExecutionMode `json:"mode"`
	Reason             string                `json:"reason"`
	MaxParallelism     int                   `json:"max_parallelism"`
	PreferredRouteMode string                `json:"preferred_route_mode,omitempty"`
	SuppressedAgents   []string              `json:"suppressed_agents,omitempty"`
	RecoveryActions    []string              `json:"recovery_actions,omitempty"`
	Diagnostics        AdaptiveDiagnostics   `json:"diagnostics"`
	DecidedAt          time.Time             `json:"decided_at"`
}
