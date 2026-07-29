package orchestrator_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestHTTPHandlerReturnsStableErrorsAndWorkflowReadback(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, method, path, body, wantError string
		wantStatus                          int
	}{
		{"invalid json", http.MethodPost, "/tasks", "{", "invalid JSON", http.StatusBadRequest},
		{"invalid task", http.MethodPost, "/tasks", `{"idempotency_key":"key","description":" "}`, "description is required", http.StatusBadRequest},
		{"missing workflow", http.MethodGet, "/tasks/missing", "", "not found", http.StatusNotFound},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(test.method, test.path, bytes.NewBufferString(test.body))
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, req)
			if response.Code != test.wantStatus {
				t.Fatalf("status=%d want=%d", response.Code, test.wantStatus)
			}
			if response.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("content type=%q", response.Header().Get("Content-Type"))
			}
			var body map[string]string
			if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if body["error"] != test.wantError {
				t.Fatalf("body=%#v", body)
			}
		})
	}
}

func TestStoreSaveAndLookupsPreserveWorkflow(t *testing.T) {
	store := orchestrator.NewMemoryStore()
	store.Save(nil)
	if store.Count() != 0 {
		t.Fatal("nil workflow was stored")
	}
	workflow := orchestrator.NewWorkflow("task-1", "key-1")
	store.Save(workflow)
	byKey, byID := store.ByKey("key-1"), store.Get("task-1")
	if byKey == nil || byID == nil || byKey.TaskID != workflow.TaskID || byID.IdempotencyKey != workflow.IdempotencyKey {
		t.Fatal("saved workflow was not retrievable")
	}
	if store.ByKey("missing") != nil || store.Get("missing") != nil {
		t.Fatal("missing workflow unexpectedly found")
	}
}

func TestDecisionAndStateErrorsLeaveWorkflowUnchanged(t *testing.T) {
	w := orchestrator.NewWorkflow("task", "key")
	if err := w.Fallback(orchestrator.Selection{Provider: "provider"}, "bad"); err == nil {
		t.Fatal("incomplete fallback accepted")
	}
	if w.Selection() != (orchestrator.Selection{}) || len(w.Events()) != 0 {
		t.Fatalf("invalid fallback changed workflow: %#v %#v", w.Selection(), w.Events())
	}
	if err := w.Transition(orchestrator.WorkflowQueued); err == nil || w.Status() != orchestrator.WorkflowQueued {
		t.Fatal("invalid transition changed workflow")
	}
}

func TestSelectorAndRouterRejectUnavailableCandidates(t *testing.T) {
	selector := orchestrator.NewSelector()
	if _, err := selector.Select(context.Background(), orchestrator.SelectionRequest{Capability: "code"}, nil); err == nil {
		t.Fatal("empty candidate set selected a model")
	}
	router, err := orchestrator.NewRouter(orchestrator.NewInMemoryRegistry())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := router.Route(orchestrator.RouteRequest{Capability: "code", Selection: orchestrator.Selection{Provider: "p", Model: "m"}}); err == nil {
		t.Fatal("empty registry routed an agent")
	}
}

func TestSchedulerRejectsDuplicateStepIDsAndUnknownResume(t *testing.T) {
	scheduler, err := orchestrator.NewScheduler(orchestrator.NewRecordingExecutor(nil), orchestrator.SchedulerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := scheduler.Run(context.Background(), orchestrator.Plan{Steps: []orchestrator.Step{{ID: "same"}, {ID: "same"}}}); err == nil {
		t.Fatal("duplicate ids accepted")
	}
	if _, err := scheduler.Resume(context.Background(), "missing"); err == nil {
		t.Fatal("unknown resume accepted")
	}
}
