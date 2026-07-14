package domain

import "time"

type TaskSignature struct {
	Key             string     `json:"key"`
	TaskType        TaskType   `json:"task_type"`
	Capability      string     `json:"capability"`
	Complexity      Complexity `json:"complexity"`
	Project         string     `json:"project,omitempty"`
	RepoPath        string     `json:"repo_path,omitempty"`
	RepoFingerprint string     `json:"repo_fingerprint,omitempty"`
	Branch          string     `json:"branch,omitempty"`
	Files           []string   `json:"files,omitempty"`
	Modules         []string   `json:"modules,omitempty"`
	Extensions      []string   `json:"extensions,omitempty"`
	Constraints     []string   `json:"constraints,omitempty"`
	Description     string     `json:"description,omitempty"`
	NormalizedText  string     `json:"normalized_text,omitempty"`
	Embedding       []float64  `json:"embedding,omitempty"`
	CreatedAt       time.Time  `json:"created_at"`
}

type RouteMemoryRecord struct {
	RouteID         string         `json:"route_id"`
	SessionID       string         `json:"session_id,omitempty"`
	TaskID          string         `json:"task_id"`
	ParentTaskID    string         `json:"parent_task_id,omitempty"`
	RootTaskID      string         `json:"root_task_id,omitempty"`
	TaskSignature   TaskSignature  `json:"task_signature"`
	TaskType        TaskType       `json:"task_type"`
	Capability      string         `json:"capability"`
	Complexity      Complexity     `json:"complexity"`
	Project         string         `json:"project,omitempty"`
	RepoPath        string         `json:"repo_path,omitempty"`
	RepoFingerprint string         `json:"repo_fingerprint,omitempty"`
	Branch          string         `json:"branch,omitempty"`
	AgentID         string         `json:"agent_id"`
	Provider        string         `json:"provider,omitempty"`
	ModelName       string         `json:"model_name,omitempty"`
	PlanMode        string         `json:"plan_mode,omitempty"`
	Success         bool           `json:"success"`
	Confidence      float64        `json:"confidence,omitempty"`
	LatencyMS       int64          `json:"latency_ms,omitempty"`
	ReviewPassed    bool           `json:"review_passed,omitempty"`
	TestsPassed     bool           `json:"tests_passed,omitempty"`
	CostEstimate    float64        `json:"cost_estimate,omitempty"`
	ErrorCount      int            `json:"error_count,omitempty"`
	Summary         string         `json:"summary,omitempty"`
	Embedding       []float64      `json:"embedding,omitempty"`
	Metadata        map[string]any `json:"metadata,omitempty"`
	CreatedAt       time.Time      `json:"created_at"`
	UpdatedAt       time.Time      `json:"updated_at"`
}

type RouteSearchResult struct {
	Record     RouteMemoryRecord `json:"record"`
	Similarity float64           `json:"similarity"`
}

type RouteScoreBreakdown struct {
	Samples         int     `json:"samples"`
	SuccessRate     float64 `json:"success_rate"`
	ConfidenceScore float64 `json:"confidence_score"`
	ReviewPassRate  float64 `json:"review_pass_rate"`
	TestPassRate    float64 `json:"test_pass_rate"`
	RecencyScore    float64 `json:"recency_score"`
	CostEfficiency  float64 `json:"cost_efficiency"`
	HistoricalScore float64 `json:"historical_score"`
}
