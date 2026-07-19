package domain

import "time"

type AgentStatus string

const (
	AgentStatusReady       AgentStatus = "ready"
	AgentStatusBusy        AgentStatus = "busy"
	AgentStatusDegraded    AgentStatus = "degraded"
	AgentStatusOffline     AgentStatus = "offline"
	AgentStatusMaintenance AgentStatus = "maintenance"
)

type TaskType string

const (
	TaskTypePlan     TaskType = "plan"
	TaskTypeCode     TaskType = "code"
	TaskTypeReview   TaskType = "review"
	TaskTypeTest     TaskType = "test"
	TaskTypeDocs     TaskType = "docs"
	TaskTypeFix      TaskType = "fix"
	TaskTypeResearch TaskType = "research"
)

type Priority string

const (
	PriorityLow      Priority = "low"
	PriorityNormal   Priority = "normal"
	PriorityHigh     Priority = "high"
	PriorityCritical Priority = "critical"
)

type TaskStatus string

const (
	TaskStatusQueued       TaskStatus = "queued"
	TaskStatusAccepted     TaskStatus = "accepted"
	TaskStatusRejected     TaskStatus = "rejected"
	TaskStatusWaitingInput TaskStatus = "waiting_input"
	TaskStatusRunning      TaskStatus = "running"
	TaskStatusCompleted    TaskStatus = "completed"
	TaskStatusDeadLettered TaskStatus = "dead_lettered"
	TaskStatusDone         TaskStatus = "done"
	TaskStatusFailed       TaskStatus = "failed"
)

type Complexity string

const (
	ComplexityLow      Complexity = "low"
	ComplexityMedium   Complexity = "medium"
	ComplexityHigh     Complexity = "high"
	ComplexityCritical Complexity = "critical"
)

type TaskInput struct {
	Description        string   `json:"description"`
	Files              []string `json:"files,omitempty"`
	Constraints        []string `json:"constraints,omitempty"`
	AcceptanceCriteria []string `json:"acceptance_criteria,omitempty"`
}

type TaskContext struct {
	Project  string `json:"project"`
	RepoPath string `json:"repo_path,omitempty"`
	Branch   string `json:"branch,omitempty"`
}

type ModelSelection struct {
	Provider                 string         `json:"provider"`
	ModelName                string         `json:"model_name"`
	Complexity               Complexity     `json:"complexity"`
	RequiresSecondaryReview  bool           `json:"requires_secondary_review,omitempty"`
	Reason                   string         `json:"reason,omitempty"`
	DetectedKeywords         []string       `json:"detected_keywords,omitempty"`
	MatchedHighRiskRules     []string       `json:"matched_high_risk_rules,omitempty"`
	MatchedLowRiskExemptions []string       `json:"matched_low_risk_exemptions,omitempty"`
	SupportLanes             []SupportLane  `json:"support_lanes,omitempty"`
	SelectionTrace           map[string]any `json:"selection_trace,omitempty"`
}

type SupportLane struct {
	Provider           string     `json:"provider"`
	ModelName          string     `json:"model_name,omitempty"`
	Role               string     `json:"role"`
	MaxComplexity      Complexity `json:"max_complexity,omitempty"`
	Capabilities       []string   `json:"capabilities,omitempty"`
	SupportedTaskTypes []TaskType `json:"supported_task_types,omitempty"`
	Reason             string     `json:"reason,omitempty"`
}

