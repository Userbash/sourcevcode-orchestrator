package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/buildinfo"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/kernel"
	"sourcevcode-orchestrator/go-core/internal/transport"
)

type Server struct {
	orchestrator   *kernel.Orchestrator
	dispatcher     *transport.Dispatcher
	requiredRoutes []string
	mux            *http.ServeMux
	wsAudit        *websocketAuditLog
}

func NewServer(orchestrator *kernel.Orchestrator, requiredRoutes []string) *Server {
	server := &Server{
		orchestrator:   orchestrator,
		requiredRoutes: append([]string(nil), requiredRoutes...),
		mux:            http.NewServeMux(),
		wsAudit:        newWebsocketAuditLog(100),
	}
	server.dispatcher = server.buildDispatcher()
	server.routes()
	return server
}

func (s *Server) Handler() http.Handler {
	return s.withMigrationHeaders(s.withRequestContext(s.mux))
}

func (s *Server) routes() {
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/api/health", s.handleHealth)
	s.mux.HandleFunc("/health/full", s.handleHealthFull)
	s.mux.HandleFunc("/diagnostics", s.handleDiagnostics)
	s.mux.HandleFunc("/stats", s.handleStats)
	s.mux.HandleFunc("/dump_memory", s.handleDumpMemory)
	s.mux.HandleFunc("/transport/audit", s.handleTransportAudit)
	s.mux.HandleFunc("/providers/inventory", s.handleProviderInventory)
	s.mux.HandleFunc("/providers/runtime_inventory", s.handleProviderRuntimeInventory)
	s.mux.HandleFunc("/providers/models/index", s.handleProviderModels)
	s.mux.HandleFunc("/providers/ai_kernel/gate", s.handleAIKernelGate)
	s.mux.HandleFunc("/providers/ai_kernel/ensure", s.handleUnavailableMutation("ai_kernel"))
	s.mux.HandleFunc("/providers/local_llm/residents", s.handleLocalResidents)
	s.mux.HandleFunc("/providers/local_llm/connect", s.handleLocalMutation("connect"))
	s.mux.HandleFunc("/providers/local_llm/disconnect", s.handleLocalMutation("disconnect"))
	s.mux.HandleFunc("/providers/local_llm/warm", s.handleLocalMutation("warm"))
	s.mux.HandleFunc("/delivery/health", s.handleDeliveryHealth)
	s.mux.HandleFunc("/delivery/inspect_timeouts", s.handleDeliveryInspectTimeouts)
	s.mux.HandleFunc("/delivery/dispatch", s.handleDeliveryDispatch)
	s.mux.HandleFunc("/delivery/ack", s.handleDeliveryAck)
	s.mux.HandleFunc("/delivery/confirm_payload", s.handleDeliveryConfirmPayload)
	s.mux.HandleFunc("/delivery/establish", s.handleDeliveryEstablish)
	s.mux.HandleFunc("/delivery/", s.handleDeliveryByTaskID)
	s.mux.HandleFunc("/mailboxes/", s.handleMailboxPath)
	s.mux.HandleFunc("/providers/", s.handleProviderPath)
	s.mux.HandleFunc("/runtime/routing_weights", s.handleRuntimeRoutingWeights)
	s.mux.HandleFunc("/runtime/providers/", s.handleRuntimeProviderPath)
	s.mux.HandleFunc("/runtime/agents/", s.handleRuntimeAgentPath)
	s.mux.HandleFunc("/health/local_models", s.handleLocalModelsHealth)
	s.mux.HandleFunc("/sourcecraft", s.handleSourcecraft)
	s.mux.HandleFunc("/sourcecraft/delegate", s.handleSourcecraftDelegate)
	s.mux.HandleFunc("/sourcecraft/parallel_delegate", s.handleSourcecraftDelegate)
	s.mux.HandleFunc("/socraticode/context_compaction/status", s.handleSocraticodeStatus)
	s.mux.HandleFunc("/control/ws", s.handleControlWebSocket)
	s.mux.HandleFunc("/chat/ws", s.handleChatWebSocket)
	s.mux.HandleFunc("/ws/runtime/events", s.handleRuntimeWebSocket)
	s.mux.HandleFunc("/ws/providers/inventory", s.handleInventoryWebSocket)
	s.mux.HandleFunc("/state", s.handleState)
	s.mux.HandleFunc("/modules", s.handleModules)
	s.mux.HandleFunc("/agents", s.handleAgents)
	s.mux.HandleFunc("/tasks", s.handleTasks)
	s.mux.HandleFunc("/tasks/preview_plan", s.handlePreviewPlan)
	s.mux.HandleFunc("/tasks/run_plan", s.handleRunPlan)
	s.mux.HandleFunc("/tasks/", s.handleTaskByID)
	s.mux.HandleFunc("/events/runtime", s.handleRuntimeEvents)
	s.mux.HandleFunc("/events/inventory", s.handleInventoryEvents)
}

