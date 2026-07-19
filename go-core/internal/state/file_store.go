package state

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type FileStore struct {
	path           string
	storageMode    string
	mu             sync.RWMutex
	workflows      map[string]domain.WorkflowRecord
	sessionStates  map[string]domain.SessionState
	invalidations  map[string][]domain.InvalidationEvent
	vectorChunks   map[string]domain.VectorChunk
	ragDocuments   map[string]domain.RAGDocument
	ragMemories    map[string]domain.RAGMemoryRecord
	routeMemories  map[string]domain.RouteMemoryRecord
	vfsArtifacts   map[string]domain.VFSArtifact
	vfsCheckpoints map[string]domain.VFSCheckpointRecord
}

type filePayload struct {
	Workflows      map[string]domain.WorkflowRecord      `json:"workflows"`
	SessionStates  map[string]domain.SessionState        `json:"session_states"`
	Invalidations  map[string][]domain.InvalidationEvent `json:"invalidations"`
	VectorChunks   map[string]domain.VectorChunk         `json:"vector_chunks"`
	RAGDocuments   map[string]domain.RAGDocument         `json:"rag_documents"`
	RAGMemories    map[string]domain.RAGMemoryRecord     `json:"rag_memories"`
	RouteMemories  map[string]domain.RouteMemoryRecord   `json:"route_memories"`
	VFSArtifacts   map[string]domain.VFSArtifact         `json:"vfs_artifacts"`
	VFSCheckpoints map[string]domain.VFSCheckpointRecord `json:"vfs_checkpoints"`
}

func NewFileStore(path string) (*FileStore, error) {
	if path == "" {
		path = filepath.Join("memory_store", "go_core_state.json")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	store := &FileStore{
		path:           path,
		storageMode:    "go_file_store",
		workflows:      map[string]domain.WorkflowRecord{},
		sessionStates:  map[string]domain.SessionState{},
		invalidations:  map[string][]domain.InvalidationEvent{},
		vectorChunks:   map[string]domain.VectorChunk{},
		ragDocuments:   map[string]domain.RAGDocument{},
		ragMemories:    map[string]domain.RAGMemoryRecord{},
		routeMemories:  map[string]domain.RouteMemoryRecord{},
		vfsArtifacts:   map[string]domain.VFSArtifact{},
		vfsCheckpoints: map[string]domain.VFSCheckpointRecord{},
	}
	if err := store.load(); err != nil {
		return nil, err
	}
	return store, nil
}

func (s *FileStore) SaveWorkflow(_ context.Context, record domain.WorkflowRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.workflows[record.Task.ID] = record
	return s.persistLocked()
}

func (s *FileStore) GetWorkflow(_ context.Context, workflowID string) (domain.WorkflowRecord, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, ok := s.workflows[workflowID]
	return record, ok, nil
}

func (s *FileStore) ListWorkflows(_ context.Context) ([]domain.WorkflowRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	records := make([]domain.WorkflowRecord, 0, len(s.workflows))
	for _, record := range s.workflows {
		records = append(records, record)
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].UpdatedAt.After(records[j].UpdatedAt)
	})
	return records, nil
}

func (s *FileStore) WorkflowCount(_ context.Context) (int, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.workflows), nil
}

func (s *FileStore) SaveSessionState(
	_ context.Context,
	sessionID string,
	branch string,
	stateValue map[string]any,
	promptVersion string,
	contextVersion string,
	expectedVersion *int,
) (domain.SessionState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := sessionKey(sessionID, branch)
	current, ok := s.sessionStates[key]
	currentVersion := 0
	if ok {
		currentVersion = current.Version
	}
	if expectedVersion != nil && *expectedVersion != currentVersion {
		return domain.SessionState{}, errors.New("session state version conflict")
	}
	next := domain.SessionState{
		SessionID:      sessionID,
		Branch:         branch,
		Version:        currentVersion + 1,
		PromptVersion:  promptVersion,
		ContextVersion: contextVersion,
		State:          cloneMap(stateValue),
		UpdatedAt:      time.Now().UTC(),
		StorageMode:    s.storageMode,
	}
	s.sessionStates[key] = next
	return cloneSessionState(next), s.persistLocked()
}

func (s *FileStore) GetSessionState(_ context.Context, sessionID string, branch string) (domain.SessionState, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	state, ok := s.sessionStates[sessionKey(sessionID, branch)]
	if !ok {
		return domain.SessionState{}, false, nil
	}
	return cloneSessionState(state), true, nil
}

