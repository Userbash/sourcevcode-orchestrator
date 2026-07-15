package transport

import (
	"context"
	"errors"
	"sort"
	"strings"
	"sync"
	"time"
)

type Emitter func(Envelope) error
type DispatchHandler func(context.Context, Envelope, Emitter) error

type HandlerRegistration struct {
	Handler DispatchHandler
	Mode    string
	SendAck bool
}

type Dispatcher struct {
	mu       sync.RWMutex
	handlers map[string]HandlerRegistration
}

func NewDispatcher() *Dispatcher {
	return &Dispatcher{handlers: make(map[string]HandlerRegistration)}
}

func (d *Dispatcher) Register(action, mode string, sendAck bool, handler DispatchHandler) {
	action = strings.ToLower(strings.TrimSpace(action))
	if action == "" || handler == nil {
		return
	}
	if mode != "stream" {
		mode = "single"
	}
	d.mu.Lock()
	d.handlers[action] = HandlerRegistration{Handler: handler, Mode: mode, SendAck: sendAck}
	d.mu.Unlock()
}

func (d *Dispatcher) RegisterSingle(action string, sendAck bool, handler func(context.Context, Envelope) (map[string]any, error)) {
	d.Register(action, "single", sendAck, func(ctx context.Context, request Envelope, emit Emitter) error {
		payload, err := handler(ctx, request)
		if err != nil {
			return err
		}
		return emit(ResponseEnvelope(request, payload))
	})
}

func (d *Dispatcher) Supports(action string) bool {
	d.mu.RLock()
	_, ok := d.handlers[strings.ToLower(strings.TrimSpace(action))]
	d.mu.RUnlock()
	return ok
}

func (d *Dispatcher) Actions() []string {
	d.mu.RLock()
	actions := make([]string, 0, len(d.handlers))
	for action := range d.handlers {
		actions = append(actions, action)
	}
	d.mu.RUnlock()
	sort.Strings(actions)
	return actions
}

func (d *Dispatcher) Dispatch(ctx context.Context, request Envelope, emit Emitter) error {
	d.mu.RLock()
	registration, ok := d.handlers[strings.ToLower(strings.TrimSpace(request.Action))]
	d.mu.RUnlock()
	if !ok {
		return emit(ErrorEnvelope(request, "UNSUPPORTED_ACTION", "unsupported action: "+request.Action, nil))
	}
	if request.Ack && registration.SendAck {
		if err := emit(AckEnvelope(request, registration.Mode)); err != nil {
			return err
		}
	}
	requestCtx := ctx
	cancel := func() {}
	if request.TimeoutMS > 0 {
		requestCtx, cancel = context.WithTimeout(ctx, time.Duration(request.TimeoutMS)*time.Millisecond)
	}
	defer cancel()

	err := registration.Handler(requestCtx, request, emit)
	if err == nil {
		return nil
	}
	code := ErrorCode(err)
	if errors.Is(err, context.DeadlineExceeded) {
		code = "TIMEOUT"
	} else if errors.Is(err, context.Canceled) {
		code = "CANCELED"
	}
	return emit(ErrorEnvelope(request, code, err.Error(), map[string]any{"handler": request.Action}))
}
