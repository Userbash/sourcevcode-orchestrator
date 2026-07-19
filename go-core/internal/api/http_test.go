package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/app"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
	"sourcevcode-orchestrator/go-core/internal/state"
	"sourcevcode-orchestrator/go-core/internal/transport"
)

func anyString(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case float64:
		return fmt.Sprintf("%g", typed)
	default:
		return ""
	}
}

func newTestServer(t *testing.T) *Server {
	t.Helper()
	var (
		mu        sync.Mutex
		residents = map[string]bool{}
	)
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/models":
			_, _ = w.Write([]byte(`{"data":[{"id":"test-model"}]}`))
		case "/v1/chat/completions":
			_, _ = w.Write([]byte(`{"id":"test-completion","choices":[{"message":{"role":"assistant","content":"completed by Go provider"},"finish_reason":"stop"}]}`))
		case "/api/tags":
			_, _ = w.Write([]byte(`{"models":[{"name":"test-model","size":1073741824}]}`))
		case "/api/ps":
			mu.Lock()
			models := make([]map[string]any, 0, len(residents))
			for name := range residents {
				models = append(models, map[string]any{"name": name, "size": float64(1073741824), "size_vram": float64(1073741824), "expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)})
			}
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"models": models})
		case "/api/pull":
			_, _ = w.Write([]byte(`{"status":"success"}`))
		case "/api/generate":
			body, _ := io.ReadAll(r.Body)
			defer r.Body.Close()
			var payload map[string]any
			_ = json.Unmarshal(body, &payload)
			modelName, _ := payload["model"].(string)
			keepAlive := strings.TrimSpace("" + anyString(payload["keep_alive"]))
			mu.Lock()
			if keepAlive == "0" {
				delete(residents, modelName)
			} else {
				residents[modelName] = true
			}
			mu.Unlock()
			_, _ = w.Write([]byte(`{"response":"ok","done":true}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(provider.Close)
	t.Setenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT", provider.URL)
	t.Setenv("AI_BRIDGE_LOCAL_LLM_MODEL", "test-model")
	t.Setenv("GO_CORE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("AI_BRIDGE_MESSAGE_BUS_BACKEND", "memory")
	t.Setenv("AI_BRIDGE_RABBITMQ_URL", "")
	t.Setenv("GO_CORE_AGENT_WORKERS", "0")
	t.Setenv("AI_KERNEL_API_KEY", "")
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("CODEX_SALE_API_KEY", "")
	t.Setenv("MISTRAL_API_KEY", "")
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	return NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
}

func TestRequiredDaemonRoutesAreRegistered(t *testing.T) {
	server := newTestServer(t)
	statusByPath := map[string]int{
		"/health":                      http.StatusOK,
		"/health/full":                 http.StatusOK,
		"/api/health":                  http.StatusOK,
		"/chat/ws":                     http.StatusBadRequest,
		"/providers/inventory":         http.StatusOK,
		"/providers/runtime_inventory": http.StatusOK,
		"/providers/models/index":      http.StatusOK,
		"/providers/ai_kernel/gate":    http.StatusServiceUnavailable,
		"/runtime/routing_weights":     http.StatusOK,
		"/health/local_models":         http.StatusOK,
		"/sourcecraft":                 http.StatusOK,
		"/diagnostics":                 http.StatusOK,
	}
	for path, expectedStatus := range statusByPath {
		t.Run(path, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, path, nil)
			response := httptest.NewRecorder()
			server.Handler().ServeHTTP(response, request)
			if response.Code != expectedStatus {
				t.Fatalf("%s: expected %d, got %d: %s", path, expectedStatus, response.Code, response.Body.String())
			}
		})
	}
}

func TestProviderInventoryExposesClaudeResourcePoolForCodexSale(t *testing.T) {
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/models":
			_, _ = w.Write([]byte(`{"data":[{"id":"claude-sonnet-4-6"}]}`))
		case "/v1/chat/completions":
			_, _ = w.Write([]byte(`{"id":"test-completion","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer provider.Close()

	t.Setenv("AI_BRIDGE_MODEL_VALIDATE_MODELS", "false")
	t.Setenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT", "")
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("CODEX_SALE_PROVIDER_ID", "codexsale")
	t.Setenv("CODEX_SALE_BASE_URL", provider.URL+"/v1")
	t.Setenv("CODEX_SALE_MODELS_ENDPOINT", provider.URL+"/v1/models")
	t.Setenv("CODEX_SALE_API_KEY", "secret")
	t.Setenv("CODEX_SALE_MODEL", "claude-sonnet-4-6")
	t.Setenv("AI_KERNEL_API_KEY", "")
	t.Setenv("MISTRAL_API_KEY", "")

	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)

	request := httptest.NewRequest(http.MethodGet, "/providers/inventory", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected ok, got %d: %s", response.Code, response.Body.String())
	}

	var payload struct {
		Data map[string]map[string]any `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	codexInventory := payload.Data["codexsale"]
	if codexInventory == nil {
		t.Fatalf("codexsale inventory missing: %#v", payload.Data)
	}
	rawPools, ok := codexInventory["resource_pools"].([]any)
	if !ok {
		t.Fatalf("resource_pools missing: %#v", codexInventory)
	}
	for _, item := range rawPools {
		pool, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if pool["pool"] != "claude" {
			continue
		}
		if pool["eligible"] != true {
			t.Fatalf("claude pool should be eligible: %#v", pool)
		}
		eligibleModels, ok := pool["eligible_models"].([]any)
		if !ok || len(eligibleModels) != 1 || eligibleModels[0] != "claude-sonnet-4-6" {
			t.Fatalf("unexpected eligible models: %#v", pool)
		}
		return
	}
	t.Fatalf("claude pool not found: %#v", rawPools)
}

func TestRuntimeRealtimeMetricsAggregatesByProviderModel(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
	workflow := domain.WorkflowRecord{
		Task:       domain.Task{ID: "metrics-task-1", Type: domain.TaskTypeCode},
		Acceptance: domain.TaskAcceptance{Provider: "local", ModelName: "test-model"},
		Result: &domain.AgentResult{
			TaskID:    "metrics-task-1",
			Provider:  "local",
			ModelName: "test-model",
			Output: domain.ResultOutput{
				Artifacts: map[string]any{
					"realtime_metrics": map[string]any{
						"transport":               "sse",
						"native_streaming":        true,
						"pseudo_realtime":         false,
						"time_to_first_token_ms":  12,
						"time_to_first_tool_ms":   28,
						"time_to_first_patch_ms":  36,
						"time_to_first_result_ms": 44,
						"time_to_first_test_ms":   52,
						"total_completion_ms":     140,
						"tokens_streamed":         64,
						"tools_executed":          2,
						"patches_applied":         1,
						"tests_executed":          1,
					},
				},
			},
		},
		UpdatedAt: time.Now().UTC(),
	}
	if err := store.SaveWorkflow(context.Background(), workflow); err != nil {
		t.Fatalf("SaveWorkflow: %v", err)
	}

	request := httptest.NewRequest(http.MethodGet, "/runtime/realtime_metrics", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", response.Code, response.Body.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if payload["status"] != "ok" {
		t.Fatalf("expected ok status, got %v", payload["status"])
	}

	data, ok := payload["data"].(map[string]any)
	if !ok {
		t.Fatalf("expected data object, got %T", payload["data"])
	}
	totals, ok := data["totals"].(map[string]any)
	if !ok {
		t.Fatalf("expected totals object, got %T", data["totals"])
	}
	if totals["samples_collected"].(float64) < 1 {
		t.Fatalf("expected collected samples, got %v", totals["samples_collected"])
	}

	providers, ok := data["providers"].(map[string]any)
	if !ok || len(providers) == 0 {
		t.Fatalf("expected providers payload, got %T", data["providers"])
	}
	localProvider, ok := providers["local"].(map[string]any)
	if !ok {
		t.Fatalf("expected local provider metrics, got %#v", providers)
	}
	models, ok := localProvider["models"].(map[string]any)
	if !ok || len(models) == 0 {
		t.Fatalf("expected local models payload, got %T", localProvider["models"])
	}

	modelSummary, ok := models["test-model"].(map[string]any)
	if !ok {
		t.Fatalf("expected test-model metrics, got %#v", models)
	}
	if modelSummary["sample_count"].(float64) != 1 {
		t.Fatalf("expected sample_count=1, got %v", modelSummary["sample_count"])
	}
	if modelSummary["avg_total_completion_ms"].(float64) != 140 {
		t.Fatalf("expected avg_total_completion_ms=140, got %v", modelSummary["avg_total_completion_ms"])
	}
	if modelSummary["avg_time_to_first_token_ms"].(float64) != 12 {
		t.Fatalf("expected avg_time_to_first_token_ms=12, got %v", modelSummary["avg_time_to_first_token_ms"])
	}
}

func TestSourcecraftDelegateUsesGoOrchestrator(t *testing.T) {
	server := newTestServer(t)
	body := bytes.NewBufferString(`{"description":"implement native Go transport","project":"migration","type":"code"}`)
	request := httptest.NewRequest(http.MethodPost, "/sourcecraft/delegate", body)
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusAccepted {
		t.Fatalf("expected accepted, got %d: %s", response.Code, response.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	workflow, ok := payload["workflow"].(map[string]any)
	if !ok {
		t.Fatalf("workflow missing: %#v", payload)
	}
	result, ok := workflow["result"].(map[string]any)
	if !ok || (result["status"] != "done" && result["status"] != "completed") {
		t.Fatalf("Go workflow did not complete: %#v", workflow)
	}
	if got := payload["answered_for"]; got != "user" {
		t.Fatalf("answered_for = %v, want user", got)
	}
	if got := payload["request_origin"]; got != "sourcecraft_http" {
		t.Fatalf("request_origin = %v, want sourcecraft_http", got)
	}
	if got := payload["client_kind"]; got != "http_client" {
		t.Fatalf("client_kind = %v, want http_client", got)
	}
	responseOrigin, ok := payload["response_origin"].(map[string]any)
	if !ok {
		t.Fatalf("response_origin missing: %#v", payload)
	}
	if got := responseOrigin["answered_for"]; got != "user" {
		t.Fatalf("response_origin.answered_for = %v, want user", got)
	}
	if got := responseOrigin["request_origin"]; got != "sourcecraft_http" {
		t.Fatalf("response_origin.request_origin = %v, want sourcecraft_http", got)
	}
	if got := responseOrigin["client_kind"]; got != "external_chat" {
		t.Fatalf("response_origin.client_kind = %v, want external_chat", got)
	}
}

func TestChatSubmitDispatchesThroughOrchestrator(t *testing.T) {
	server := newTestServer(t)

	request := transport.Envelope{
		Type:      "command",
		RequestID: "chat-1",
		Action:    "chat.submit",
		Ack:       true,
		Data: map[string]any{
			"description": "ping orchestrator",
			"type":        "code",
			"project":     "default",
		},
	}

	ctx := context.WithValue(context.Background(), requestContextKey{}, requestMetadata{
		Transport:     "websocket",
		RequestOrigin: "chat_ws",
		ClientKind:    "external_chat",
		AnsweredFor:   "user",
	})

	var frames []transport.Envelope
	if err := server.dispatcher.Dispatch(ctx, request, func(frame transport.Envelope) error {
		frames = append(frames, frame)
		return nil
	}); err != nil {
		t.Fatalf("dispatch failed: %v", err)
	}

	if len(frames) != 2 {
		t.Fatalf("expected 2 websocket frames, got %d", len(frames))
	}
	if frames[0].Type != "ack" {
		t.Fatalf("expected first frame to be ack, got %q", frames[0].Type)
	}
	if frames[1].Type != "response" {
		t.Fatalf("expected second frame to be response, got %q", frames[1].Type)
	}
	if frames[1].Action != "chat.submit" {
		t.Fatalf("expected response action chat.submit, got %q", frames[1].Action)
	}

	data := frames[1].Data
	responseOrigin, ok := data["response_origin"].(map[string]any)
	if !ok {
		t.Fatalf("expected response_origin map, got %T", data["response_origin"])
	}
	if responseOrigin["request_origin"] != "chat_ws" {
		t.Fatalf("expected request_origin chat_ws, got %v", responseOrigin["request_origin"])
	}
	if responseOrigin["client_kind"] != "external_chat" {
		t.Fatalf("expected client_kind external_chat, got %v", responseOrigin["client_kind"])
	}
	if responseOrigin["answered_for"] != "user" {
		t.Fatalf("expected answered_for user, got %v", responseOrigin["answered_for"])
	}
}

func TestRouteProfilesKeepMetadataAndMigrationActionsAligned(t *testing.T) {
	cases := []struct {
		name       string
		method     string
		path       string
		wantMeta   requestMetadata
		wantAction string
	}{
		{
			name:       "chat websocket",
			method:     http.MethodGet,
			path:       "/chat/ws",
			wantMeta:   requestMetadata{Transport: "websocket", RequestOrigin: "chat_ws", ClientKind: "external_chat", AnsweredFor: "user"},
			wantAction: "",
		},
		{
			name:       "sourcecraft delegate",
			method:     http.MethodPost,
			path:       "/sourcecraft/delegate",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "sourcecraft_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "sourcecraft.delegate.get",
		},
		{
			name:       "sourcecraft parallel delegate",
			method:     http.MethodPost,
			path:       "/sourcecraft/parallel_delegate",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "sourcecraft_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "sourcecraft.parallel_delegate.get",
		},
		{
			name:       "tasks preview",
			method:     http.MethodPost,
			path:       "/tasks/preview_plan",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "tasks_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "tasks.plan.preview",
		},
		{
			name:       "tasks run",
			method:     http.MethodPost,
			path:       "/tasks/run_plan",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "tasks_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "tasks.plan.run",
		},
		{
			name:       "tasks checkpoint",
			method:     http.MethodGet,
			path:       "/tasks/workflow-1/checkpoint",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "tasks_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "tasks.plan.checkpoint.get",
		},
		{
			name:       "tasks resume",
			method:     http.MethodPost,
			path:       "/tasks/workflow-1/resume_plan",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "tasks_http", ClientKind: "http_client", AnsweredFor: "user"},
			wantAction: "tasks.plan.resume",
		},
		{
			name:       "health",
			method:     http.MethodGet,
			path:       "/health",
			wantMeta:   requestMetadata{Transport: "http", RequestOrigin: "health_http", ClientKind: "http_client", AnsweredFor: "observer"},
			wantAction: "",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			request := httptest.NewRequest(tc.method, tc.path, nil)
			transport, origin, clientKind, answeredFor := inferRequestMetadata(request)
			if transport != tc.wantMeta.Transport || origin != tc.wantMeta.RequestOrigin || clientKind != tc.wantMeta.ClientKind || answeredFor != tc.wantMeta.AnsweredFor {
				t.Fatalf("metadata = {%s %s %s %s}, want {%s %s %s %s}", transport, origin, clientKind, answeredFor, tc.wantMeta.Transport, tc.wantMeta.RequestOrigin, tc.wantMeta.ClientKind, tc.wantMeta.AnsweredFor)
			}
			if got := migrationActionForPath(request); got != tc.wantAction {
				t.Fatalf("migrationActionForPath() = %q, want %q", got, tc.wantAction)
			}
		})
	}
}

func TestSourcecraftStatusAdvertisesPlanningOnlyRuntime(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/sourcecraft", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("expected ok, got %d: %s", response.Code, response.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if got := payload["runtime_mode"]; got != "semantic-routing" {
		t.Fatalf("runtime_mode = %v, want semantic-routing", got)
	}
	if got := payload["mutation_supported"]; got != false {
		t.Fatalf("mutation_supported = %v, want false", got)
	}
	families, ok := payload["task_families"].([]any)
	if !ok || len(families) == 0 {
		t.Fatalf("task_families = %#v, want non-empty list", payload["task_families"])
	}
	actions, ok := payload["safe_actions"].([]any)
	if !ok || len(actions) == 0 {
		t.Fatalf("safe_actions = %#v, want non-empty list", payload["safe_actions"])
	}
}

func TestDiagnosticsHasNoMissingRequiredRoutes(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/diagnostics?matrix_only=true", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	var payload struct {
		Matrix []struct {
			Route  string `json:"route"`
			Status string `json:"status"`
		} `json:"matrix"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode diagnostics: %v", err)
	}
	for _, route := range payload.Matrix {
		if route.Status == "missing" {
			t.Fatalf("required route reported missing: %s", route.Route)
		}
	}
}

func TestDeliveryMailboxLifecycleUsesGoRuntime(t *testing.T) {
	server := newTestServer(t)
	envelope := `{"protocol_version":"1.0","task_id":"delivery-1","source_agent":"orchestrator","target_agent":"coder-local","max_retries":1,"payload":{"objective":"Implement mailbox delivery","input_data":{"prompt":"test"}}}`
	dispatchRequest := httptest.NewRequest(http.MethodPost, "/delivery/dispatch", bytes.NewBufferString(envelope))
	dispatchRequest.Header.Set("Content-Type", "application/json")
	dispatchResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(dispatchResponse, dispatchRequest)
	if dispatchResponse.Code != http.StatusAccepted {
		t.Fatalf("dispatch failed: %d %s", dispatchResponse.Code, dispatchResponse.Body.String())
	}

	fetchRequest := httptest.NewRequest(http.MethodPost, "/mailboxes/coder-local/fetch", bytes.NewBufferString(`{"limit":1}`))
	fetchRequest.Header.Set("Content-Type", "application/json")
	fetchResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(fetchResponse, fetchRequest)
	if fetchResponse.Code != http.StatusOK {
		t.Fatalf("mailbox fetch failed: %d %s", fetchResponse.Code, fetchResponse.Body.String())
	}
	if !strings.Contains(fetchResponse.Body.String(), "delivery-1") {
		t.Fatalf("fetched mailbox missing envelope: %s", fetchResponse.Body.String())
	}

	confirmRequest := httptest.NewRequest(http.MethodPost, "/delivery/confirm_payload", bytes.NewBufferString(`{"task_id":"delivery-1","agent_id":"coder-local","envelope":`+envelope+`}`))
	confirmRequest.Header.Set("Content-Type", "application/json")
	confirmResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(confirmResponse, confirmRequest)
	if confirmResponse.Code != http.StatusOK || !strings.Contains(confirmResponse.Body.String(), `"confirmed":true`) {
		t.Fatalf("confirm payload failed: %d %s", confirmResponse.Code, confirmResponse.Body.String())
	}

	establishRequest := httptest.NewRequest(http.MethodPost, "/delivery/establish", bytes.NewBufferString(`{"task_id":"delivery-1","agent_id":"coder-local"}`))
	establishRequest.Header.Set("Content-Type", "application/json")
	establishResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(establishResponse, establishRequest)
	if establishResponse.Code != http.StatusOK {
		t.Fatalf("establish failed: %d %s", establishResponse.Code, establishResponse.Body.String())
	}

	ackRequest := httptest.NewRequest(http.MethodPost, "/delivery/ack", bytes.NewBufferString(`{"task_id":"delivery-1","status":"accepted","received_by":"coder-local"}`))
	ackRequest.Header.Set("Content-Type", "application/json")
	ackResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(ackResponse, ackRequest)
	if ackResponse.Code != http.StatusOK {
		t.Fatalf("ack failed: %d %s", ackResponse.Code, ackResponse.Body.String())
	}

	snapshotRequest := httptest.NewRequest(http.MethodGet, "/delivery/delivery-1", nil)
	snapshotResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(snapshotResponse, snapshotRequest)
	if snapshotResponse.Code != http.StatusOK || !strings.Contains(snapshotResponse.Body.String(), `"status":"accepted"`) {
		t.Fatalf("snapshot failed: %d %s", snapshotResponse.Code, snapshotResponse.Body.String())
	}

	healthRequest := httptest.NewRequest(http.MethodGet, "/delivery/health", nil)
	healthResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(healthResponse, healthRequest)
	if healthResponse.Code != http.StatusOK || !strings.Contains(healthResponse.Body.String(), `"accepted":1`) {
		t.Fatalf("delivery health failed: %d %s", healthResponse.Code, healthResponse.Body.String())
	}
}

func TestExecutionPlanPreviewAndCheckpointUseGoRuntime(t *testing.T) {
	server := newTestServer(t)
	previewBody := bytes.NewBufferString(`{"session_id":"plan-session","type":"code","description":"Split planner work across files","files":["a.go","b.go","c.go"],"acceptance_criteria":["plan","checkpoint"],"project":"migration","repo_path":".","checkpoint_policy":"branch","review_depth":2,"required_capability":"code"}`)
	previewRequest := httptest.NewRequest(http.MethodPost, "/tasks/preview_plan", previewBody)
	previewRequest.Header.Set("Content-Type", "application/json")
	previewResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(previewResponse, previewRequest)
	if previewResponse.Code != http.StatusOK {
		t.Fatalf("preview failed: %d %s", previewResponse.Code, previewResponse.Body.String())
	}
	if previewResponse.Header().Get("X-Control-WS-Action") != "tasks.plan.preview" {
		t.Fatalf("preview compatibility header missing: %#v", previewResponse.Header())
	}

	var previewPayload map[string]any
	if err := json.Unmarshal(previewResponse.Body.Bytes(), &previewPayload); err != nil {
		t.Fatalf("decode preview response: %v", err)
	}
	data, ok := previewPayload["data"].(map[string]any)
	if !ok {
		t.Fatalf("preview payload missing data: %#v", previewPayload)
	}
	task, ok := data["task"].(map[string]any)
	if !ok {
		t.Fatalf("preview payload missing task: %#v", data)
	}
	rootTaskID, _ := task["id"].(string)
	if strings.TrimSpace(rootTaskID) == "" {
		t.Fatalf("preview returned empty task id: %#v", task)
	}
	checkpointBranch, _ := data["checkpoint_branch"].(string)
	if !strings.HasPrefix(checkpointBranch, "parallel_plan:") {
		t.Fatalf("unexpected checkpoint branch: %#v", data)
	}

	checkpointRequest := httptest.NewRequest(http.MethodGet, "/tasks/"+rootTaskID+"/checkpoint?session_id=plan-session", nil)
	checkpointResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(checkpointResponse, checkpointRequest)
	if checkpointResponse.Code != http.StatusOK {
		t.Fatalf("checkpoint fetch failed: %d %s", checkpointResponse.Code, checkpointResponse.Body.String())
	}
	if checkpointResponse.Header().Get("X-Control-WS-Action") != "tasks.plan.checkpoint.get" {
		t.Fatalf("checkpoint compatibility header missing: %#v", checkpointResponse.Header())
	}

	var checkpointPayload map[string]any
	if err := json.Unmarshal(checkpointResponse.Body.Bytes(), &checkpointPayload); err != nil {
		t.Fatalf("decode checkpoint response: %v", err)
	}
	checkpointData, ok := checkpointPayload["data"].(map[string]any)
	if !ok {
		t.Fatalf("checkpoint payload missing data: %#v", checkpointPayload)
	}
	if checkpointData["root_task_id"] != rootTaskID {
		t.Fatalf("checkpoint root task mismatch: %#v", checkpointData)
	}
	if checkpointData["session_id"] != "plan-session" {
		t.Fatalf("checkpoint session mismatch: %#v", checkpointData)
	}
	if checkpointData["status"] != "planned" {
		t.Fatalf("checkpoint status mismatch: %#v", checkpointData)
	}
	pending, ok := checkpointData["pending_task_ids"].([]any)
	if !ok || len(pending) == 0 {
		t.Fatalf("checkpoint pending tasks missing: %#v", checkpointData)
	}
}

func TestExecutionPlanRunUsesGoRuntime(t *testing.T) {
	server := newTestServer(t)
	runBody := bytes.NewBufferString(`{"session_id":"run-plan-session","type":"code","description":"Execute planner work across files","files":["a.go","b.go","c.go"],"acceptance_criteria":["run","checkpoint"],"project":"migration","repo_path":".","checkpoint_policy":"branch","review_depth":2,"required_capability":"code"}`)
	runRequest := httptest.NewRequest(http.MethodPost, "/tasks/run_plan", runBody)
	runRequest.Header.Set("Content-Type", "application/json")
	runResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(runResponse, runRequest)
	if runResponse.Code != http.StatusOK {
		t.Fatalf("run plan failed: %d %s", runResponse.Code, runResponse.Body.String())
	}
	if runResponse.Header().Get("X-Control-WS-Action") != "tasks.plan.run" {
		t.Fatalf("run compatibility header missing: %#v", runResponse.Header())
	}

	var runPayload map[string]any
	if err := json.Unmarshal(runResponse.Body.Bytes(), &runPayload); err != nil {
		t.Fatalf("decode run response: %v", err)
	}
	data, ok := runPayload["data"].(map[string]any)
	if !ok {
		t.Fatalf("run payload missing data: %#v", runPayload)
	}
	task, ok := data["task"].(map[string]any)
	if !ok {
		t.Fatalf("run payload missing task: %#v", data)
	}
	rootTaskID, _ := task["id"].(string)
	if strings.TrimSpace(rootTaskID) == "" {
		t.Fatalf("run returned empty task id: %#v", task)
	}
	checkpoint, ok := data["checkpoint"].(map[string]any)
	if !ok {
		t.Fatalf("run payload missing checkpoint: %#v", data)
	}
	if checkpoint["status"] != "completed" {
		t.Fatalf("checkpoint not completed: %#v", checkpoint)
	}
	if pending, ok := checkpoint["pending_task_ids"].([]any); !ok || len(pending) != 0 {
		t.Fatalf("checkpoint still has pending tasks: %#v", checkpoint)
	}
	if completed, ok := checkpoint["completed_task_ids"].([]any); !ok || len(completed) == 0 {
		t.Fatalf("checkpoint missing completed tasks: %#v", checkpoint)
	}
	if workflows, ok := data["workflows"].([]any); !ok || len(workflows) == 0 {
		t.Fatalf("run payload missing workflows: %#v", data)
	}

	checkpointRequest := httptest.NewRequest(http.MethodGet, "/tasks/"+rootTaskID+"/checkpoint?session_id=run-plan-session", nil)
	checkpointResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(checkpointResponse, checkpointRequest)
	if checkpointResponse.Code != http.StatusOK {
		t.Fatalf("checkpoint fetch after run failed: %d %s", checkpointResponse.Code, checkpointResponse.Body.String())
	}
	var checkpointPayload map[string]any
	if err := json.Unmarshal(checkpointResponse.Body.Bytes(), &checkpointPayload); err != nil {
		t.Fatalf("decode checkpoint after run: %v", err)
	}
	checkpointData, ok := checkpointPayload["data"].(map[string]any)
	if !ok || checkpointData["status"] != "completed" {
		t.Fatalf("persisted checkpoint mismatch: %#v", checkpointPayload)
	}
}

func TestRuntimeRecoveryLifecycleUsesGoRuntime(t *testing.T) {
	server := newTestServer(t)

	weightsRequest := httptest.NewRequest(http.MethodGet, "/runtime/routing_weights", nil)
	weightsResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(weightsResponse, weightsRequest)
	if weightsResponse.Code != http.StatusOK || !strings.Contains(weightsResponse.Body.String(), "coder-local") {
		t.Fatalf("routing weights failed: %d %s", weightsResponse.Code, weightsResponse.Body.String())
	}

	providerProbeRequest := httptest.NewRequest(http.MethodGet, "/runtime/providers/local/probe", nil)
	providerProbeResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(providerProbeResponse, providerProbeRequest)
	if providerProbeResponse.Code != http.StatusOK || !strings.Contains(providerProbeResponse.Body.String(), `"provider":"local"`) {
		t.Fatalf("provider probe failed: %d %s", providerProbeResponse.Code, providerProbeResponse.Body.String())
	}

	suppressRequest := httptest.NewRequest(http.MethodPost, "/runtime/agents/coder-local/suppress", bytes.NewBufferString(`{"reason":"test suppression","seconds":60}`))
	suppressRequest.Header.Set("Content-Type", "application/json")
	suppressResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(suppressResponse, suppressRequest)
	if suppressResponse.Code != http.StatusOK || !strings.Contains(suppressResponse.Body.String(), `"status":"maintenance"`) {
		t.Fatalf("suppress failed: %d %s", suppressResponse.Code, suppressResponse.Body.String())
	}

	agentProbeRequest := httptest.NewRequest(http.MethodGet, "/runtime/agents/coder-local/probe", nil)
	agentProbeResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(agentProbeResponse, agentProbeRequest)
	if agentProbeResponse.Code != http.StatusOK || !strings.Contains(agentProbeResponse.Body.String(), `"agent_id":"coder-local"`) {
		t.Fatalf("agent probe failed: %d %s", agentProbeResponse.Code, agentProbeResponse.Body.String())
	}

	recoverRequest := httptest.NewRequest(http.MethodPost, "/runtime/agents/coder-local/recover", bytes.NewBufferString(`{}`))
	recoverRequest.Header.Set("Content-Type", "application/json")
	recoverResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(recoverResponse, recoverRequest)
	if recoverResponse.Code != http.StatusOK || !strings.Contains(recoverResponse.Body.String(), `"status":"ready"`) {
		t.Fatalf("recover failed: %d %s", recoverResponse.Code, recoverResponse.Body.String())
	}
}

func TestLocalLLMResidentsAndLifecycleUseGoRuntime(t *testing.T) {
	server := newTestServer(t)
	warmRequest := httptest.NewRequest(http.MethodPost, "/providers/local_llm/warm", bytes.NewBufferString(`{"model_name":"test-model"}`))
	warmRequest.Header.Set("Content-Type", "application/json")
	warmResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(warmResponse, warmRequest)
	if warmResponse.Code != http.StatusOK {
		t.Fatalf("warm failed: %d %s", warmResponse.Code, warmResponse.Body.String())
	}

	residentsRequest := httptest.NewRequest(http.MethodGet, "/providers/local_llm/residents", nil)
	residentsResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(residentsResponse, residentsRequest)
	if residentsResponse.Code != http.StatusOK {
		t.Fatalf("residents failed: %d %s", residentsResponse.Code, residentsResponse.Body.String())
	}
	if !strings.Contains(residentsResponse.Body.String(), "test-model") {
		t.Fatalf("resident model missing: %s", residentsResponse.Body.String())
	}

	disconnectRequest := httptest.NewRequest(http.MethodPost, "/providers/local_llm/disconnect", bytes.NewBufferString(`{"model_name":"test-model"}`))
	disconnectRequest.Header.Set("Content-Type", "application/json")
	disconnectResponse := httptest.NewRecorder()
	server.Handler().ServeHTTP(disconnectResponse, disconnectRequest)
	if disconnectResponse.Code != http.StatusOK {
		t.Fatalf("disconnect failed: %d %s", disconnectResponse.Code, disconnectResponse.Body.String())
	}
}

func TestHealthFullIncludesRequestContextMarkers(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/health/full", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("expected ok, got %d: %s", response.Code, response.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if got := payload["request_origin"]; got != "health_http" {
		t.Fatalf("request_origin = %v, want health_http", got)
	}
	if got := payload["client_kind"]; got != "http_client" {
		t.Fatalf("client_kind = %v, want http_client", got)
	}
	if got := payload["answered_for"]; got != "observer" {
		t.Fatalf("answered_for = %v, want observer", got)
	}
	requestContext, ok := payload["request_context"].(map[string]any)
	if !ok {
		t.Fatalf("request_context missing: %#v", payload)
	}
	if got := requestContext["request_origin"]; got != "health_http" {
		t.Fatalf("request_context.request_origin = %v, want health_http", got)
	}
	if got := requestContext["client_kind"]; got != "http_client" {
		t.Fatalf("request_context.client_kind = %v, want http_client", got)
	}
	if got := requestContext["answered_for"]; got != "observer" {
		t.Fatalf("request_context.answered_for = %v, want observer", got)
	}
}

func TestTasksResponseIncludesRequestMarkers(t *testing.T) {
	server := newTestServer(t)
	body := bytes.NewBufferString(`{"description":"implement markers","type":"code"}`)
	request := httptest.NewRequest(http.MethodPost, "/tasks", body)
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusAccepted {
		t.Fatalf("expected accepted, got %d: %s", response.Code, response.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if got := payload["request_origin"]; got != "tasks_http" {
		t.Fatalf("request_origin = %v, want tasks_http", got)
	}
	if got := payload["client_kind"]; got != "http_client" {
		t.Fatalf("client_kind = %v, want http_client", got)
	}
	if got := payload["answered_for"]; got != "user" {
		t.Fatalf("answered_for = %v, want user", got)
	}
	responseOrigin, ok := payload["response_origin"].(map[string]any)
	if !ok {
		t.Fatalf("response_origin missing: %#v", payload)
	}
	if got := responseOrigin["request_origin"]; got != "tasks_http" {
		t.Fatalf("response_origin.request_origin = %v, want tasks_http", got)
	}
	if got := responseOrigin["client_kind"]; got != "http_client" {
		t.Fatalf("response_origin.client_kind = %v, want http_client", got)
	}
	if got := responseOrigin["answered_for"]; got != "user" {
		t.Fatalf("response_origin.answered_for = %v, want user", got)
	}
}

func TestTasksRejectInvalidInputValues(t *testing.T) {
	server := newTestServer(t)
	testCases := []struct {
		name      string
		body      string
		wantError string
	}{
		{
			name:      "invalid priority",
			body:      `{"description":"implement validation","type":"code","priority":"urgent"}`,
			wantError: "unsupported priority",
		},
		{
			name:      "invalid complexity",
			body:      `{"description":"implement validation","type":"code","complexity":"severe"}`,
			wantError: "unsupported complexity",
		},
		{
			name:      "invalid checkpoint policy",
			body:      `{"description":"implement validation","type":"code","checkpoint_policy":"always"}`,
			wantError: "unsupported checkpoint_policy",
		},
		{
			name:      "blank description",
			body:      `{"description":"   ","type":"code"}`,
			wantError: "task description is required",
		},
		{
			name:      "negative review depth",
			body:      `{"description":"implement validation","type":"code","review_depth":-1}`,
			wantError: "review_depth must be greater than or equal to zero",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/tasks", bytes.NewBufferString(tc.body))
			request.Header.Set("Content-Type", "application/json")
			response := httptest.NewRecorder()
			server.Handler().ServeHTTP(response, request)

			if response.Code != http.StatusBadRequest {
				t.Fatalf("expected bad request, got %d: %s", response.Code, response.Body.String())
			}
			if !strings.Contains(response.Body.String(), tc.wantError) {
				t.Fatalf("expected error containing %q, got %s", tc.wantError, response.Body.String())
			}
		})
	}
}

func TestTasksNormalizesNestedTransportPayloads(t *testing.T) {
	server := newTestServer(t)
	body := bytes.NewBufferString(`{
		"session_id":"nested-session",
		"type":"code",
		"input":{"description":"implement nested transport support","files":["main.go"]},
		"context":{"project":"migration","repo_path":"."},
		"priority":"high",
		"complexity":"medium",
		"checkpoint_policy":"branch",
		"review_depth":1
	}`)
	request := httptest.NewRequest(http.MethodPost, "/tasks", body)
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusAccepted {
		t.Fatalf("expected accepted, got %d: %s", response.Code, response.Body.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	workflow, ok := payload["workflow"].(map[string]any)
	if !ok {
		t.Fatalf("workflow missing: %#v", payload)
	}
	task, ok := workflow["task"].(map[string]any)
	if !ok {
		t.Fatalf("task missing: %#v", workflow)
	}
	input, ok := task["input"].(map[string]any)
	if !ok || input["description"] != "implement nested transport support" {
		t.Fatalf("task input mismatch: %#v", task)
	}
	contextPayload, ok := task["context"].(map[string]any)
	if !ok || contextPayload["project"] != "migration" || contextPayload["repo_path"] != "." {
		t.Fatalf("task context mismatch: %#v", task)
	}
	if got := task["priority"]; got != "high" {
		t.Fatalf("task priority = %v, want high", got)
	}
	if got := task["complexity"]; got != "medium" {
		t.Fatalf("task complexity = %v, want medium", got)
	}
	if got := task["checkpoint_policy"]; got != "branch" {
		t.Fatalf("task checkpoint_policy = %v, want branch", got)
	}
	if got := task["review_depth"]; got != float64(1) {
		t.Fatalf("task review_depth = %v, want 1", got)
	}
}

func TestHealthFullIncludesRuntimeSessionRoute(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/health/full", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("expected ok, got %d: %s", response.Code, response.Body.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	routes, ok := payload["required_routes"].([]any)
	if !ok {
		t.Fatalf("required_routes missing: %#v", payload)
	}
	found := false
	for _, raw := range routes {
		route, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if strings.TrimSpace(fmt.Sprint(route["route"])) == "/runtime/sessions/{session_id}/events" {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("expected runtime session route in manifest, got %#v", routes)
	}
}

func TestStatsIncludesRealtimeMetrics(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
	workflow := domain.WorkflowRecord{
		Task:       domain.Task{ID: "metrics-task-2", Type: domain.TaskTypeCode},
		Acceptance: domain.TaskAcceptance{Provider: "local", ModelName: "test-model"},
		Result: &domain.AgentResult{
			TaskID:    "metrics-task-2",
			Provider:  "local",
			ModelName: "test-model",
			Output: domain.ResultOutput{
				Artifacts: map[string]any{
					"realtime_metrics": map[string]any{
						"transport":              "sse",
						"native_streaming":       true,
						"time_to_first_token_ms": 18,
						"total_completion_ms":    160,
					},
				},
			},
		},
		UpdatedAt: time.Now().UTC(),
	}
	if err := store.SaveWorkflow(context.Background(), workflow); err != nil {
		t.Fatalf("SaveWorkflow: %v", err)
	}

	request := httptest.NewRequest(http.MethodGet, "/stats", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", response.Code, response.Body.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := payload["data"].(map[string]any)
	if !ok {
		t.Fatalf("expected data object, got %T", payload["data"])
	}
	if _, ok := data["state"].(map[string]any); !ok {
		t.Fatalf("expected state object, got %T", data["state"])
	}
	realtimeMetrics, ok := data["realtime_metrics"].(map[string]any)
	if !ok {
		t.Fatalf("expected realtime_metrics object, got %T", data["realtime_metrics"])
	}
	totals, ok := realtimeMetrics["totals"].(map[string]any)
	if !ok {
		t.Fatalf("expected realtime totals object, got %T", realtimeMetrics["totals"])
	}
	if totals["samples_collected"].(float64) < 1 {
		t.Fatalf("expected samples_collected >= 1, got %v", totals["samples_collected"])
	}
}

func TestStatsIncludesLiveRealtimeMetrics(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
	startedAt := time.Now().Add(-200 * time.Millisecond).UTC()
	orchestrator.ObserveLiveRealtimeSession("live-session-1", "local", "test-model", startedAt)
	orchestrator.ObserveLiveRealtimeDelta(domain.AgentDelta{SessionID: "live-session-1", Provider: "local", ModelName: "test-model", Kind: domain.AgentDeltaToken, Timestamp: startedAt.Add(12 * time.Millisecond)})
	orchestrator.ObserveLiveRealtimeDelta(domain.AgentDelta{SessionID: "live-session-1", Provider: "local", ModelName: "test-model", Kind: domain.AgentDeltaToolStarted, Timestamp: startedAt.Add(24 * time.Millisecond)})
	orchestrator.CompleteLiveRealtimeSession("live-session-1", "local", "test-model", startedAt.Add(150*time.Millisecond), false)

	request := httptest.NewRequest(http.MethodGet, "/stats", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", response.Code, response.Body.String())
	}

	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := payload["data"].(map[string]any)
	if !ok {
		t.Fatalf("expected data object, got %T", payload["data"])
	}
	liveMetrics, ok := data["live_realtime_metrics"].(map[string]any)
	if !ok {
		t.Fatalf("expected live_realtime_metrics object, got %T", data["live_realtime_metrics"])
	}
	if tracked := int64(liveMetrics["tracked_sessions"].(float64)); tracked < 1 {
		t.Fatalf("expected tracked_sessions >= 1, got %d", tracked)
	}
	models, ok := liveMetrics["models"].([]any)
	if !ok || len(models) == 0 {
		t.Fatalf("expected live models, got %#v", liveMetrics["models"])
	}
	firstModel, ok := models[0].(map[string]any)
	if !ok {
		t.Fatalf("expected first live model object, got %T", models[0])
	}
	if strings.TrimSpace(fmt.Sprint(firstModel["provider"])) != "local" {
		t.Fatalf("expected local provider, got %#v", firstModel)
	}
}

func TestPrometheusMetricsExposeLiveRealtimeAggregates(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
	startedAt := time.Now().Add(-200 * time.Millisecond).UTC()
	orchestrator.ObserveLiveRealtimeSession("live-session-2", "local", "test-model", startedAt)
	orchestrator.ObserveLiveRealtimeDelta(domain.AgentDelta{SessionID: "live-session-2", Provider: "local", ModelName: "test-model", Kind: domain.AgentDeltaToken, Timestamp: startedAt.Add(12 * time.Millisecond)})
	orchestrator.ObserveLiveRealtimeDelta(domain.AgentDelta{SessionID: "live-session-2", Provider: "local", ModelName: "test-model", Kind: domain.AgentDeltaToolStarted, Timestamp: startedAt.Add(24 * time.Millisecond)})
	orchestrator.CompleteLiveRealtimeSession("live-session-2", "local", "test-model", startedAt.Add(150*time.Millisecond), false)

	request := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", response.Code, response.Body.String())
	}
	body := response.Body.String()
	for _, expected := range []string{
		`go_core_realtime_live_sessions_started_total{model="test-model",provider="local"} 1`,
		`go_core_realtime_live_tokens_streamed_total{model="test-model",provider="local"} 1`,
		`go_core_realtime_live_time_to_first_token_ms_bucket{le="25",model="test-model",provider="local"} 1`,
		`go_core_realtime_live_time_to_first_token_ms_sum{model="test-model",provider="local"} 12`,
		`go_core_realtime_live_time_to_first_token_ms_count{model="test-model",provider="local"} 1`,
		`go_core_realtime_live_total_completion_ms_count{model="test-model",provider="local"} 1`,
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("expected metrics output to contain %q, got:\n%s", expected, body)
		}
	}
}

func TestPrometheusMetricsExposeRealtimeAggregates(t *testing.T) {
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	orchestrator := kernel.NewWithStore(store)
	server := NewServer(orchestrator, app.DefaultRequiredHTTPEndpoints)
	workflow := domain.WorkflowRecord{
		Task:       domain.Task{ID: "metrics-task-3", Type: domain.TaskTypeCode},
		Acceptance: domain.TaskAcceptance{Provider: "local", ModelName: "test-model"},
		Result: &domain.AgentResult{
			TaskID:    "metrics-task-3",
			Provider:  "local",
			ModelName: "test-model",
			Output: domain.ResultOutput{
				Artifacts: map[string]any{
					"realtime_metrics": map[string]any{
						"transport":               "sse",
						"native_streaming":        true,
						"pseudo_realtime":         false,
						"time_to_first_token_ms":  12,
						"time_to_first_tool_ms":   28,
						"time_to_first_patch_ms":  36,
						"time_to_first_result_ms": 44,
						"time_to_first_test_ms":   52,
						"total_completion_ms":     140,
						"tokens_streamed":         64,
						"tools_executed":          2,
						"patches_applied":         1,
						"tests_executed":          1,
					},
				},
			},
		},
		UpdatedAt: time.Now().UTC(),
	}
	if err := store.SaveWorkflow(context.Background(), workflow); err != nil {
		t.Fatalf("SaveWorkflow: %v", err)
	}

	request := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", response.Code, response.Body.String())
	}
	if contentType := response.Header().Get("Content-Type"); !strings.Contains(contentType, "text/plain") {
		t.Fatalf("expected text/plain content-type, got %q", contentType)
	}
	body := response.Body.String()
	for _, expected := range []string{
		`go_core_realtime_samples_total{model="test-model",provider="local"} 1`,
		`go_core_realtime_time_to_first_token_ms{model="test-model",provider="local"} 12`,
		`go_core_realtime_total_completion_ms{model="test-model",provider="local"} 140`,
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("expected metrics output to contain %q, got:\n%s", expected, body)
		}
	}
}
