package api

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/transport"
)

func (s *Server) handleProviderInventory(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.providerInventory("")})
}

func (s *Server) handleProviderRuntimeInventory(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok", "data": s.providerInventory("", true),
	})
}

func (s *Server) handleProviderModels(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.modelIndex()})
}

func (s *Server) handleProviderPath(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/providers/")
	if strings.HasPrefix(path, "models/index/") {
		modelName := strings.TrimPrefix(path, "models/index/")
		record, ok := s.modelIndex()[modelName]
		if !ok {
			s.writeJSON(w, http.StatusNotFound, map[string]any{"status": "error", "error": "model not found"})
			return
		}
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": record})
		return
	}
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 2 && (parts[1] == "inventory" || parts[1] == "runtime_inventory") {
		inventory := s.providerInventory(parts[0])
		record, ok := inventory[strings.ToLower(parts[0])]
		if !ok {
			s.writeJSON(w, http.StatusNotFound, map[string]any{"status": "error", "error": "provider not found"})
			return
		}
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": record})
		return
	}
	http.NotFound(w, r)
}

func (s *Server) handleRuntimeRoutingWeights(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": s.orchestrator.RuntimeRoutingWeights()})
}

func (s *Server) handleRuntimeProviderPath(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/runtime/providers/"), "/")
	parts := strings.Split(path, "/")
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || parts[1] != "probe" {
		http.NotFound(w, r)
		return
	}
	payload := s.orchestrator.ProbeProviderRuntime(r.Context(), parts[0])
	status := http.StatusOK
	if payload["status"] == "error" {
		status = http.StatusBadRequest
	}
	s.writeJSON(w, status, payload)
}

