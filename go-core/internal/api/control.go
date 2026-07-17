package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/buildinfo"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/transport"
)

func (s *Server) buildDispatcher() *transport.Dispatcher {
	dispatcher := transport.NewDispatcher()

	dispatcher.RegisterSingle("providers.inventory.get", false, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.providerInventory("")}, nil
	})
	dispatcher.RegisterSingle("providers.inventory.provider.get", false, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		provider := stringField(request.Data, "provider")
		inventory := s.providerInventory(provider)
		if len(inventory) == 0 {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "provider not found: "+provider, nil)
		}
		return map[string]any{"status": "ok", "data": inventory[provider]}, nil
	})
	dispatcher.RegisterSingle("providers.runtime_inventory.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.providerInventory("", true)}, nil
	})
	dispatcher.RegisterSingle("providers.runtime_inventory.provider.get", false, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		provider := stringField(request.Data, "provider")
		inventory := s.providerInventory(provider)
		if len(inventory) == 0 {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "provider not found: "+provider, nil)
		}
		return map[string]any{"status": "ok", "data": inventory[provider]}, nil
	})
	dispatcher.RegisterSingle("providers.openai.runtime_inventory.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		provider := agents.PreferredCloudProvider(agents.LoadOpenAICompatibleConfigs())
		inventory := s.providerInventory(provider, true)
		return map[string]any{"status": "ok", "data": inventory[provider]}, nil
	})
	dispatcher.RegisterSingle("providers.codexsale.runtime_inventory.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		inventory := s.providerInventory("codexsale", true)
		return map[string]any{"status": "ok", "data": inventory["codexsale"]}, nil
	})
	dispatcher.RegisterSingle("providers.models.index.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.modelIndex()}, nil
	})
	dispatcher.RegisterSingle("providers.models.lookup.get", false, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		modelName := stringField(request.Data, "model_name")
		record, ok := s.modelIndex()[modelName]
		if !ok {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "model not found: "+modelName, nil)
		}
		return map[string]any{"status": "ok", "data": record}, nil
	})
	dispatcher.RegisterSingle("stats.get", false, func(ctx context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.orchestrator.StateSnapshot(ctx)}, nil
	})
	dispatcher.RegisterSingle("health.local_models.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return s.localModelHealth(), nil
	})
	dispatcher.RegisterSingle("memory.dump.get", false, func(ctx context.Context, _ transport.Envelope) (map[string]any, error) {
		workflows, err := s.orchestrator.Workflows(ctx)
		if err != nil {
			return nil, err
		}
		return map[string]any{"status": "ok", "workflows": workflows, "state": s.orchestrator.ModuleState()}, nil
	})
	dispatcher.RegisterSingle("transport.audit.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return s.transportAudit(), nil
	})
	dispatcher.RegisterSingle("sourcecraft.status.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return s.sourcecraftStatus(), nil
	})
	dispatcher.RegisterSingle("socraticode.context_compaction.status.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "unavailable", "mode": "raw_prompt", "reason": "socraticode module is not registered in go-core"}, nil
	})
	dispatcher.RegisterSingle("providers.ai_kernel.gate.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return s.aiKernelGate(), nil
	})
	dispatcher.RegisterSingle("providers.local_llm.residents.get", false, func(ctx context.Context, _ transport.Envelope) (map[string]any, error) {
		manager := s.orchestrator.LocalModelManager()
		if manager == nil {
			return nil, transport.NewProtocolFrameError("UNAVAILABLE", "local llm runtime is unavailable", nil)
		}
		payload, err := manager.Residents(ctx)
		if err != nil {
			return nil, transport.NewProtocolFrameError("PROVIDER_ERROR", err.Error(), nil)
		}
		return payload, nil
	})
	dispatcher.RegisterSingle("diagnostics.get", false, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		return s.diagnosticsSnapshot(ctx, stringSliceField(request.Data, "layers"), boolField(request.Data, "matrix_only")), nil
	})
	dispatcher.RegisterSingle("runtime.routing_weights.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.orchestrator.RuntimeRoutingWeights()}, nil
	})
	dispatcher.RegisterSingle("runtime.provider.probe", false, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		provider := stringField(request.Data, "provider")
		if provider == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "provider is required", nil)
		}
		payload := s.orchestrator.ProbeProviderRuntime(ctx, provider)
		if payload["status"] == "error" {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", firstStringField(payload, "error"), nil)
		}
		return payload, nil
	})
	dispatcher.RegisterSingle("runtime.agent.probe", false, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		agentID := stringField(request.Data, "agent_id")
		if agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "agent_id is required", nil)
		}
		payload := s.orchestrator.ProbeAgentRuntime(ctx, agentID)
		if payload["status"] == "error" {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", firstStringField(payload, "error"), nil)
		}
		return payload, nil
	})
	dispatcher.RegisterSingle("runtime.agent.suppress", true, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		agentID := stringField(request.Data, "agent_id")
		if agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "agent_id is required", nil)
		}
		state, ok := s.orchestrator.SuppressLane(agentID, stringField(request.Data, "reason"), intField(request.Data, "seconds"))
		if !ok {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "agent not found: "+agentID, nil)
		}
		return map[string]any{"status": "ok", "agent_id": agentID, "runtime_state": state}, nil
	})
	dispatcher.RegisterSingle("runtime.agent.recover", true, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		agentID := stringField(request.Data, "agent_id")
		if agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "agent_id is required", nil)
		}
		state, ok := s.orchestrator.RecoverLane(agentID)
		if !ok {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "agent not found: "+agentID, nil)
		}
		return map[string]any{"status": "ok", "agent_id": agentID, "runtime_state": state}, nil
	})
	dispatcher.RegisterSingle("delivery.health.get", false, func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.orchestrator.DeliveryHealthSnapshot()}, nil
	})
	dispatcher.RegisterSingle("delivery.inspect_timeouts", true, func(ctx context.Context, _ transport.Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok", "data": s.orchestrator.InspectDeliveryTimeouts(ctx)}, nil
	})
	dispatcher.RegisterSingle("delivery.dispatch", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		envelope, err := decodeTransportEnvelope(request.Data)
		if err != nil {
			return nil, err
		}
		return map[string]any{"status": "ok", "data": s.orchestrator.DispatchEnvelope(ctx, envelope)}, nil
	})
	dispatcher.RegisterSingle("delivery.snapshot.get", false, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		taskID := stringField(request.Data, "task_id")
		if taskID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "task_id is required", nil)
		}
		snapshot := s.orchestrator.RefreshDelivery(ctx, taskID)
		if len(snapshot) == 0 {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "delivery task not found: "+taskID, nil)
		}
		return map[string]any{"status": "ok", "data": snapshot}, nil
	})
	dispatcher.RegisterSingle("delivery.ack", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		taskID := stringField(request.Data, "task_id")
		if taskID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "task_id is required", nil)
		}
		ack := s.orchestrator.AckDelivery(ctx, taskID, domain.AckStatus(stringField(request.Data, "status")), stringField(request.Data, "received_by"), stringField(request.Data, "reason"))
		return map[string]any{"status": "ok", "ack": ack, "data": s.orchestrator.RefreshDelivery(ctx, taskID)}, nil
	})
	dispatcher.RegisterSingle("delivery.confirm_payload", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		taskID := stringField(request.Data, "task_id")
		agentID := stringField(request.Data, "agent_id")
		if taskID == "" || agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "task_id and agent_id are required", nil)
		}
		envelope, err := decodeNestedEnvelope(request.Data, "envelope")
		if err != nil {
			return nil, err
		}
		confirmed := s.orchestrator.ConfirmDeliveryPayload(ctx, taskID, agentID, envelope)
		return map[string]any{"status": "ok", "confirmed": confirmed, "data": s.orchestrator.RefreshDelivery(ctx, taskID)}, nil
	})
	dispatcher.RegisterSingle("delivery.establish", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		taskID := stringField(request.Data, "task_id")
		agentID := stringField(request.Data, "agent_id")
		if taskID == "" || agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "task_id and agent_id are required", nil)
		}
		ack := s.orchestrator.EstablishDeliveryHandshake(ctx, taskID, agentID)
		return map[string]any{"status": "ok", "ack": ack, "data": s.orchestrator.RefreshDelivery(ctx, taskID)}, nil
	})
	dispatcher.RegisterSingle("mailbox.snapshot.get", false, func(_ context.Context, request transport.Envelope) (map[string]any, error) {
		agentID := stringField(request.Data, "agent_id")
		if agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "agent_id is required", nil)
		}
		return map[string]any{"status": "ok", "data": s.orchestrator.MailboxSnapshot(agentID)}, nil
	})
	dispatcher.RegisterSingle("mailbox.fetch", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		agentID := stringField(request.Data, "agent_id")
		if agentID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "agent_id is required", nil)
		}
		return map[string]any{"status": "ok", "data": s.orchestrator.FetchAgentMailbox(ctx, agentID, intField(request.Data, "limit"))}, nil
	})
	dispatcher.RegisterSingle("tasks.plan.preview", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		task, err := taskFromTransport(request.Data)
		if err != nil {
			return nil, err
		}
		preview, err := s.orchestrator.PreviewExecutionPlan(ctx, task)
		if err != nil {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", err.Error(), nil)
		}
		return map[string]any{"status": "ok", "data": preview}, nil
	})
	dispatcher.RegisterSingle("tasks.plan.checkpoint.get", false, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		sessionID := firstStringField(request.Data, "session_id")
		rootTaskID := firstStringField(request.Data, "task_id", "root_task_id", "id")
		if sessionID == "" || rootTaskID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "session_id and task_id are required", nil)
		}
		checkpoint, ok, err := s.orchestrator.LoadParallelCheckpoint(ctx, sessionID, rootTaskID)
		if err != nil {
			return nil, transport.NewProtocolFrameError("INTERNAL_ERROR", err.Error(), nil)
		}
		if !ok {
			return nil, transport.NewProtocolFrameError("NOT_FOUND", "parallel checkpoint not found: "+rootTaskID, nil)
		}
		return map[string]any{"status": "ok", "data": checkpoint}, nil
	})
	dispatcher.RegisterSingle("tasks.plan.run", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		task, err := taskFromTransport(request.Data)
		if err != nil {
			return nil, err
		}
		run, err := s.orchestrator.RunExecutionPlan(ctx, task)
		if err != nil {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", err.Error(), nil)
		}
		return map[string]any{"status": "ok", "data": run}, nil
	})
	dispatcher.RegisterSingle("tasks.plan.resume", true, func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		sessionID := firstStringField(request.Data, "session_id")
		rootTaskID := firstStringField(request.Data, "task_id", "root_task_id", "id")
		if sessionID == "" || rootTaskID == "" {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", "session_id and task_id are required", nil)
		}
		run, err := s.orchestrator.ResumeExecutionPlan(ctx, sessionID, rootTaskID)
		if err != nil {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", err.Error(), nil)
		}
		return map[string]any{"status": "ok", "data": run}, nil
	})

	unavailableMutation := func(component string) func(context.Context, transport.Envelope) (map[string]any, error) {
		return func(_ context.Context, _ transport.Envelope) (map[string]any, error) {
			return nil, transport.NewProtocolFrameError("UNAVAILABLE", component+" runtime bridge is not configured in go-core", nil)
		}
	}
	localMutation := func(action string) func(context.Context, transport.Envelope) (map[string]any, error) {
		return func(ctx context.Context, env transport.Envelope) (map[string]any, error) {
			payload, _, err := s.runLocalMutation(ctx, action, stringField(env.Data, "model_name"))
			return payload, err
		}
	}
	dispatcher.RegisterSingle("providers.local_llm.connect", true, localMutation("connect"))
	dispatcher.RegisterSingle("providers.local_llm.disconnect", true, localMutation("disconnect"))
	dispatcher.RegisterSingle("providers.local_llm.warm", true, localMutation("warm"))
	dispatcher.RegisterSingle("providers.ai_kernel.ensure", true, unavailableMutation("ai_kernel"))
	dispatcher.RegisterSingle("local_llm/connect", true, localMutation("connect"))
	dispatcher.RegisterSingle("local_llm/disconnect", true, localMutation("disconnect"))
	dispatcher.RegisterSingle("local_llm/warm", true, localMutation("warm"))
	dispatcher.RegisterSingle("ai_kernel/ensure", true, unavailableMutation("ai_kernel"))

	submit := func(ctx context.Context, request transport.Envelope) (map[string]any, error) {
		task, err := taskFromTransport(request.Data)
		if err != nil {
			return nil, err
		}
		record, err := s.orchestrator.SubmitTask(ctx, task)
		if err != nil {
			return nil, transport.NewProtocolFrameError("BAD_REQUEST", err.Error(), nil)
		}
		return workflowResponsePayload(ctx, record, "websocket"), nil
	}
	dispatcher.RegisterSingle("chat.submit", true, submit)
	dispatcher.RegisterSingle("sourcecraft.delegate.get", true, submit)
	dispatcher.RegisterSingle("sourcecraft.parallel_delegate.get", true, submit)

	registerEventStream := func(action, streamName string, inventory bool) {
		dispatcher.Register(action, "stream", true, func(ctx context.Context, request transport.Envelope, emit transport.Emitter) error {
			topic := stringField(request.Data, "topic")
			if topic == "" {
				topic = "all"
			}
			var history []domain.StreamEvent
			var events <-chan domain.StreamEvent
			var closeSubscription func()
			if inventory {
				history = s.orchestrator.InventoryEventSnapshot(topic)
				subscription := s.orchestrator.SubscribeInventoryEvents(topic)
				events = subscription.Events
				closeSubscription = subscription.Close
			} else {
				history = s.orchestrator.RuntimeEventSnapshot(topic)
				subscription := s.orchestrator.SubscribeRuntimeEvents(topic)
				events = subscription.Events
				closeSubscription = subscription.Close
			}
			defer closeSubscription()
			if err := emit(transport.EventEnvelope(request, "snapshot", map[string]any{"stream": streamName, "topic": topic, "events": history}, false)); err != nil {
				return err
			}
			for {
				select {
				case <-ctx.Done():
					return ctx.Err()
				case event, ok := <-events:
					if !ok {
						return nil
					}
					if err := emit(transport.EventEnvelope(request, "delta", map[string]any{"event": event}, false)); err != nil {
						return err
					}
				}
			}
		})
	}
	registerEventStream("runtime.events.subscribe", "runtime", false)
	registerEventStream("providers.inventory.subscribe", "inventory", true)
	registerEventStream("providers.runtime_inventory.subscribe", "inventory", true)
	registerEventStream("providers.runtime_inventory.provider.subscribe", "inventory", true)
	registerEventStream("providers.openai.runtime_inventory.subscribe", "inventory", true)
	registerEventStream("providers.codexsale.runtime_inventory.subscribe", "inventory", true)
	registerEventStream("providers.models.index.subscribe", "inventory", true)
	registerEventStream("diagnostics.subscribe", "runtime", false)
	registerEventStream("socraticode.context_compaction.status.subscribe", "runtime", false)
	registerSourcecraftStream := func(action string) {
		dispatcher.Register(action, "stream", true, func(ctx context.Context, request transport.Envelope, emit transport.Emitter) error {
			payload, err := submit(ctx, request)
			if err != nil {
				return err
			}
			return emit(transport.EventEnvelope(request, "event", payload, true))
		})
	}
	registerSourcecraftStream("sourcecraft.delegate")
	registerSourcecraftStream("sourcecraft.parallel_delegate")
	registerSourcecraftStream("sourcecraft/delegate")
	registerSourcecraftStream("sourcecraft/parallel_delegate")

	return dispatcher
}

