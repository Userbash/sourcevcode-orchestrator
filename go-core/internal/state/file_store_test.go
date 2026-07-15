package state

import (
	"context"
	"path/filepath"
	"testing"
)

func TestFileStoreSessionStateAndInvalidations(t *testing.T) {
	store, err := NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}

	ctx := context.Background()
	stateValue := map[string]any{"session_note": "ready", "budget": 2}
	state, err := store.SaveSessionState(ctx, "session-1", "main", stateValue, "prompt-v1", "ctx-v1", nil)
	if err != nil {
		t.Fatalf("SaveSessionState() error = %v", err)
	}
	if state.Version != 1 {
		t.Fatalf("SaveSessionState() version = %d, want 1", state.Version)
	}

	loaded, ok, err := store.GetSessionState(ctx, "session-1", "main")
	if err != nil {
		t.Fatalf("GetSessionState() error = %v", err)
	}
	if !ok {
		t.Fatal("GetSessionState() ok = false, want true")
	}
	if loaded.State["session_note"] != "ready" {
		t.Fatalf("GetSessionState() session_note = %v, want ready", loaded.State["session_note"])
	}

	event, err := store.RecordInvalidation(ctx, "session-1", "main", "CACHE_GUARD_HARD_STOP", map[string]any{"task_id": "task-1"})
	if err != nil {
		t.Fatalf("RecordInvalidation() error = %v", err)
	}
	if event.Reason != "CACHE_GUARD_HARD_STOP" {
		t.Fatalf("RecordInvalidation() reason = %q", event.Reason)
	}

	events, err := store.RecentInvalidations(ctx, "session-1", "main", 10)
	if err != nil {
		t.Fatalf("RecentInvalidations() error = %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("RecentInvalidations() len = %d, want 1", len(events))
	}
	if events[0].Payload["task_id"] != "task-1" {
		t.Fatalf("RecentInvalidations() payload task_id = %v, want task-1", events[0].Payload["task_id"])
	}
}