func (s *Server) handleRuntimeAgentPath(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/runtime/agents/"), "/")
	parts := strings.Split(path, "/")
	if len(parts) < 2 || strings.TrimSpace(parts[0]) == "" {
		http.NotFound(w, r)
		return
	}
	agentID := strings.TrimSpace(parts[0])
	action := parts[1]
	switch {
	case r.Method == http.MethodGet && len(parts) == 2 && action == "probe":
		payload := s.orchestrator.ProbeAgentRuntime(r.Context(), agentID)
		status := http.StatusOK
		if payload["status"] == "error" {
			status = http.StatusNotFound
		}
		s.writeJSON(w, status, payload)
	case r.Method == http.MethodPost && len(parts) == 2 && action == "suppress":
		var req struct {
			Reason  string `json:"reason"`
			Seconds int    `json:"seconds"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			s.writeJSON(w, http.StatusBadRequest, map[string]any{"status": "error", "error": err.Error()})
			return
		}
		state, ok := s.orchestrator.SuppressLane(agentID, req.Reason, req.Seconds)
		if !ok {
			s.writeJSON(w, http.StatusNotFound, map[string]any{"status": "error", "error": "agent not found", "agent_id": agentID})
			return
		}
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "agent_id": agentID, "runtime_state": state})
	case r.Method == http.MethodPost && len(parts) == 2 && action == "recover":
		state, ok := s.orchestrator.RecoverLane(agentID)
		if !ok {
			s.writeJSON(w, http.StatusNotFound, map[string]any{"status": "error", "error": "agent not found", "agent_id": agentID})
			return
		}
		s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "agent_id": agentID, "runtime_state": state})
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) handleAIKernelGate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	payload := s.aiKernelGate()
	status := http.StatusServiceUnavailable
	if ready, _ := payload["ready"].(bool); ready {
		status = http.StatusOK
	}
	s.writeJSON(w, status, payload)
}

func (s *Server) handleUnavailableMutation(component string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		s.writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"status": "unavailable",
			"error":  component + " runtime bridge is not configured in go-core",
		})
	}
}

func (s *Server) handleLocalModelsHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	payload := s.localModelHealth()
	status := http.StatusServiceUnavailable
	if ready, _ := payload["overall_ok"].(bool); ready {
		status = http.StatusOK
	}
	s.writeJSON(w, status, payload)
}

func (s *Server) handleLocalResidents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	manager := s.orchestrator.LocalModelManager()
	if manager == nil {
		s.writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "error", "message": "local llm runtime is unavailable"})
		return
	}
	data, err := manager.Residents(r.Context())
	if err != nil {
		s.writeJSON(w, http.StatusBadGateway, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, data)
}

func (s *Server) handleLocalMutation(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		payload, status, err := s.runLocalMutation(r.Context(), action, modelNameFromRequest(r))
		if err != nil {
			s.writeJSON(w, status, protocolErrorBody(err))
			return
		}
		s.writeJSON(w, status, payload)
	}
}

func (s *Server) runLocalMutation(ctx context.Context, action string, modelName string) (map[string]any, int, error) {
	manager := s.orchestrator.LocalModelManager()
	if manager == nil {
		return nil, http.StatusServiceUnavailable, transport.NewProtocolFrameError("UNAVAILABLE", "local llm runtime is unavailable", nil)
	}
	if strings.TrimSpace(modelName) == "" {
		return nil, http.StatusBadRequest, transport.NewProtocolFrameError("INVALID_REQUEST", "model_name is required", nil)
	}
	var (
		payload map[string]any
		err     error
	)
	switch action {
	case "connect":
		payload, err = manager.Connect(ctx, modelName)
	case "disconnect":
		payload, err = manager.Disconnect(ctx, modelName)
	case "warm":
		payload, err = manager.Warm(ctx, modelName)
	default:
		return nil, http.StatusBadRequest, transport.NewProtocolFrameError("INVALID_REQUEST", "unknown local llm action: "+action, nil)
	}
	if err != nil {
		return nil, http.StatusBadGateway, transport.NewProtocolFrameError("PROVIDER_ERROR", err.Error(), nil)
	}
	s.orchestrator.RefreshInventory(ctx)
	return map[string]any{"status": "ok", "model_name": modelName, "action": action, "result": payload}, http.StatusOK, nil
}

func modelNameFromRequest(r *http.Request) string {
	if modelName := strings.TrimSpace(r.URL.Query().Get("model_name")); modelName != "" {
		return modelName
	}
	defer r.Body.Close()
	var payload map[string]any
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		return ""
	}
	return strings.TrimSpace(stringField(payload, "model_name"))
}

func (s *Server) handleSourcecraft(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, s.sourcecraftStatus())
}

func (s *Server) handleSourcecraftDelegate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var payload map[string]any
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	task, err := taskFromTransport(payload)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	record, err := s.orchestrator.SubmitTask(r.Context(), task)
	if err != nil {
		s.writeJSON(w, http.StatusBadRequest, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusAccepted, workflowResponsePayload(r.Context(), record, "http"))
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	workflows, err := s.orchestrator.Workflows(r.Context())
	if err != nil {
		s.writeJSON(w, http.StatusInternalServerError, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": map[string]any{"state": s.orchestrator.StateSnapshot(r.Context()), "realtime_metrics": aggregateRealtimeMetrics(workflows), "live_realtime_metrics": s.orchestrator.LiveRealtimeMetricsSnapshot()}})
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	workflows, err := s.orchestrator.Workflows(r.Context())
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	report := buildRealtimeMetricsReport(workflows)
	liveSnapshot := s.orchestrator.LiveRealtimeMetricsSnapshot()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = w.Write([]byte(formatRealtimeMetricsPrometheus(report) + formatLiveRealtimeMetricsPrometheus(liveSnapshot)))
}

func (s *Server) handleDumpMemory(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	workflows, err := s.orchestrator.Workflows(r.Context())
	if err != nil {
		s.writeJSON(w, http.StatusInternalServerError, map[string]any{"status": "error", "error": err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "workflows": workflows, "state": s.orchestrator.ModuleState()})
}

func (s *Server) handleTransportAudit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusOK, s.transportAudit())
}

func (s *Server) handleSocraticodeStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.writeJSON(w, http.StatusServiceUnavailable, map[string]any{
		"status": "unavailable", "mode": "raw_prompt", "reason": "socraticode module is not registered in go-core",
	})
}

func (s *Server) withMigrationHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if action := migrationActionForPath(r); action != "" {
			w.Header().Set("X-Control-WS-Action", action)
		}
		next.ServeHTTP(w, r)
	})
}

func migrationActionForPath(r *http.Request) string {
	if profile, ok := routeProfileForRequest(r); ok {
		return profile.MigrationAction
	}
	return ""
}

func protocolErrorBody(err error) map[string]any {
	return map[string]any{"status": "error", "error": transport.NewProtocolFrameError(transport.ErrorCode(err), err.Error(), nil)}
}