func (s *Server) handleControlWebSocket(w http.ResponseWriter, r *http.Request) {
	s.handleWebSocket(w, r, false, "")
}

func (s *Server) handleChatWebSocket(w http.ResponseWriter, r *http.Request) {
	s.handleWebSocket(w, r, true, "")
}

func (s *Server) handleRuntimeWebSocket(w http.ResponseWriter, r *http.Request) {
	s.handleWebSocket(w, r, false, "runtime.events.subscribe")
}

func (s *Server) handleInventoryWebSocket(w http.ResponseWriter, r *http.Request) {
	s.handleWebSocket(w, r, false, "providers.inventory.subscribe")
}

func (s *Server) handleWebSocket(w http.ResponseWriter, r *http.Request, normalizeChat bool, automaticAction string) {
	session := transport.NewSession(transport.DefaultWSSubprotocols, 5*time.Second, 30*time.Second, nil, nil, nil)
	handshake, err := session.Accept(r.Context(), r.Header)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	conn, err := transport.UpgradeWebSocket(w, r, handshake.Subprotocol)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer conn.Close()
	defer session.Close(context.Background())

	connectionCtx, cancelConnection := context.WithCancel(r.Context())
	defer cancelConnection()
	go func() {
		<-connectionCtx.Done()
		_ = conn.Close()
	}()

	send := func(frame transport.Envelope) error {
		ctx, cancel := context.WithTimeout(connectionCtx, session.SendTimeout())
		defer cancel()
		return conn.WriteJSON(ctx, frame)
	}

	if err := send(transport.Envelope{
		Type:          "system",
		RequestID:     handshake.SessionID,
		CorrelationID: handshake.SessionID,
		Action:        "kernel.version",
		Data: map[string]any{
			"kernel_version": buildinfo.Snapshot(),
			"session_id":     handshake.SessionID,
			"subprotocol":    handshake.Subprotocol,
		},
	}); err != nil {
		return
	}

	go func() {
		ticker := time.NewTicker(session.HeartbeatInterval())
		defer ticker.Stop()
		for {
			select {
			case <-connectionCtx.Done():
				return
			case <-ticker.C:
				if err := send(session.HeartbeatEnvelope()); err != nil {
					cancelConnection()
					return
				}
			}
		}
	}()

	if automaticAction != "" {
		request := transport.Envelope{Type: "subscribe", RequestID: handshake.SessionID, CorrelationID: handshake.SessionID, Action: automaticAction, Ack: false}
		s.startDispatch(connectionCtx, session, request, send)
	}

	for {
		raw, err := conn.ReadMessage(connectionCtx)
		if err != nil {
			if !errors.Is(err, io.EOF) && connectionCtx.Err() == nil {
				_ = send(transport.ErrorEnvelope(transport.Envelope{RequestID: handshake.SessionID}, "INVALID_FRAME", err.Error(), nil))
			}
			return
		}
		envelope, err := transport.ParseEnvelope(raw, normalizeChat)
		if err != nil {
			s.recordWebsocketAudit(r.URL.Path, r.RemoteAddr, handshake.SessionID, normalizeChat, automaticAction, raw, nil, err, "parse_error")
			_ = send(transport.ErrorEnvelope(transport.Envelope{RequestID: handshake.SessionID}, transport.ErrorCode(err), err.Error(), nil))
			continue
		}
		var rawFields map[string]json.RawMessage
		if json.Unmarshal(raw, &rawFields) == nil {
			if _, present := rawFields["ack"]; !present {
				envelope.Ack = true
			}
		}
		s.recordWebsocketAudit(r.URL.Path, r.RemoteAddr, handshake.SessionID, normalizeChat, automaticAction, raw, &envelope, nil, "accepted")
		if response, handled, controlErr := session.HandleControlFrame(connectionCtx, envelope); handled {
			if controlErr != nil {
				_ = send(transport.ErrorEnvelope(envelope, transport.ErrorCode(controlErr), controlErr.Error(), nil))
			} else {
				_ = send(response)
			}
			continue
		}
		s.startDispatch(connectionCtx, session, envelope, send)
	}
}

