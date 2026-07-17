package domain

import (
	"context"
	"time"
)

const (
	LegacyReasoningModel = "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"
	TargetReasoningModel = "gemma4-12b-agentic-fable5:q4_k_m"
	TargetKernelProvider = "ai_kernel"
)

type SelfLearningRole string

const (
	SelfLearningRoleReasoning     SelfLearningRole = "reasoning"
	SelfLearningRoleCoding        SelfLearningRole = "coding"
	SelfLearningRoleCritic        SelfLearningRole = "critic"
	SelfLearningRoleSyntheticData SelfLearningRole = "synthetic_data"
)

type TraceRecordStatus string

const (
	TraceRecordStatusPending TraceRecordStatus = "pending"
	TraceRecordStatusSuccess TraceRecordStatus = "success"
	TraceRecordStatusFail    TraceRecordStatus = "fail"
)

type FineTuneJobStatus string

const (
	FineTuneJobStatusQueued    FineTuneJobStatus = "queued"
	FineTuneJobStatusRunning   FineTuneJobStatus = "running"
	FineTuneJobStatusSucceeded FineTuneJobStatus = "succeeded"
	FineTuneJobStatusFailed    FineTuneJobStatus = "failed"
)

type SelfLearningModel struct {
	Provider        string             `json:"provider"`
	ModelName       string             `json:"model_name"`
	Roles           []SelfLearningRole `json:"roles,omitempty"`
	ContextWindow   int                `json:"context_window,omitempty"`
	MaxOutputTokens int                `json:"max_output_tokens,omitempty"`
	Quantization    string             `json:"quantization,omitempty"`
	Format          string             `json:"format,omitempty"`
	Specialization  []string           `json:"specialization,omitempty"`
	HotReloadable   bool               `json:"hot_reloadable,omitempty"`
	Available       bool               `json:"available"`
	Status          string             `json:"status,omitempty"`
	ObservedAt      time.Time          `json:"observed_at"`
	Metadata        map[string]any     `json:"metadata,omitempty"`
}

type SelfLearningRegistrySnapshot struct {
	Provider      string              `json:"provider"`
	TargetModel   string              `json:"target_model"`
	Preferred     SelfLearningModel   `json:"preferred"`
	Alternatives  []SelfLearningModel `json:"alternatives,omitempty"`
	LegacyPresent bool                `json:"legacy_present,omitempty"`
	ObservedAt    time.Time           `json:"observed_at"`
	UpdatedAt     time.Time           `json:"updated_at"`
}

type ReasoningRequest struct {
	Prompt       string         `json:"prompt"`
	SystemPrompt string         `json:"system_prompt,omitempty"`
	SessionID    string         `json:"session_id,omitempty"`
	TaskID       string         `json:"task_id,omitempty"`
	Context      map[string]any `json:"context,omitempty"`
}

type ReasoningResponse struct {
	Thought   string         `json:"thought,omitempty"`
	RAGQuery  string         `json:"rag_query,omitempty"`
	Code      string         `json:"code,omitempty"`
	FinalText string         `json:"final_text,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type RAGQuery struct {
	Query     string         `json:"query"`
	SessionID string         `json:"session_id,omitempty"`
	TaskID    string         `json:"task_id,omitempty"`
	Limit     int            `json:"limit,omitempty"`
	Filters   map[string]any `json:"filters,omitempty"`
}

type RAGResult struct {
	DocumentID string         `json:"document_id,omitempty"`
	MemoryID   string         `json:"memory_id,omitempty"`
	Score      float64        `json:"score,omitempty"`
	Content    string         `json:"content,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
}

type CodeExecutionResult struct {
	Status      TraceRecordStatus `json:"status"`
	Compiler    string            `json:"compiler,omitempty"`
	Command     []string          `json:"command,omitempty"`
	Score       float64           `json:"score,omitempty"`
	Stdout      string            `json:"stdout,omitempty"`
	Stderr      string            `json:"stderr,omitempty"`
	ErrorLog    string            `json:"error_log,omitempty"`
	DurationMS  int64             `json:"duration_ms,omitempty"`
	CompletedAt time.Time         `json:"completed_at,omitempty"`
}

type TraceRecord struct {
	TraceID        string              `json:"trace_id"`
	SessionID      string              `json:"session_id,omitempty"`
	TaskID         string              `json:"task_id,omitempty"`
	Provider       string              `json:"provider,omitempty"`
	ModelName      string              `json:"model_name,omitempty"`
	Prompt         string              `json:"prompt"`
	ContextRAG     []RAGResult         `json:"context_rag,omitempty"`
	ThoughtProcess string              `json:"thought_process,omitempty"`
	GeneratedCode  string              `json:"generated_code,omitempty"`
	FinalAnswer    string              `json:"final_answer,omitempty"`
	Evaluation     CodeExecutionResult `json:"evaluation"`
	Metadata       map[string]any      `json:"metadata,omitempty"`
	CreatedAt      time.Time           `json:"created_at"`
}

type PreferenceExample struct {
	DatasetID string         `json:"dataset_id,omitempty"`
	Prompt    string         `json:"prompt"`
	Chosen    TraceRecord    `json:"chosen"`
	Rejected  TraceRecord    `json:"rejected"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type FineTuneJob struct {
	JobID         string            `json:"job_id"`
	ModelName     string            `json:"model_name"`
	BaseModel     string            `json:"base_model,omitempty"`
	DatasetPath   string            `json:"dataset_path"`
	AdapterPath   string            `json:"adapter_path,omitempty"`
	MergedPath    string            `json:"merged_path,omitempty"`
	Command       []string          `json:"command,omitempty"`
	Status        FineTuneJobStatus `json:"status"`
	ErrorLog      string            `json:"error_log,omitempty"`
	StartedAt     *time.Time        `json:"started_at,omitempty"`
	CompletedAt   *time.Time        `json:"completed_at,omitempty"`
	HotReloadedAt *time.Time        `json:"hot_reloaded_at,omitempty"`
	Metadata      map[string]any    `json:"metadata,omitempty"`
}

type HotReloadRequest struct {
	Provider     string         `json:"provider"`
	ModelName    string         `json:"model_name"`
	ModelPath    string         `json:"model_path"`
	ManifestPath string         `json:"manifest_path,omitempty"`
	Metadata     map[string]any `json:"metadata,omitempty"`
}

type ModelDiscovery interface {
	Start(ctx context.Context)
	Refresh(ctx context.Context) (SelfLearningRegistrySnapshot, error)
	Snapshot() SelfLearningRegistrySnapshot
}

type ReasoningEngine interface {
	Think(ctx context.Context, request ReasoningRequest) (ReasoningResponse, error)
}

type RAGRetriever interface {
	Retrieve(ctx context.Context, query RAGQuery) ([]RAGResult, error)
}

type TraceRecorder interface {
	RecordTrace(ctx context.Context, trace TraceRecord) error
}

type CodeEvaluator interface {
	Evaluate(ctx context.Context, trace TraceRecord) (CodeExecutionResult, error)
}

type PreferenceDatasetBuilder interface {
	BuildPreferenceDataset(ctx context.Context, since time.Time) ([]PreferenceExample, error)
}

type Trainer interface {
	StartTraining(ctx context.Context, job FineTuneJob) (FineTuneJob, error)
}

type ModelHotReloader interface {
	ReloadModel(ctx context.Context, request HotReloadRequest) error
}