func (s *FileStore) RecordInvalidation(_ context.Context, sessionID string, branch string, reason string, payload map[string]any) (domain.InvalidationEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	event := domain.InvalidationEvent{
		SessionID:   sessionID,
		Branch:      branch,
		Reason:      reason,
		Payload:     cloneMap(payload),
		LoggedAt:    time.Now().UTC(),
		StorageMode: s.storageMode,
	}
	key := sessionKey(sessionID, branch)
	s.invalidations[key] = append(s.invalidations[key], event)
	return cloneInvalidation(event), s.persistLocked()
}

func (s *FileStore) RecentInvalidations(_ context.Context, sessionID string, branch string, limit int) ([]domain.InvalidationEvent, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	events := s.invalidations[sessionKey(sessionID, branch)]
	start := 0
	if limit > 0 && len(events) > limit {
		start = len(events) - limit
	}
	out := make([]domain.InvalidationEvent, 0, len(events)-start)
	for _, event := range events[start:] {
		out = append(out, cloneInvalidation(event))
	}
	return out, nil
}

func (s *FileStore) UpsertVectorChunks(_ context.Context, chunks []domain.VectorChunk) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, chunk := range chunks {
		copyChunk := cloneVectorChunk(chunk)
		if strings.TrimSpace(copyChunk.ChunkID) == "" {
			continue
		}
		s.vectorChunks[copyChunk.ChunkID] = copyChunk
	}
	return s.persistLocked()
}

func (s *FileStore) ListVectorChunks(_ context.Context, sessionID string, branch string, limit int) ([]domain.VectorChunk, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	sessionID = strings.TrimSpace(sessionID)
	branch = strings.TrimSpace(branch)
	chunks := make([]domain.VectorChunk, 0, len(s.vectorChunks))
	for _, chunk := range s.vectorChunks {
		if sessionID != "" && chunk.SessionID != sessionID {
			continue
		}
		if branch != "" && chunk.Branch != branch {
			continue
		}
		chunks = append(chunks, cloneVectorChunk(chunk))
	}
	sort.Slice(chunks, func(i, j int) bool {
		return chunks[i].CreatedAt.After(chunks[j].CreatedAt)
	})
	if limit > 0 && len(chunks) > limit {
		chunks = chunks[:limit]
	}
	return chunks, nil
}

func (s *FileStore) UpsertRAGDocuments(_ context.Context, documents []domain.RAGDocument) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, doc := range documents {
		copyDoc := cloneRAGDocument(doc)
		if strings.TrimSpace(copyDoc.DocumentID) == "" {
			continue
		}
		if copyDoc.CreatedAt.IsZero() {
			copyDoc.CreatedAt = time.Now().UTC()
		}
		if copyDoc.UpdatedAt.IsZero() {
			copyDoc.UpdatedAt = copyDoc.CreatedAt
		}
		s.ragDocuments[copyDoc.DocumentID] = copyDoc
	}
	return s.persistLocked()
}

func (s *FileStore) ListRAGDocuments(_ context.Context, scope string, ownerID string, limit int) ([]domain.RAGDocument, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	scope = strings.TrimSpace(scope)
	ownerID = strings.TrimSpace(ownerID)
	documents := make([]domain.RAGDocument, 0, len(s.ragDocuments))
	for _, doc := range s.ragDocuments {
		if scope != "" && doc.Scope != scope {
			continue
		}
		if ownerID != "" && doc.OwnerID != ownerID {
			continue
		}
		documents = append(documents, cloneRAGDocument(doc))
	}
	sort.Slice(documents, func(i, j int) bool {
		left := documents[i].UpdatedAt
		right := documents[j].UpdatedAt
		if left.Equal(right) {
			return documents[i].CreatedAt.After(documents[j].CreatedAt)
		}
		return left.After(right)
	})
	if limit > 0 && len(documents) > limit {
		documents = documents[:limit]
	}
	return documents, nil
}

func (s *FileStore) UpsertRAGMemories(_ context.Context, memories []domain.RAGMemoryRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, memory := range memories {
		copyMemory := cloneRAGMemory(memory)
		if strings.TrimSpace(copyMemory.MemoryID) == "" {
			continue
		}
		if copyMemory.CreatedAt.IsZero() {
			copyMemory.CreatedAt = time.Now().UTC()
		}
		if copyMemory.UpdatedAt.IsZero() {
			copyMemory.UpdatedAt = copyMemory.CreatedAt
		}
		s.ragMemories[copyMemory.MemoryID] = copyMemory
	}
	return s.persistLocked()
}

