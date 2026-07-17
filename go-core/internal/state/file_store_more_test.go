package state

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

func TestFileStoreSaveSessionStateRejectsVersionConflict(t *testing.T) {
	store, err := NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}

	ctx := context.Background()
	if _, err := store.SaveSessionState(ctx, "session-1", "main", map[string]any{"ok": true}, "prompt-v1", "ctx-v1", nil); err != nil {
		t.Fatalf("SaveSessionState() initial error = %v", err)
	}
	expectedVersion := 0
	if _, err := store.SaveSessionState(ctx, "session-1", "main", map[string]any{"ok": false}, "prompt-v2", "ctx-v2", &expectedVersion); err == nil {
		t.Fatal("SaveSessionState() version conflict error = nil, want error")
	}
}

func TestFileStoreCollectionsIgnoreBlankIDsAndReturnClones(t *testing.T) {
	store, err := NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	ctx := context.Background()
	base := time.Date(2026, time.July, 16, 9, 0, 0, 0, time.UTC)

	vectorChunks := []domain.VectorChunk{
		{ChunkID: " ", SessionID: "session-1", Branch: "main", CreatedAt: base.Add(-time.Minute)},
		{ChunkID: "chunk-1", SessionID: "session-1", Branch: "main", Terms: []string{"go"}, Embedding: []float64{1, 2}, Metadata: map[string]any{"nested": map[string]any{"k": "v"}}, CreatedAt: base},
		{ChunkID: "chunk-2", SessionID: "session-2", Branch: "main", CreatedAt: base.Add(-2 * time.Minute)},
	}
	if err := store.UpsertVectorChunks(ctx, vectorChunks); err != nil {
		t.Fatalf("UpsertVectorChunks() error = %v", err)
	}
	vectorChunks[1].Terms[0] = "mutated"
	vectorChunks[1].Embedding[0] = 99
	vectorChunks[1].Metadata["nested"].(map[string]any)["k"] = "mutated"

	chunks, err := store.ListVectorChunks(ctx, "session-1", "main", 10)
	if err != nil {
		t.Fatalf("ListVectorChunks() error = %v", err)
	}
	if len(chunks) != 1 {
		t.Fatalf("ListVectorChunks() len = %d, want 1", len(chunks))
	}
	if chunks[0].Terms[0] != "go" || chunks[0].Embedding[0] != 1 {
		t.Fatalf("ListVectorChunks() clone lost original values = %#v", chunks[0])
	}
	if chunks[0].Metadata["nested"].(map[string]any)["k"] != "v" {
		t.Fatalf("ListVectorChunks() metadata nested value = %v, want v", chunks[0].Metadata["nested"].(map[string]any)["k"])
	}

	routeMemories := []domain.RouteMemoryRecord{
		{RouteID: "", Project: "proj", RepoFingerprint: "repo", Capability: "plan", CreatedAt: base},
		{RouteID: "route-1", Project: "proj", RepoFingerprint: "repo", Capability: "plan", Metadata: map[string]any{"score": 1}, CreatedAt: base, UpdatedAt: base},
	}
	if err := store.UpsertRouteMemories(ctx, routeMemories); err != nil {
		t.Fatalf("UpsertRouteMemories() error = %v", err)
	}
	routeMemories[1].Metadata["score"] = 2
	routes, err := store.ListRouteMemories(ctx, "proj", "repo", "plan", 10)
	if err != nil {
		t.Fatalf("ListRouteMemories() error = %v", err)
	}
	if len(routes) != 1 {
		t.Fatalf("ListRouteMemories() len = %d, want 1", len(routes))
	}
	if routes[0].Metadata["score"] != 1 {
		t.Fatalf("ListRouteMemories() metadata score = %v, want 1", routes[0].Metadata["score"])
	}

	artifacts := []domain.VFSArtifact{
		{ArtifactID: " ", Content: []byte("skip")},
		{ArtifactID: "artifact-1", Content: []byte("payload"), Metadata: map[string]any{"lang": "go"}, CreatedAt: base},
	}
	if err := store.UpsertVFSArtifacts(ctx, artifacts); err != nil {
		t.Fatalf("UpsertVFSArtifacts() error = %v", err)
	}
	artifacts[1].Content[0] = 'X'
	artifacts[1].Metadata["lang"] = "rust"
	artifact, ok, err := store.GetVFSArtifact(ctx, "artifact-1")
	if err != nil {
		t.Fatalf("GetVFSArtifact() error = %v", err)
	}
	if !ok {
		t.Fatal("GetVFSArtifact() ok = false, want true")
	}
	if string(artifact.Content) != "payload" {
		t.Fatalf("GetVFSArtifact() content = %q, want payload", string(artifact.Content))
	}
	if artifact.Metadata["lang"] != "go" {
		t.Fatalf("GetVFSArtifact() metadata lang = %v, want go", artifact.Metadata["lang"])
	}

	checkpoints := []domain.VFSCheckpointRecord{
		{Path: " ", Checkpoint: map[string]any{"skip": true}},
		{Path: "src/main.go", Checkpoint: map[string]any{"line": 17}, Metadata: map[string]any{"branch": "main"}, CreatedAt: base, UpdatedAt: base},
	}
	if err := store.UpsertVFSCheckpoints(ctx, checkpoints); err != nil {
		t.Fatalf("UpsertVFSCheckpoints() error = %v", err)
	}
	checkpoints[1].Checkpoint["line"] = 99
	checkpoints[1].Metadata["branch"] = "mutated"
	checkpoint, ok, err := store.GetVFSCheckpoint(ctx, "src/main.go")
	if err != nil {
		t.Fatalf("GetVFSCheckpoint() error = %v", err)
	}
	if !ok {
		t.Fatal("GetVFSCheckpoint() ok = false, want true")
	}
	if checkpoint.Checkpoint["line"] != 17 {
		t.Fatalf("GetVFSCheckpoint() line = %v, want 17", checkpoint.Checkpoint["line"])
	}
	if checkpoint.Metadata["branch"] != "main" {
		t.Fatalf("GetVFSCheckpoint() branch = %v, want main", checkpoint.Metadata["branch"])
	}
}