func (s *Server) startDispatch(ctx context.Context, session *transport.Session, request transport.Envelope, send transport.Emitter) {
	requestCtx, cancel := context.WithCancel(ctx)
	if err := session.TrackRequest(request.RequestID, cancel); err != nil {
		_ = send(transport.ErrorEnvelope(request, "CONFLICT", err.Error(), nil))
		cancel()
		return
	}
	if request.Type == "subscribe" {
		_, _ = session.RegisterSubscription(ctx, transport.SubscriptionBinding{ID: request.RequestID, Topic: request.Action})
	}
	go func() {
		defer cancel()
		defer session.FinishRequest(request.RequestID)
		defer session.RemoveSubscription(request.RequestID)
		_ = s.dispatcher.Dispatch(requestCtx, request, send)
	}()
}

func taskFromTransport(data map[string]any) (domain.Task, error) {
	input := mapField(data, "input")
	contextPayload := mapField(data, "context")
	description := firstStringField(data, "description", "message", "text", "prompt", "objective")
	if description == "" {
		description = firstStringField(input, "description")
	}
	if description == "" {
		return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "task description is required", nil)
	}
	taskType := domain.TaskType(strings.ToLower(firstStringField(data, "type")))
	if taskType == "" {
		taskType = domain.TaskTypeCode
	}
	switch taskType {
	case domain.TaskTypePlan, domain.TaskTypeCode, domain.TaskTypeReview, domain.TaskTypeTest, domain.TaskTypeDocs, domain.TaskTypeFix, domain.TaskTypeResearch:
	default:
		return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "unsupported task type: "+string(taskType), nil)
	}
	priority := domain.Priority(strings.ToLower(firstStringField(data, "priority")))
	if priority == "" {
		priority = domain.PriorityNormal
	}
	switch priority {
	case domain.PriorityLow, domain.PriorityNormal, domain.PriorityHigh, domain.PriorityCritical:
	default:
		return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "unsupported priority: "+string(priority), nil)
	}
	complexity := domain.Complexity(strings.ToLower(firstStringField(data, "complexity")))
	if complexity != "" {
		switch complexity {
		case domain.ComplexityLow, domain.ComplexityMedium, domain.ComplexityHigh, domain.ComplexityCritical:
		default:
			return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "unsupported complexity: "+string(complexity), nil)
		}
	}
	files := stringSliceField(data, "files")
	if len(files) == 0 {
		files = stringSliceField(input, "files")
	}
	constraints := stringSliceField(data, "constraints")
	if len(constraints) == 0 {
		constraints = stringSliceField(input, "constraints")
	}
	acceptance := stringSliceField(data, "acceptance_criteria")
	if len(acceptance) == 0 {
		acceptance = stringSliceField(input, "acceptance_criteria")
	}
	project := firstStringField(data, "project", "workspace")
	if project == "" {
		project = firstStringField(contextPayload, "project")
	}
	repoPath := firstStringField(data, "repo_path", "cwd")
	if repoPath == "" {
		repoPath = firstStringField(contextPayload, "repo_path")
	}
	branch := stringField(data, "branch")
	if branch == "" {
		branch = stringField(contextPayload, "branch")
	}
	checkpointPolicy := strings.ToLower(strings.TrimSpace(stringField(data, "checkpoint_policy")))
	if checkpointPolicy != "" {
		switch checkpointPolicy {
		case "branch":
		default:
			return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "unsupported checkpoint_policy: "+checkpointPolicy, nil)
		}
	}
	reviewDepth := intField(data, "review_depth")
	if reviewDepth < 0 {
		return domain.Task{}, transport.NewProtocolFrameError("BAD_REQUEST", "review_depth must be greater than or equal to zero", nil)
	}
	return domain.Task{
		ID:                 stringField(data, "id"),
		SessionID:          stringField(data, "session_id"),
		ParentTaskID:       stringField(data, "parent_task_id"),
		Type:               taskType,
		Priority:           priority,
		RequiredCapability: stringField(data, "required_capability"),
		AssignedProvider:   firstStringField(data, "provider", "assigned_provider"),
		AssignedModel:      firstStringField(data, "model", "requested_model", "assigned_model"),
		Complexity:         complexity,
		MemoryScope:        stringField(data, "memory_scope"),
		MemoryKeys:         stringSliceField(data, "memory_keys"),
		CachePolicy:        stringField(data, "cache_policy"),
		Input: domain.TaskInput{
			Description:        description,
			Files:              files,
			Constraints:        constraints,
			AcceptanceCriteria: acceptance,
		},
		Context: domain.TaskContext{
			Project:  project,
			RepoPath: repoPath,
			Branch:   branch,
		},
		Dependencies:      stringSliceField(data, "dependencies"),
		BranchID:          stringField(data, "branch_id"),
		DraftLayer:        stringField(data, "draft_layer"),
		CheckpointPolicy:  checkpointPolicy,
		ReviewDepth:       reviewDepth,
		ResumeToken:       stringField(data, "resume_token"),
		EstimatedCost:     float64Field(data, "estimated_cost"),
		ExecutionContract: mapField(data, "execution_contract"),
		RepoFingerprint:   stringField(data, "repo_fingerprint"),
		RoutingHints:      mapField(data, "routing_hints"),
	}, nil
}