func (s *FileStore) ListRAGMemories(_ context.Context, scope string, ownerID string, limit int) ([]domain.RAGMemoryRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	scope = strings.TrimSpace(scope)
	ownerID = strings.TrimSpace(ownerID)
	memories := make([]domain.RAGMemoryRecord, 0, len(s.ragMemories))
	for _, memory := range s.ragMemories {
		if scope != "" && memory.Scope != scope {
			continue
		}
		if ownerID != "" && memory.OwnerID != ownerID {
			continue
		}
		memories = append(memories, cloneRAGMemory(memory))
	}
	sort.Slice(memories, func(i, j int) bool {
		left := memories[i].UpdatedAt
		right := memories[j].UpdatedAt
		if left.Equal(right) {
			return memories[i].CreatedAt.After(memories[j].CreatedAt)
		}
		return left.After(right)
	})
	if limit > 0 && len(memories) > limit {
		memories = memories[:limit]
	}
	return memories, nil
}

func (s *FileStore) UpsertRouteMemories(_ context.Context, memories []domain.RouteMemoryRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, memory := range memories {
		copyMemory := cloneRouteMemory(memory)
		if strings.TrimSpace(copyMemory.RouteID) == "" {
			continue
		}
		if copyMemory.CreatedAt.IsZero() {
			copyMemory.CreatedAt = time.Now().UTC()
		}
		if copyMemory.UpdatedAt.IsZero() {
			copyMemory.UpdatedAt = copyMemory.CreatedAt
		}
		s.routeMemories[copyMemory.RouteID] = copyMemory
	}
	return s.persistLocked()
}

func (s *FileStore) ListRouteMemories(_ context.Context, project string, repoFingerprint string, capability string, limit int) ([]domain.RouteMemoryRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	project = strings.TrimSpace(project)
	repoFingerprint = strings.TrimSpace(repoFingerprint)
	capability = strings.TrimSpace(capability)
	memories := make([]domain.RouteMemoryRecord, 0, len(s.routeMemories))
	for _, memory := range s.routeMemories {
		if project != "" && memory.Project != project {
			continue
		}
		if repoFingerprint != "" && memory.RepoFingerprint != repoFingerprint {
			continue
		}
		if capability != "" && memory.Capability != capability {
			continue
		}
		memories = append(memories, cloneRouteMemory(memory))
	}
	sort.Slice(memories, func(i, j int) bool {
		left := memories[i].UpdatedAt
		right := memories[j].UpdatedAt
		if left.Equal(right) {
			return memories[i].CreatedAt.After(memories[j].CreatedAt)
		}
		return left.After(right)
	})
	if limit > 0 && len(memories) > limit {
		memories = memories[:limit]
	}
	return memories, nil
}

func (s *FileStore) UpsertVFSArtifacts(_ context.Context, artifacts []domain.VFSArtifact) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, artifact := range artifacts {
		copyArtifact := cloneVFSArtifact(artifact)
		if strings.TrimSpace(copyArtifact.ArtifactID) == "" {
			continue
		}
		if copyArtifact.CreatedAt.IsZero() {
			copyArtifact.CreatedAt = time.Now().UTC()
		}
		s.vfsArtifacts[copyArtifact.ArtifactID] = copyArtifact
	}
	return s.persistLocked()
}

func (s *FileStore) GetVFSArtifact(_ context.Context, artifactID string) (domain.VFSArtifact, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	artifact, ok := s.vfsArtifacts[strings.TrimSpace(artifactID)]
	if !ok {
		return domain.VFSArtifact{}, false, nil
	}
	return cloneVFSArtifact(artifact), true, nil
}

func (s *FileStore) UpsertVFSCheckpoints(_ context.Context, checkpoints []domain.VFSCheckpointRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, checkpoint := range checkpoints {
		copyCheckpoint := cloneVFSCheckpoint(checkpoint)
		if strings.TrimSpace(copyCheckpoint.Path) == "" {
			continue
		}
		if copyCheckpoint.CreatedAt.IsZero() {
			copyCheckpoint.CreatedAt = time.Now().UTC()
		}
		if copyCheckpoint.UpdatedAt.IsZero() {
			copyCheckpoint.UpdatedAt = copyCheckpoint.CreatedAt
		}
		s.vfsCheckpoints[copyCheckpoint.Path] = copyCheckpoint
	}
	return s.persistLocked()
}

func (s *FileStore) GetVFSCheckpoint(_ context.Context, path string) (domain.VFSCheckpointRecord, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	checkpoint, ok := s.vfsCheckpoints[strings.TrimSpace(path)]
	if !ok {
		return domain.VFSCheckpointRecord{}, false, nil
	}
	return cloneVFSCheckpoint(checkpoint), true, nil
}

func (s *FileStore) Snapshot() map[string]any {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return map[string]any{
		"storage_mode":         s.storageMode,
		"path":                 s.path,
		"workflow_count":       len(s.workflows),
		"session_count":        len(s.sessionStates),
		"invalidation_count":   len(s.invalidations),
		"vector_chunk_count":   len(s.vectorChunks),
		"rag_document_count":   len(s.ragDocuments),
		"rag_memory_count":     len(s.ragMemories),
		"route_memory_count":   len(s.routeMemories),
		"vfs_artifact_count":   len(s.vfsArtifacts),
		"vfs_checkpoint_count": len(s.vfsCheckpoints),
	}
}