func (s *Server) handleDeliveryHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.orchestrator.DeliveryHealthSnapshot()})
}

func (s *Server) handleDeliveryInspectTimeouts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.orchestrator.InspectDeliveryTimeouts(r.Context())})
}

func (s *Server) handleDeliveryDispatch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var envelope domain.TaskEnvelope
	if err := json.NewDecoder(r.Body).Decode(&envelope); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusAccepted, map[string]any{"status": "ok", "data": s.orchestrator.DispatchEnvelope(r.Context(), envelope)})
}

func (s *Server) handleDeliveryByTaskID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	taskID := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/delivery/"))
	if taskID == "" {
		s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "delivery task not found"})
		return
	}
	snapshot := s.orchestrator.RefreshDelivery(r.Context(), taskID)
	if len(snapshot) == 0 {
		s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "delivery task not found"})
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": snapshot})
}

func (s *Server) handleDeliveryAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		TaskID     string           `json:"task_id"`
		Status     domain.AckStatus `json:"status"`
		ReceivedBy string           `json:"received_by"`
		Reason     string           `json:"reason"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	ack := s.orchestrator.AckDelivery(r.Context(), req.TaskID, req.Status, req.ReceivedBy, req.Reason)
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "ack": ack, "data": s.orchestrator.RefreshDelivery(r.Context(), req.TaskID)})
}

func (s *Server) handleDeliveryConfirmPayload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		TaskID   string              `json:"task_id"`
		AgentID  string              `json:"agent_id"`
		Envelope domain.TaskEnvelope `json:"envelope"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	confirmed := s.orchestrator.ConfirmDeliveryPayload(r.Context(), req.TaskID, req.AgentID, req.Envelope)
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "confirmed": confirmed, "data": s.orchestrator.RefreshDelivery(r.Context(), req.TaskID)})
}

func (s *Server) handleDeliveryEstablish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		TaskID  string `json:"task_id"`
		AgentID string `json:"agent_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	ack := s.orchestrator.EstablishDeliveryHandshake(r.Context(), req.TaskID, req.AgentID)
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "ack": ack, "data": s.orchestrator.RefreshDelivery(r.Context(), req.TaskID)})
}

func (s *Server) handleMailboxPath(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/mailboxes/")
	parts := strings.Split(path, "/")
	if len(parts) == 0 || strings.TrimSpace(parts[0]) == "" {
		s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "mailbox not found"})
		return
	}
	agentID := strings.TrimSpace(parts[0])
	if len(parts) == 1 && r.Method == http.MethodGet {
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.orchestrator.MailboxSnapshot(agentID)})
		return
	}
	if len(parts) == 2 && parts[1] == "fetch" && r.Method == http.MethodPost {
		var req struct {
			Limit int `json:"limit"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.orchestrator.FetchAgentMailbox(r.Context(), agentID, req.Limit)})
		return
	}
	s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "mailbox route not found"})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{
		"status":    "ok",
		"component": "go-core",
		"time":      time.Now().UTC(),
	}))
}

func (s *Server) handleHealthFull(w http.ResponseWriter, r *http.Request) {
	snapshot := s.orchestrator.StateSnapshot(context.Background())
	s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{
		"status":             "ok",
		"component":          "go-core",
		"time":               time.Now().UTC(),
		"kernel_version":     buildinfo.Snapshot(),
		"required_routes":    s.routeManifest(),
		"compatibility_gaps": s.compatibilityGaps(),
		"transport": map[string]any{
			"runtime_stream":    "sse+websocket",
			"inventory_stream":  "sse+websocket",
			"websocket_session": "implemented",
			"ws_dispatcher":     "implemented",
			"daemon_bootstrap":  "go",
		},
		"request_context": metadataFromContext(r.Context()),
		"state":           snapshot,
	}))
}

func (s *Server) handleDiagnostics(w http.ResponseWriter, r *http.Request) {
	layers := r.URL.Query()["layer"]
	matrixOnly := strings.EqualFold(r.URL.Query().Get("matrix_only"), "true")
	s.writeJSON(w, http.StatusOK, s.diagnosticsSnapshot(r.Context(), layers, matrixOnly))
}