type Task struct {
	ID                 string         `json:"id"`
	SessionID          string         `json:"session_id,omitempty"`
	ParentTaskID       string         `json:"parent_task_id,omitempty"`
	Type               TaskType       `json:"type"`
	Priority           Priority       `json:"priority"`
	RequiredCapability string         `json:"required_capability,omitempty"`
	AssignedProvider   string         `json:"assigned_provider,omitempty"`
	AssignedModel      string         `json:"assigned_model,omitempty"`
	Complexity         Complexity     `json:"complexity,omitempty"`
	MemoryScope        string         `json:"memory_scope,omitempty"`
	MemoryKeys         []string       `json:"memory_keys,omitempty"`
	CachePolicy        string         `json:"cache_policy,omitempty"`
	Input              TaskInput      `json:"input"`
	Context            TaskContext    `json:"context"`
	Dependencies       []string       `json:"dependencies,omitempty"`
	BranchID           string         `json:"branch_id,omitempty"`
	DraftLayer         string         `json:"draft_layer,omitempty"`
	CheckpointPolicy   string         `json:"checkpoint_policy,omitempty"`
	ReviewDepth        int            `json:"review_depth,omitempty"`
	ResumeToken        string         `json:"resume_token,omitempty"`
	EstimatedCost      float64        `json:"estimated_cost,omitempty"`
	ExecutionContract  map[string]any `json:"execution_contract,omitempty"`
	RepoFingerprint    string         `json:"repo_fingerprint,omitempty"`
	RoutingHints       map[string]any `json:"routing_hints,omitempty"`
	CreatedAt          time.Time      `json:"created_at"`
}

type TaskAcceptance struct {
	TaskID                  string     `json:"task_id"`
	Status                  TaskStatus `json:"status"`
	AgentID                 string     `json:"agent_id,omitempty"`
	Complexity              Complexity `json:"complexity"`
	Reason                  string     `json:"reason"`
	Capability              string     `json:"capability,omitempty"`
	Provider                string     `json:"provider,omitempty"`
	ModelName               string     `json:"model_name,omitempty"`
	RequiresSecondaryReview bool       `json:"requires_secondary_review,omitempty"`
	AcceptedAt              time.Time  `json:"accepted_at"`
}

type PlanStep struct {
	ID            string   `json:"id"`
	Title         string   `json:"title"`
	Capability    string   `json:"capability"`
	WorkerClass   string   `json:"worker_class,omitempty"`
	ClusterID     string   `json:"cluster_id,omitempty"`
	ContextBudget int      `json:"context_budget,omitempty"`
	ConflictKeys  []string `json:"conflict_keys,omitempty"`
	Dependencies  []string `json:"dependencies,omitempty"`
	Files         []string `json:"files,omitempty"`
}

type ExecutionPlan struct {
	TaskID            string         `json:"task_id"`
	Complexity        Complexity     `json:"complexity"`
	PrimaryCapability string         `json:"primary_capability,omitempty"`
	Selection         ModelSelection `json:"selection"`
	Steps             []PlanStep     `json:"steps"`
	CreatedAt         time.Time      `json:"created_at"`
}

type PlanTaskArtifact struct {
	TaskID            string         `json:"task_id"`
	Title             string         `json:"title"`
	Capability        string         `json:"capability"`
	WorkerClass       string         `json:"worker_class,omitempty"`
	ClusterID         string         `json:"cluster_id,omitempty"`
	ContextBudget     int            `json:"context_budget,omitempty"`
	ConflictKeys      []string       `json:"conflict_keys,omitempty"`
	Provider          string         `json:"provider,omitempty"`
	ModelName         string         `json:"model_name,omitempty"`
	Files             []string       `json:"files,omitempty"`
	Dependencies      []string       `json:"dependencies,omitempty"`
	BranchID          string         `json:"branch_id,omitempty"`
	DraftLayer        string         `json:"draft_layer,omitempty"`
	EstimatedCost     float64        `json:"estimated_cost,omitempty"`
	Weight            float64        `json:"weight,omitempty"`
	ExecutionContract map[string]any `json:"execution_contract,omitempty"`
}

type PlanArtifact struct {
	RootTaskID        string             `json:"root_task_id"`
	PrimaryCapability string             `json:"primary_capability,omitempty"`
	TaskCount         int                `json:"task_count"`
	Tasks             []PlanTaskArtifact `json:"tasks"`
	ParallelGroups    [][]string         `json:"parallel_groups,omitempty"`
	Handoffs          []map[string]any   `json:"handoffs,omitempty"`
	CreatedAt         time.Time          `json:"created_at"`
}