func TestFileStoreSnapshotCountsPersistedCollections(t *testing.T) {
	store, err := NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	ctx := context.Background()
	base := time.Date(2026, time.July, 16, 9, 30, 0, 0, time.UTC)

	if _, err := store.SaveSessionState(ctx, "session-1", "main", map[string]any{"ready": true}, "prompt", "context", nil); err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}
	if _, err := store.RecordInvalidation(ctx, "session-1", "main", "cache_miss", map[string]any{"task": "t1"}); err != nil {
		t.Fatalf("RecordInvalidation() error = %v", err)
	}
	if err := store.UpsertVectorChunks(ctx, []domain.VectorChunk{{ChunkID: "chunk-1", CreatedAt: base}}); err != nil {
		t.Fatalf("UpsertVectorChunks() error = %v", err)
	}
	if err := store.UpsertRAGDocuments(ctx, []domain.RAGDocument{{DocumentID: "doc-1", CreatedAt: base, UpdatedAt: base}}); err != nil {
		t.Fatalf("UpsertRAGDocuments() error = %v", err)
	}
	if err := store.UpsertRAGMemories(ctx, []domain.RAGMemoryRecord{{MemoryID: "mem-1", CreatedAt: base, UpdatedAt: base}}); err != nil {
		t.Fatalf("UpsertRAGMemories() error = %v", err)
	}
	if err := store.UpsertRouteMemories(ctx, []domain.RouteMemoryRecord{{RouteID: "route-1", CreatedAt: base, UpdatedAt: base}}); err != nil {
		t.Fatalf("UpsertRouteMemories() error = %v", err)
	}
	if err := store.UpsertVFSArtifacts(ctx, []domain.VFSArtifact{{ArtifactID: "artifact-1", CreatedAt: base}}); err != nil {
		t.Fatalf("UpsertVFSArtifacts() error = %v", err)
	}
	if err := store.UpsertVFSCheckpoints(ctx, []domain.VFSCheckpointRecord{{Path: "a.go", Checkpoint: map[string]any{"ok": true}, CreatedAt: base, UpdatedAt: base}}); err != nil {
		t.Fatalf("UpsertVFSCheckpoints() error = %v", err)
	}

	snapshot := store.Snapshot()
	assertSnapshotCount(t, snapshot, "session_count", 1)
	assertSnapshotCount(t, snapshot, "invalidation_count", 1)
	assertSnapshotCount(t, snapshot, "vector_chunk_count", 1)
	assertSnapshotCount(t, snapshot, "rag_document_count", 1)
	assertSnapshotCount(t, snapshot, "rag_memory_count", 1)
	assertSnapshotCount(t, snapshot, "route_memory_count", 1)
	assertSnapshotCount(t, snapshot, "vfs_artifact_count", 1)
	assertSnapshotCount(t, snapshot, "vfs_checkpoint_count", 1)
}

func assertSnapshotCount(t *testing.T, snapshot map[string]any, key string, want int) {
	t.Helper()
	got, ok := snapshot[key].(int)
	if !ok {
		t.Fatalf("Snapshot()[%q] type = %T, want int", key, snapshot[key])
	}
	if got != want {
		t.Fatalf("Snapshot()[%q] = %d, want %d", key, got, want)
	}
}