func (s *Server) handleState(w http.ResponseWriter, _ *http.Request) {
	s.writeJSON(w, http.StatusOK, s.orchestrator.StateSnapshot(context.Background()))
}

func (s *Server) handleModules(w http.ResponseWriter, _ *http.Request) {
	s.writeJSON(w, http.StatusOK, s.orchestrator.Modules())
}

func (s *Server) handleAgents(w http.ResponseWriter, _ *http.Request) {
	s.writeJSON(w, http.StatusOK, s.orchestrator.Agents())
}

func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		workflows, err := s.orchestrator.Workflows(r.Context())
		if err != nil {
			s.writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
			return
		}
		s.writeJSON(w, http.StatusOK, workflows)
	case http.MethodPost:
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		task, err := taskFromTransport(payload)
		if err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		workflow, err := s.orchestrator.SubmitTask(r.Context(), task)
		if err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		s.writeJSON(w, http.StatusAccepted, workflowResponsePayload(r.Context(), workflow, "http"))
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (s *Server) handlePreviewPlan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var payload map[string]any
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	task, err := taskFromTransport(payload)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	preview, err := s.orchestrator.PreviewExecutionPlan(r.Context(), task)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{"status": "ok", "data": preview}))
}

func (s *Server) handleRunPlan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var payload map[string]any
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	task, err := taskFromTransport(payload)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	run, err := s.orchestrator.RunExecutionPlan(r.Context(), task)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{"status": "ok", "data": run}))
}

func (s *Server) handleTaskByID(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/tasks/"), "/")
	if path == "" {
		s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "task not found"})
		return
	}
	if strings.HasSuffix(path, "/checkpoint") {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		rootTaskID := strings.TrimSuffix(path, "/checkpoint")
		rootTaskID = strings.TrimSuffix(rootTaskID, "/")
		sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))
		if sessionID == "" {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": "session_id is required"})
			return
		}
		checkpoint, ok, err := s.orchestrator.LoadParallelCheckpoint(r.Context(), sessionID, rootTaskID)
		if err != nil {
			s.writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
			return
		}
		if !ok {
			s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "checkpoint not found"})
			return
		}
		s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{"status": "ok", "data": checkpoint}))
		return
	}
	if strings.HasSuffix(path, "/resume_plan") {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		rootTaskID := strings.TrimSuffix(path, "/resume_plan")
		rootTaskID = strings.TrimSuffix(rootTaskID, "/")
		sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))
		if sessionID == "" {
			var payload map[string]any
			if err := json.NewDecoder(r.Body).Decode(&payload); err == nil {
				sessionID = strings.TrimSpace(stringField(payload, "session_id"))
			}
		}
		if sessionID == "" {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": "session_id is required"})
			return
		}
		run, err := s.orchestrator.ResumeExecutionPlan(r.Context(), sessionID, rootTaskID)
		if err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
			return
		}
		s.writeJSON(w, http.StatusOK, addResponseMetadata(r.Context(), map[string]any{"status": "ok", "data": run}))
		return
	}
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	workflow, ok, err := s.orchestrator.Workflow(r.Context(), path)
	if err != nil {
		s.writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	if !ok {
		s.writeJSON(w, http.StatusNotFound, map[string]any{"error": "task not found"})
		return
	}
	s.writeJSON(w, http.StatusOK, workflowResponsePayload(r.Context(), workflow, "http"))
}

func (s *Server) handleRuntimeEvents(w http.ResponseWriter, r *http.Request) {
	topic := strings.TrimSpace(r.URL.Query().Get("topic"))
	history := s.orchestrator.RuntimeEventSnapshot(topic)
	subscription := s.orchestrator.SubscribeRuntimeEvents(topic)
	defer subscription.Close()
	s.streamEvents(w, r, history, subscription.Events)
}

func (s *Server) handleInventoryEvents(w http.ResponseWriter, r *http.Request) {
	topic := strings.TrimSpace(r.URL.Query().Get("topic"))
	history := s.orchestrator.InventoryEventSnapshot(topic)
	subscription := s.orchestrator.SubscribeInventoryEvents(topic)
	defer subscription.Close()
	s.streamEvents(w, r, history, subscription.Events)
}