type ParallelPlanStatus string

const (
	ParallelPlanStatusPlanned   ParallelPlanStatus = "planned"
	ParallelPlanStatusRunning   ParallelPlanStatus = "running"
	ParallelPlanStatusBlocked   ParallelPlanStatus = "blocked"
	ParallelPlanStatusFailed    ParallelPlanStatus = "failed"
	ParallelPlanStatusCompleted ParallelPlanStatus = "completed"
)

type ExecutionPlanPreview struct {
	Task             Task          `json:"task"`
	Plan             ExecutionPlan `json:"plan"`
	PlanArtifact     PlanArtifact  `json:"plan_artifact"`
	PendingTaskIDs   []string      `json:"pending_task_ids"`
	CheckpointBranch string        `json:"checkpoint_branch"`
	CreatedAt        time.Time     `json:"created_at"`
}

type ParallelPlanCheckpoint struct {
	Kind             string             `json:"kind"`
	RootTaskID       string             `json:"root_task_id"`
	SessionID        string             `json:"session_id"`
	Branch           string             `json:"branch"`
	RootTask         Task               `json:"root_task,omitempty"`
	Plan             ExecutionPlan      `json:"plan,omitempty"`
	PlanArtifact     PlanArtifact       `json:"plan_artifact"`
	PendingTaskIDs   []string           `json:"pending_task_ids"`
	CompletedTaskIDs []string           `json:"completed_task_ids,omitempty"`
	ResultsByTaskID  map[string]any     `json:"results_by_task_id,omitempty"`
	BatchNo          int                `json:"batch_no"`
	Status           ParallelPlanStatus `json:"status"`
	UpdatedAt        time.Time          `json:"updated_at"`
}

type ResultOutput struct {
	Summary      string           `json:"summary"`
	FilesChanged []string         `json:"files_changed,omitempty"`
	CommandsRun  []string         `json:"commands_run,omitempty"`
	TestResults  []map[string]any `json:"test_results,omitempty"`
	Artifacts    map[string]any   `json:"artifacts,omitempty"`
}

type AgentResult struct {
	TaskID              string       `json:"task_id"`
	AgentID             string       `json:"agent_id"`
	Status              TaskStatus   `json:"status"`
	Confidence          float64      `json:"confidence"`
	Errors              []string     `json:"errors,omitempty"`
	NextRecommendations []string     `json:"next_recommendations,omitempty"`
	Provider            string       `json:"provider,omitempty"`
	ModelName           string       `json:"model_name,omitempty"`
	Output              ResultOutput `json:"output"`
	CompletedAt         time.Time    `json:"completed_at"`
}

type AgentInfo struct {
	ID           string      `json:"id"`
	Type         string      `json:"type"`
	Provider     string      `json:"provider"`
	ModelName    string      `json:"model_name"`
	Capabilities []string    `json:"capabilities"`
	Status       AgentStatus `json:"status"`
}

type AgentRuntimeState struct {
	AgentID            string      `json:"agent_id"`
	Provider           string      `json:"provider,omitempty"`
	Status             AgentStatus `json:"status"`
	PriorityScore      float64     `json:"priority_score"`
	InFlight           int         `json:"in_flight,omitempty"`
	AgentSlotUsage     float64     `json:"agent_slot_usage,omitempty"`
	ModelSlotUsage     float64     `json:"model_slot_usage,omitempty"`
	GlobalSlotUsage    float64     `json:"global_slot_usage,omitempty"`
	ErrorRate          float64     `json:"error_rate"`
	DisabledReason     string      `json:"disabled_reason,omitempty"`
	LastError          string      `json:"last_error,omitempty"`
	LastProbeStatus    string      `json:"last_probe_status,omitempty"`
	LastProbeError     string      `json:"last_probe_error,omitempty"`
	LastProbeLatencyMS int64       `json:"last_probe_latency_ms,omitempty"`
	SuppressedUntil    *time.Time  `json:"suppressed_until,omitempty"`
	UpdatedAt          time.Time   `json:"updated_at"`
}