func float64Field(data map[string]any, key string) float64 {
	value, ok := data[key]
	if !ok || value == nil {
		return 0
	}
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int64:
		return float64(typed)
	case json.Number:
		parsed, err := typed.Float64()
		if err == nil {
			return parsed
		}
	}
	text := strings.TrimSpace(fmt.Sprint(value))
	if text == "" {
		return 0
	}
	var parsed float64
	_, _ = fmt.Sscanf(text, "%f", &parsed)
	return parsed
}

func intField(data map[string]any, key string) int {
	value, ok := data[key]
	if !ok || value == nil {
		return 0
	}
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case float32:
		return int(typed)
	case int:
		return typed
	case int64:
		return int(typed)
	case json.Number:
		parsed, err := typed.Int64()
		if err == nil {
			return int(parsed)
		}
	}
	text := strings.TrimSpace(fmt.Sprint(value))
	if text == "" {
		return 0
	}
	var parsed int
	_, _ = fmt.Sscanf(text, "%d", &parsed)
	return parsed
}

func decodeTransportEnvelope(data map[string]any) (domain.TaskEnvelope, error) {
	raw, err := json.Marshal(data)
	if err != nil {
		return domain.TaskEnvelope{}, transport.NewProtocolFrameError("BAD_REQUEST", "invalid delivery envelope", nil)
	}
	var envelope domain.TaskEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return domain.TaskEnvelope{}, transport.NewProtocolFrameError("BAD_REQUEST", "invalid delivery envelope", nil)
	}
	if strings.TrimSpace(envelope.TaskID) == "" {
		return domain.TaskEnvelope{}, transport.NewProtocolFrameError("BAD_REQUEST", "task_id is required", nil)
	}
	return envelope, nil
}

