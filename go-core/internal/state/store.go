package state

import (
	"context"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type Store interface {
	SaveWorkflow(ctx context.Context, record domain.WorkflowRecord) error
	GetWorkflow(ctx context.Context, workflowID string) (domain.WorkflowRecord, bool, error)
	ListWorkflows(ctx context.Context) ([]domain.WorkflowRecord, error)
	GetSessionState(ctx context.Context, sessionID string, branch string) (domain.SessionState, bool, error)
	SaveSessionState(
		ctx context.Context,
		sessionID string,
		branch string,
		state map[string]any,
		promptVersion string,
		contextVersion string,
		expectedVersion *int,
	) (domain.SessionState, error)
	RecordInvalidation(ctx context.Context, sessionID string, branch string, reason string, payload map[string]any) (domain.InvalidationEvent, error)
	RecentInvalidations(ctx context.Context, sessionID string, branch string, limit int) ([]domain.InvalidationEvent, error)
	UpsertVectorChunks(ctx context.Context, chunks []domain.VectorChunk) error
	ListVectorChunks(ctx context.Context, sessionID string, branch string, limit int) ([]domain.VectorChunk, error)
	UpsertRAGDocuments(ctx context.Context, documents []domain.RAGDocument) error
	ListRAGDocuments(ctx context.Context, scope string, ownerID string, limit int) ([]domain.RAGDocument, error)
	UpsertRAGMemories(ctx context.Context, memories []domain.RAGMemoryRecord) error
	ListRAGMemories(ctx context.Context, scope string, ownerID string, limit int) ([]domain.RAGMemoryRecord, error)
	UpsertRouteMemories(ctx context.Context, memories []domain.RouteMemoryRecord) error
	ListRouteMemories(ctx context.Context, project string, repoFingerprint string, capability string, limit int) ([]domain.RouteMemoryRecord, error)
	UpsertVFSArtifacts(ctx context.Context, artifacts []domain.VFSArtifact) error
	GetVFSArtifact(ctx context.Context, artifactID string) (domain.VFSArtifact, bool, error)
	UpsertVFSCheckpoints(ctx context.Context, checkpoints []domain.VFSCheckpointRecord) error
	GetVFSCheckpoint(ctx context.Context, path string) (domain.VFSCheckpointRecord, bool, error)
	Snapshot() map[string]any
}