type PolicyDecision struct {
	Decision      string         `json:"decision"`
	Severity      string         `json:"severity"`
	Reasons       []string       `json:"reasons,omitempty"`
	Evidence      map[string]any `json:"evidence,omitempty"`
	PolicyVersion string         `json:"policy_version,omitempty"`
	NextAction    string         `json:"next_action,omitempty"`
	AgentID       string         `json:"agent_id,omitempty"`
}

type ProviderHealth struct {
	Provider      string     `json:"provider"`
	Configured    bool       `json:"configured"`
	Available     bool       `json:"available"`
	Status        string     `json:"status"`
	BaseURL       string     `json:"base_url,omitempty"`
	Error         string     `json:"error,omitempty"`
	ObservedAt    time.Time  `json:"observed_at"`
	ProbeQueued   bool       `json:"probe_queued,omitempty"`
	CooldownUntil *time.Time `json:"cooldown_until,omitempty"`
	RefreshAfter  *time.Time `json:"refresh_after,omitempty"`
	RuntimeCapabilities *ProviderRuntimeCapabilities `json:"runtime_capabilities,omitempty"`
}

type RuntimeTransportMode string

const (
	RuntimeTransportBuffered       RuntimeTransportMode = "buffered"
	RuntimeTransportPseudoRealtime RuntimeTransportMode = "pseudo_realtime"
	RuntimeTransportNativeStream   RuntimeTransportMode = "native_stream"
)

type ProviderRuntimeCapabilities struct {
	Connected            bool                 `json:"connected"`
	NativeStreaming      bool                 `json:"native_streaming,omitempty"`
	ToolStreaming        bool                 `json:"tool_streaming,omitempty"`
	PatchStreaming       bool                 `json:"patch_streaming,omitempty"`
	TestStreaming        bool                 `json:"test_streaming,omitempty"`
	MaxParallelRequests  int                  `json:"max_parallel_requests,omitempty"`
	SupportsCancellation bool                 `json:"supports_cancellation,omitempty"`
	TransportMode        RuntimeTransportMode `json:"transport_mode,omitempty"`
	ObservedAt           time.Time            `json:"observed_at,omitempty"`
}

type ProviderAPIError struct {
	Provider     string    `json:"provider,omitempty"`
	Model        string    `json:"model,omitempty"`
	Operation    string    `json:"operation,omitempty"`
	Endpoint     string    `json:"endpoint,omitempty"`
	EndpointKind string    `json:"endpoint_kind,omitempty"`
	HTTPStatus   int       `json:"http_status,omitempty"`
	UpstreamCode string    `json:"upstream_code,omitempty"`
	UpstreamType string    `json:"upstream_type,omitempty"`
	Message      string    `json:"message,omitempty"`
	Retryable    bool      `json:"retryable,omitempty"`
	Category     string    `json:"category,omitempty"`
	RequestID    string    `json:"request_id,omitempty"`
	ObservedAt   time.Time `json:"observed_at,omitempty"`
	LatencyMS    int64     `json:"latency_ms,omitempty"`
}