func (s *Server) streamEvents(w http.ResponseWriter, r *http.Request, history []domain.StreamEvent, stream <-chan domain.StreamEvent) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	for _, event := range history {
		if err := writeSSE(w, event); err != nil {
			return
		}
	}
	flusher.Flush()

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			fmt.Fprint(w, ": heartbeat\n\n")
			flusher.Flush()
		case event, ok := <-stream:
			if !ok {
				return
			}
			if err := writeSSE(w, event); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

func writeSSE(w http.ResponseWriter, event domain.StreamEvent) error {
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "event: %s\n", event.Kind); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "data: %s\n\n", data); err != nil {
		return err
	}
	return nil
}

func (s *Server) routeManifest() []map[string]any {
	routes := []string{
		"/health", "/api/health", "/health/full", "/health/local_models",
		"/diagnostics", "/stats", "/dump_memory", "/transport/audit",
		"/state", "/modules", "/agents", "/tasks", "/tasks/preview_plan", "/tasks/run_plan", "/tasks/{id}", "/tasks/{id}/checkpoint", "/tasks/{id}/resume_plan",
		"/providers/inventory", "/providers/{provider}/inventory",
		"/providers/runtime_inventory", "/providers/{provider}/runtime_inventory",
		"/providers/models/index", "/providers/models/index/{model_name}",
		"/providers/ai_kernel/gate", "/providers/ai_kernel/ensure",
		"/providers/local_llm/residents", "/providers/local_llm/connect",
		"/providers/local_llm/disconnect", "/providers/local_llm/warm",
		"/delivery/health", "/delivery/inspect_timeouts", "/delivery/dispatch",
		"/delivery/{task_id}", "/delivery/ack", "/delivery/confirm_payload",
		"/delivery/establish", "/mailboxes/{agent_id}", "/mailboxes/{agent_id}/fetch",
		"/runtime/routing_weights", "/runtime/providers/{provider}/probe",
		"/runtime/agents/{agent_id}/probe", "/runtime/agents/{agent_id}/suppress",
		"/runtime/agents/{agent_id}/recover",
		"/sourcecraft", "/sourcecraft/delegate", "/sourcecraft/parallel_delegate",
		"/socraticode/context_compaction/status",
		"/events/runtime", "/events/inventory", "/control/ws", "/chat/ws",
		"/ws/runtime/events", "/ws/providers/inventory",
	}
	manifest := make([]map[string]any, 0, len(routes))
	required := make(map[string]struct{}, len(s.requiredRoutes))
	for _, route := range s.requiredRoutes {
		required[route] = struct{}{}
	}
	for _, route := range routes {
		_, isRequired := required[route]
		manifest = append(manifest, map[string]any{
			"route":    route,
			"required": isRequired,
			"status":   "implemented",
		})
	}
	for _, route := range s.requiredRoutes {
		if route == "/tasks/{id}" {
			continue
		}
		found := false
		for _, item := range manifest {
			if item["route"] == route {
				found = true
				break
			}
		}
		if !found {
			manifest = append(manifest, map[string]any{
				"route":    route,
				"required": true,
				"status":   "missing",
			})
		}
	}
	return manifest
}

func (s *Server) compatibilityGaps() []string {
	return []string{
		"voice/audio and ML-specific integrations still live in the legacy python tree",
		"advanced trained memory and validation still require more Go ports",
		"socraticode runtime and ai_kernel ensure remain on compatibility stubs in go-core",
	}
}

func (s *Server) diagnosticLayers(snapshot map[string]any) []map[string]any {
	return []map[string]any{
		{
			"name":   "transport",
			"status": "implemented",
			"details": map[string]any{
				"http":                 true,
				"sse_runtime":          true,
				"sse_inventory":        true,
				"websocket_session":    true,
				"websocket_dispatcher": true,
			},
		},
		{
			"name":    "runtime",
			"status":  "partial",
			"details": snapshot,
		},
		{
			"name":   "compatibility",
			"status": "partial",
			"details": map[string]any{
				"gaps": s.compatibilityGaps(),
			},
		},
	}
}

func (s *Server) filterDiagnosticLayers(layers []map[string]any, requested []string) []map[string]any {
	if len(requested) == 0 {
		return layers
	}
	allowed := make(map[string]struct{}, len(requested))
	for _, item := range requested {
		allowed[strings.TrimSpace(strings.ToLower(item))] = struct{}{}
	}
	filtered := make([]map[string]any, 0, len(layers))
	for _, layer := range layers {
		name, _ := layer["name"].(string)
		if _, ok := allowed[strings.ToLower(name)]; ok {
			filtered = append(filtered, layer)
		}
	}
	return filtered
}

func (s *Server) writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