func decodeNestedEnvelope(data map[string]any, key string) (domain.TaskEnvelope, error) {
	nested, ok := data[key].(map[string]any)
	if !ok || nested == nil {
		return domain.TaskEnvelope{}, transport.NewProtocolFrameError("BAD_REQUEST", key+" payload is required", nil)
	}
	return decodeTransportEnvelope(nested)
}

func stringField(data map[string]any, key string) string {
	value, ok := data[key]
	if !ok || value == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(value))
}

func firstStringField(data map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := data[key]; ok && value != nil {
			if text := strings.TrimSpace(fmt.Sprint(value)); text != "" {
				return text
			}
		}
	}
	return ""
}

func boolField(data map[string]any, key string) bool {
	value, ok := data[key]
	if !ok {
		return false
	}
	if flag, ok := value.(bool); ok {
		return flag
	}
	switch strings.ToLower(strings.TrimSpace(fmt.Sprint(value))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func stringSliceField(data map[string]any, key string) []string {
	value, ok := data[key]
	if !ok || value == nil {
		return nil
	}
	var result []string
	switch items := value.(type) {
	case []any:
		for _, item := range items {
			if text := strings.TrimSpace(fmt.Sprint(item)); text != "" {
				result = append(result, text)
			}
		}
	case []string:
		for _, item := range items {
			if text := strings.TrimSpace(item); text != "" {
				result = append(result, text)
			}
		}
	case string:
		for _, item := range strings.Split(items, ",") {
			if text := strings.TrimSpace(item); text != "" {
				result = append(result, text)
			}
		}
	}
	return result
}

func mapField(data map[string]any, key string) map[string]any {
	value, _ := data[key].(map[string]any)
	if value == nil {
		return map[string]any{}
	}
	result := make(map[string]any, len(value))
	for field, item := range value {
		result[field] = item
	}
	return result
}