type ProviderModelStatus struct {
	Provider                string            `json:"provider"`
	ModelName               string            `json:"model_name"`
	Available               bool              `json:"available"`
	Status                  string            `json:"status"`
	Reason                  string            `json:"reason,omitempty"`
	InventoryStatus         string            `json:"inventory_status,omitempty"`
	TransportStatus         string            `json:"transport_status,omitempty"`
	VerificationStatus      string            `json:"verification_status,omitempty"`
	Transport               string            `json:"transport,omitempty"`
	ObservedAt              time.Time         `json:"observed_at"`
	IsDefault               bool              `json:"is_default,omitempty"`
	Metadata                map[string]any    `json:"metadata,omitempty"`
	LastHTTPStatus          int               `json:"last_http_status,omitempty"`
	LastProbeLatencyMS      int64             `json:"last_probe_latency_ms,omitempty"`
	FirstSeenAt             *time.Time        `json:"first_seen_at,omitempty"`
	VerificationStartedAt   *time.Time        `json:"verification_started_at,omitempty"`
	LastStateChangeAt       *time.Time        `json:"last_state_change_at,omitempty"`
	LastSuccessAt           *time.Time        `json:"last_success_at,omitempty"`
	NextVerificationAt      *time.Time        `json:"next_verification_at,omitempty"`
	ExpiresAt               *time.Time        `json:"expires_at,omitempty"`
	ConsecutiveFailures     int               `json:"consecutive_failures,omitempty"`
	ConsecutiveSuccesses    int               `json:"consecutive_successes,omitempty"`
	RegistrationAttempts    int               `json:"registration_attempts,omitempty"`
	PendingCycles           int               `json:"pending_cycles,omitempty"`
	QueueStatus             string            `json:"queue_status,omitempty"`
	QueuePosition           int               `json:"queue_position,omitempty"`
	VerificationIntervalSec int               `json:"verification_interval_sec,omitempty"`
	LastError               *ProviderAPIError `json:"last_error,omitempty"`
}

type ModelCapabilities struct {
	Streaming      bool `json:"streaming"`
	ToolCalling    bool `json:"tool_calling,omitempty"`
	PatchStreaming bool `json:"patch_streaming,omitempty"`
	TestStreaming  bool `json:"test_streaming,omitempty"`
	LongContext    bool `json:"long_context,omitempty"`
}

type ProviderCatalogSnapshot struct {
	Provider                    string                `json:"provider"`
	ProviderID                  string                `json:"provider_id,omitempty"`
	Configured                  bool                  `json:"configured"`
	Available                   bool                  `json:"available"`
	Status                      string                `json:"status,omitempty"`
	BaseURL                     string                `json:"base_url,omitempty"`
	ModelsEndpoint              string                `json:"models_endpoint,omitempty"`
	ChatCompletionsEndpoint     string                `json:"chat_completions_endpoint,omitempty"`
	ResponsesEndpoint           string                `json:"responses_endpoint,omitempty"`
	MessagesEndpoint            string                `json:"messages_endpoint,omitempty"`
	MessagesCountTokensEndpoint string                `json:"messages_count_tokens_endpoint,omitempty"`
	CodexEndpoint               string                `json:"codex_endpoint,omitempty"`
	DefaultModel                string                `json:"default_model,omitempty"`
	Models                      []ProviderModelStatus `json:"models,omitempty"`
	Error                       string                `json:"error,omitempty"`
	ObservedAt                  time.Time             `json:"observed_at,omitempty"`
	RefreshIntervalSec          int                   `json:"refresh_interval_sec,omitempty"`
	RuntimeCapabilities         *ProviderRuntimeCapabilities `json:"runtime_capabilities,omitempty"`
}

type ModuleInfo struct {
	Name     string         `json:"name"`
	Kind     string         `json:"kind"`
	Metadata map[string]any `json:"metadata,omitempty"`
}

type SessionState struct {
	SessionID      string         `json:"session_id"`
	Branch         string         `json:"branch"`
	Version        int            `json:"version"`
	PromptVersion  string         `json:"prompt_version"`
	ContextVersion string         `json:"context_version"`
	State          map[string]any `json:"state"`
	UpdatedAt      time.Time      `json:"updated_at"`
	StorageMode    string         `json:"storage_mode"`
}

