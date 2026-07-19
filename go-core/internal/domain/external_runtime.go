package domain

import (
	"context"
	"time"
)

type CodingRuntimeMode string

const (
	CodingRuntimeModePlan   CodingRuntimeMode = "plan"
	CodingRuntimeModeBuild  CodingRuntimeMode = "build"
	CodingRuntimeModeReview CodingRuntimeMode = "review"
)

type CodingRuntimeRequest struct {
	Task             Task
	Plan             ExecutionPlan
	Mode             CodingRuntimeMode
	AllowedTools     []string
	AllowedSubagents []string
	RAGResults       []RAGResult
	Metadata         map[string]any
}

type CodingRuntimeSession struct {
	Runtime    string
	SessionID  string
	Provider   string
	Model      string
	AcceptedAt time.Time
	Metadata   map[string]any
}

type CodingRuntimeEvent struct {
	SessionID string
	TaskID    string
	Kind      string
	Message   string
	Progress  float64
	Timestamp time.Time
	Metadata  map[string]any
}

type ExternalCodingRuntime interface {
	Name() string
	Supports(task Task) bool
	StartTask(ctx context.Context, req CodingRuntimeRequest) (CodingRuntimeSession, error)
	WaitTask(ctx context.Context, session CodingRuntimeSession) (AgentResult, error)
	AbortTask(ctx context.Context, sessionID string) error
	Events(ctx context.Context, sessionID string) (<-chan CodingRuntimeEvent, error)
}
