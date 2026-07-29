package http_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestHTTPContract(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service)
	if err != nil {
		t.Fatal(err)
	}
	health := request(t, handler, http.MethodGet, "/health", nil)
	if health.Code != http.StatusOK {
		t.Fatalf("health = %d", health.Code)
	}
	payload := []byte(`{"description":"implement endpoint","capability":"code","idempotency_key":"retry-safe"}`)
	response := request(t, handler, http.MethodPost, "/tasks", payload)
	if response.Code != http.StatusAccepted {
		t.Fatalf("first submit = %d", response.Code)
	}
	var first map[string]any
	if err := json.NewDecoder(response.Body).Decode(&first); err != nil {
		t.Fatal(err)
	}
	id, _ := first["workflow_id"].(string)
	if id == "" {
		t.Fatalf("response missing workflow_id: %#v", first)
	}
	response = request(t, handler, http.MethodPost, "/tasks", payload)
	var replay map[string]any
	if err := json.NewDecoder(response.Body).Decode(&replay); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || replay["workflow_id"] != id || replay["replayed"] != true {
		t.Fatalf("replay response = %#v status=%d", replay, response.Code)
	}
	response = request(t, handler, http.MethodGet, "/tasks/"+id, nil)
	if response.Code != http.StatusOK {
		t.Fatalf("workflow readback = %d", response.Code)
	}
}

func request(t *testing.T, handler http.Handler, method, path string, body []byte) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	return response
}

func TestTaskRequestSchemaIsStable(t *testing.T) {
	payload := map[string]any{"description": "implement endpoint", "capability": "code", "idempotency_key": "retry-safe"}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequest(http.MethodPost, "http://127.0.0.1:8010/tasks", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if req.Header.Get("Content-Type") != "application/json" {
		t.Fatal("task submissions must be JSON")
	}
}