type InvalidationEvent struct {
	SessionID   string         `json:"session_id"`
	Branch      string         `json:"branch"`
	Reason      string         `json:"reason"`
	Payload     map[string]any `json:"payload,omitempty"`
	LoggedAt    time.Time      `json:"logged_at"`
	StorageMode string         `json:"storage_mode"`
}

type WorkflowRecord struct {
	Task       Task           `json:"task"`
	Plan       ExecutionPlan  `json:"plan"`
	Acceptance TaskAcceptance `json:"acceptance"`
	Result     *AgentResult   `json:"result,omitempty"`
	UpdatedAt  time.Time      `json:"updated_at"`
}

type ExecutionPlanRun struct {
	Task         Task                   `json:"task"`
	Plan         ExecutionPlan          `json:"plan"`
	PlanArtifact PlanArtifact           `json:"plan_artifact"`
	Checkpoint   ParallelPlanCheckpoint `json:"checkpoint"`
	Workflows    []WorkflowRecord       `json:"workflows"`
	StartedAt    time.Time              `json:"started_at"`
	CompletedAt  time.Time              `json:"completed_at"`
}

type StreamEvent struct {
	ID        string         `json:"id"`
	Stream    string         `json:"stream"`
	Topic     string         `json:"topic"`
	Kind      string         `json:"kind"`
	EntityID  string         `json:"entity_id,omitempty"`
	Timestamp time.Time      `json:"timestamp"`
	Payload   map[string]any `json:"payload,omitempty"`
}

type AgentDeltaKind string

const (
	AgentDeltaStarted          AgentDeltaKind = "started"
	AgentDeltaToken            AgentDeltaKind = "token"
	AgentDeltaThoughtSummary   AgentDeltaKind = "thought_summary"
	AgentDeltaToolCallStarted  AgentDeltaKind = "tool_call_started"
	AgentDeltaToolCallFinished AgentDeltaKind = "tool_call_finished"
	AgentDeltaFilePatch        AgentDeltaKind = "file_patch"
	AgentDeltaCommandStarted   AgentDeltaKind = "command_started"
	AgentDeltaCommandFinished  AgentDeltaKind = "command_finished"
	AgentDeltaPartialResult    AgentDeltaKind = "partial_result"
	AgentDeltaFinalResult      AgentDeltaKind = "final_result"
	AgentDeltaHeartbeat        AgentDeltaKind = "heartbeat"
	AgentDeltaError            AgentDeltaKind = "error"
	AgentDeltaPatchPreview     AgentDeltaKind = "patch.preview"
	AgentDeltaPatchChunk       AgentDeltaKind = "patch.chunk"
	AgentDeltaPatchApplyStart  AgentDeltaKind = "patch.apply.started"
	AgentDeltaPatchApplyFinish AgentDeltaKind = "patch.apply.finished"
	AgentDeltaToolStarted      AgentDeltaKind = "tool.started"
	AgentDeltaToolStdout       AgentDeltaKind = "tool.stdout"
	AgentDeltaToolStderr       AgentDeltaKind = "tool.stderr"
	AgentDeltaToolFinished     AgentDeltaKind = "tool.finished"
	AgentDeltaTestStarted      AgentDeltaKind = "test.started"
	AgentDeltaTestCase         AgentDeltaKind = "test.case"
	AgentDeltaTestFinished     AgentDeltaKind = "test.finished"
)

