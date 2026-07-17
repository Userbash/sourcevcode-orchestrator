package domain

import "time"

type RAGDocument struct {
	DocumentID     string         `json:"document_id"`
	Scope          string         `json:"scope"`
	OwnerType      string         `json:"owner_type,omitempty"`
	OwnerID        string         `json:"owner_id,omitempty"`
	SourceType     string         `json:"source_type,omitempty"`
	SourceRef      string         `json:"source_ref,omitempty"`
	Title          string         `json:"title,omitempty"`
	ContentText    string         `json:"content_text,omitempty"`
	ContentSummary string         `json:"content_summary,omitempty"`
	Metadata       map[string]any `json:"metadata,omitempty"`
	Importance     float64        `json:"importance,omitempty"`
	RepoID         string         `json:"repo_id,omitempty"`
	Branch         string         `json:"branch,omitempty"`
	CommitSHA      string         `json:"commit_sha,omitempty"`
	ArtifactIDs    []string       `json:"artifact_ids,omitempty"`
	CreatedAt      time.Time      `json:"created_at"`
	UpdatedAt      time.Time      `json:"updated_at"`
	LastAccessedAt time.Time      `json:"last_accessed_at"`
}

type RAGMemoryRecord struct {
	MemoryID   string         `json:"memory_id"`
	MemoryType string         `json:"memory_type"`
	Scope      string         `json:"scope"`
	OwnerID    string         `json:"owner_id,omitempty"`
	Content    map[string]any `json:"content,omitempty"`
	Summary    string         `json:"summary,omitempty"`
	Embedding  []float64      `json:"embedding,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
	Confidence float64        `json:"confidence,omitempty"`
	Importance float64        `json:"importance,omitempty"`
	RepoID     string         `json:"repo_id,omitempty"`
	Branch     string         `json:"branch,omitempty"`
	CommitSHA  string         `json:"commit_sha,omitempty"`
	CreatedAt  time.Time      `json:"created_at"`
	UpdatedAt  time.Time      `json:"updated_at"`
}

type VFSArtifact struct {
	ArtifactID string         `json:"artifact_id"`
	StorageURI string         `json:"storage_uri"`
	SHA256     string         `json:"sha256"`
	SizeBytes  int64          `json:"size_bytes"`
	MIMEType   string         `json:"mime_type,omitempty"`
	Content    []byte         `json:"content,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
	CreatedAt  time.Time      `json:"created_at"`
}

type VFSCheckpointRecord struct {
	Path       string         `json:"path"`
	TaskID     string         `json:"task_id,omitempty"`
	AgentID    string         `json:"agent_id,omitempty"`
	Checkpoint map[string]any `json:"checkpoint"`
	Checksum   string         `json:"checksum"`
	Integrity  string         `json:"integrity,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
	StorageURI string         `json:"storage_uri,omitempty"`
	CreatedAt  time.Time      `json:"created_at"`
	UpdatedAt  time.Time      `json:"updated_at"`
}

type ReasoningDecisionPoint struct {
	Kind     string         `json:"kind"`
	Summary  string         `json:"summary,omitempty"`
	Outcome  string         `json:"outcome,omitempty"`
	Metadata map[string]any `json:"metadata,omitempty"`
}

type ReasoningTrace struct {
	TraceID              string                   `json:"trace_id"`
	SessionID            string                   `json:"session_id,omitempty"`
	TaskID               string                   `json:"task_id,omitempty"`
	ParentTaskID         string                   `json:"parent_task_id,omitempty"`
	AgentID              string                   `json:"agent_id,omitempty"`
	Provider             string                   `json:"provider,omitempty"`
	ModelName            string                   `json:"model_name,omitempty"`
	TaskType             TaskType                 `json:"task_type,omitempty"`
	Branch               string                   `json:"branch,omitempty"`
	PromptSummary        string                   `json:"prompt_summary,omitempty"`
	ReflectionSummary    string                   `json:"reflection_summary,omitempty"`
	ResultSummary        string                   `json:"result_summary,omitempty"`
	ReasoningMode        string                   `json:"reasoning_mode,omitempty"`
	RetrievalUsed        bool                     `json:"retrieval_used,omitempty"`
	VectorMemoryCount    int                      `json:"vector_memory_count,omitempty"`
	RouteMemoryCount     int                      `json:"route_memory_count,omitempty"`
	ReasoningMemoryCount int                      `json:"reasoning_memory_count,omitempty"`
	LatencyMS            int64                    `json:"latency_ms,omitempty"`
	DecisionPoints       []ReasoningDecisionPoint `json:"decision_points,omitempty"`
	NextQuestions        []string                 `json:"next_questions,omitempty"`
	Metadata             map[string]any           `json:"metadata,omitempty"`
	CreatedAt            time.Time                `json:"created_at"`
}