func (s *FileStore) load() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	if len(data) == 0 {
		return nil
	}
	var payload filePayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return err
	}
	if payload.Workflows != nil {
		s.workflows = payload.Workflows
	}
	if payload.SessionStates != nil {
		s.sessionStates = payload.SessionStates
	}
	if payload.Invalidations != nil {
		s.invalidations = payload.Invalidations
	}
	if payload.VectorChunks != nil {
		s.vectorChunks = payload.VectorChunks
	}
	if payload.RAGDocuments != nil {
		s.ragDocuments = payload.RAGDocuments
	}
	if payload.RAGMemories != nil {
		s.ragMemories = payload.RAGMemories
	}
	if payload.RouteMemories != nil {
		s.routeMemories = payload.RouteMemories
	}
	if payload.VFSArtifacts != nil {
		s.vfsArtifacts = payload.VFSArtifacts
	}
	if payload.VFSCheckpoints != nil {
		s.vfsCheckpoints = payload.VFSCheckpoints
	}
	return nil
}

func (s *FileStore) persistLocked() error {
	payload := filePayload{
		Workflows:      s.workflows,
		SessionStates:  s.sessionStates,
		Invalidations:  s.invalidations,
		VectorChunks:   s.vectorChunks,
		RAGDocuments:   s.ragDocuments,
		RAGMemories:    s.ragMemories,
		RouteMemories:  s.routeMemories,
		VFSArtifacts:   s.vfsArtifacts,
		VFSCheckpoints: s.vfsCheckpoints,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	tmpPath := s.path + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmpPath, s.path)
}

func sessionKey(sessionID string, branch string) string {
	return sessionID + "::" + branch
}

func cloneMap(value map[string]any) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	cloned := make(map[string]any, len(value))
	for key, item := range value {
		cloned[key] = cloneAny(item)
	}
	return cloned
}

func cloneAny(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		return cloneMap(typed)
	case []any:
		out := make([]any, len(typed))
		for i, item := range typed {
			out[i] = cloneAny(item)
		}
		return out
	case []string:
		return append([]string(nil), typed...)
	case []int:
		return append([]int(nil), typed...)
	case []float64:
		return append([]float64(nil), typed...)
	default:
		return typed
	}
}

func cloneSessionState(value domain.SessionState) domain.SessionState {
	value.State = cloneMap(value.State)
	return value
}

func cloneInvalidation(value domain.InvalidationEvent) domain.InvalidationEvent {
	value.Payload = cloneMap(value.Payload)
	return value
}

func cloneVectorChunk(value domain.VectorChunk) domain.VectorChunk {
	value.Terms = append([]string(nil), value.Terms...)
	value.Embedding = append([]float64(nil), value.Embedding...)
	value.Metadata = cloneMap(value.Metadata)
	return value
}

func cloneRAGDocument(value domain.RAGDocument) domain.RAGDocument {
	value.Metadata = cloneMap(value.Metadata)
	value.ArtifactIDs = append([]string(nil), value.ArtifactIDs...)
	return value
}

func cloneRAGMemory(value domain.RAGMemoryRecord) domain.RAGMemoryRecord {
	value.Content = cloneMap(value.Content)
	value.Metadata = cloneMap(value.Metadata)
	value.Embedding = append([]float64(nil), value.Embedding...)
	return value
}

func cloneTaskSignature(value domain.TaskSignature) domain.TaskSignature {
	value.Files = append([]string(nil), value.Files...)
	value.Modules = append([]string(nil), value.Modules...)
	value.Extensions = append([]string(nil), value.Extensions...)
	value.Constraints = append([]string(nil), value.Constraints...)
	value.Embedding = append([]float64(nil), value.Embedding...)
	return value
}

func cloneRouteMemory(value domain.RouteMemoryRecord) domain.RouteMemoryRecord {
	value.TaskSignature = cloneTaskSignature(value.TaskSignature)
	value.Embedding = append([]float64(nil), value.Embedding...)
	value.Metadata = cloneMap(value.Metadata)
	return value
}

func cloneVFSArtifact(value domain.VFSArtifact) domain.VFSArtifact {
	value.Content = append([]byte(nil), value.Content...)
	value.Metadata = cloneMap(value.Metadata)
	return value
}

func cloneVFSCheckpoint(value domain.VFSCheckpointRecord) domain.VFSCheckpointRecord {
	value.Checkpoint = cloneMap(value.Checkpoint)
	value.Metadata = cloneMap(value.Metadata)
	return value
}