type AgentDelta struct {
	SessionID string         `json:"session_id,omitempty"`
	TaskID    string         `json:"task_id"`
	AgentID   string         `json:"agent_id,omitempty"`
	Provider  string         `json:"provider,omitempty"`
	ModelName string         `json:"model_name,omitempty"`
	Kind      AgentDeltaKind `json:"kind"`
	Sequence  int64          `json:"sequence,omitempty"`
	Content   string         `json:"content,omitempty"`
	Timestamp time.Time      `json:"timestamp"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type RealtimeExecutionMetrics struct {
	Transport           string `json:"transport,omitempty"`
	NativeStreaming     bool   `json:"native_streaming,omitempty"`
	PseudoRealtime      bool   `json:"pseudo_realtime,omitempty"`
	TimeToFirstTokenMS  int64  `json:"time_to_first_token_ms,omitempty"`
	TimeToFirstToolMS   int64  `json:"time_to_first_tool_ms,omitempty"`
	TimeToFirstPatchMS  int64  `json:"time_to_first_patch_ms,omitempty"`
	TimeToFirstResultMS int64  `json:"time_to_first_result_ms,omitempty"`
	TimeToFirstTestMS   int64  `json:"time_to_first_test_ms,omitempty"`
	TotalCompletionMS   int64  `json:"total_completion_ms,omitempty"`
	TokensStreamed      int    `json:"tokens_streamed,omitempty"`
	ToolsExecuted       int    `json:"tools_executed,omitempty"`
	PatchesApplied      int    `json:"patches_applied,omitempty"`
	TestsExecuted       int    `json:"tests_executed,omitempty"`
}

type LiveSessionState struct {
	SessionID        string                   `json:"session_id"`
	TaskID           string                   `json:"task_id"`
	Mode             string                   `json:"mode,omitempty"`
	AgentID          string                   `json:"agent_id,omitempty"`
	Provider         string                   `json:"provider,omitempty"`
	ModelName        string                   `json:"model_name,omitempty"`
	Capabilities     ModelCapabilities        `json:"capabilities,omitempty"`
	Active           bool                     `json:"active"`
	Progress         float64                  `json:"progress,omitempty"`
	LastDeltaKind    string                   `json:"last_delta_kind,omitempty"`
	LastMessage      string                   `json:"last_message,omitempty"`
	PartialOutput    string                   `json:"partial_output,omitempty"`
	PatchPreview     string                   `json:"patch_preview,omitempty"`
	PatchDraft       string                   `json:"patch_draft,omitempty"`
	ActiveWorkers    []string                 `json:"active_workers,omitempty"`
	ActiveTools      []string                 `json:"active_tools,omitempty"`
	CompletedWorkers []string                 `json:"completed_workers,omitempty"`
	PendingApprovals []string                 `json:"pending_approvals,omitempty"`
	RealtimeMetrics  RealtimeExecutionMetrics `json:"realtime_metrics,omitempty"`
	UpdatedAt        time.Time                `json:"updated_at"`
}

type VectorChunk struct {
	ChunkID        string         `json:"chunk_id"`
	SessionID      string         `json:"session_id"`
	Branch         string         `json:"branch,omitempty"`
	Source         string         `json:"source"`
	SourceID       string         `json:"source_id,omitempty"`
	ChunkIndex     int            `json:"chunk_index"`
	Text           string         `json:"text"`
	NormalizedText string         `json:"normalized_text,omitempty"`
	Terms          []string       `json:"terms,omitempty"`
	Embedding      []float64      `json:"embedding,omitempty"`
	Metadata       map[string]any `json:"metadata,omitempty"`
	CreatedAt      time.Time      `json:"created_at"`
}

type VectorSearchQuery struct {
	SessionID      string    `json:"session_id,omitempty"`
	Branch         string    `json:"branch,omitempty"`
	Text           string    `json:"text"`
	Terms          []string  `json:"terms,omitempty"`
	Embedding      []float64 `json:"embedding,omitempty"`
	TopK           int       `json:"top_k,omitempty"`
	CandidateLimit int       `json:"candidate_limit,omitempty"`
}

type VectorSearchResult struct {
	Chunk         VectorChunk `json:"chunk"`
	Score         float64     `json:"score"`
	Cosine        float64     `json:"cosine"`
	TermOverlap   float64     `json:"term_overlap"`
	KeywordHits   int         `json:"keyword_hits"`
	SummarySignal float64     `json:"summary_signal"`
	RecencyScore  float64     `json:"recency_score"`
}
