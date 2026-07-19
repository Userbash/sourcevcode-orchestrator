package observability

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type Entry struct {
	Timestamp time.Time      `json:"timestamp"`
	Level     string         `json:"level"`
	Message   string         `json:"message"`
	Fields    map[string]any `json:"fields,omitempty"`
}

type Config struct {
	Service    string
	Level      slog.Level
	Format     string
	AddSource  bool
	FilePath   string
	BufferSize int
}

type Manager struct {
	logger *slog.Logger
	buffer *ringBuffer
}

type ringBuffer struct {
	mu     sync.RWMutex
	size   int
	next   int
	filled bool
	items  []Entry
}

type captureHandler struct {
	inner  slog.Handler
	buffer *ringBuffer
	attrs  []slog.Attr
	groups []string
}

type stdlibBridge struct{}

var globalManager atomic.Pointer[Manager]

func Init(service string) *Manager {
	cfg := Config{
		Service:    strings.TrimSpace(service),
		Level:      parseLevel(envOrDefault("GO_CORE_LOG_LEVEL", "info")),
		Format:     strings.ToLower(envOrDefault("GO_CORE_LOG_FORMAT", "json")),
		AddSource:  envBool("GO_CORE_LOG_ADD_SOURCE"),
		FilePath:   strings.TrimSpace(os.Getenv("GO_CORE_LOG_PATH")),
		BufferSize: envInt("GO_CORE_LOG_BUFFER_SIZE", 1000),
	}
	if cfg.Service == "" {
		cfg.Service = "go_core"
	}
	if cfg.BufferSize < 100 {
		cfg.BufferSize = 100
	}
	manager := newManager(cfg)
	globalManager.Store(manager)
	slog.SetDefault(manager.logger)
	log.SetFlags(0)
	log.SetOutput(stdlibBridge{})
	return manager
}

func Default() *Manager {
	manager := globalManager.Load()
	if manager != nil {
		return manager
	}
	return Init("go_core")
}

func Logger(component string) *slog.Logger {
	component = strings.TrimSpace(component)
	if component == "" {
		return Default().logger
	}
	return Default().logger.With("component", component)
}

func Recent(limit int) []Entry {
	return Default().Recent(limit)
}

func Diagnostics(limit int) map[string]any {
	return Default().Diagnostics(limit)
}

func (m *Manager) Recent(limit int) []Entry {
	if m == nil || m.buffer == nil {
		return nil
	}
	return m.buffer.snapshot(limit)
}

func (m *Manager) Diagnostics(limit int) map[string]any {
	entries := m.Recent(limit)
	levelCounts := map[string]int{}
	componentCounts := map[string]int{}
	errorSamples := []map[string]any{}
	timeoutCount := 0
	wsFailureCount := 0
	providerIssueCount := 0
	for _, entry := range entries {
		levelCounts[entry.Level]++
		if component, _ := entry.Fields["component"].(string); component != "" {
			componentCounts[component]++
		}
		message := strings.ToLower(entry.Message)
		if strings.Contains(message, "timeout") || strings.Contains(message, "deadline exceeded") {
			timeoutCount++
		}
		if strings.Contains(message, "websocket") && entry.Level == "ERROR" {
			wsFailureCount++
		}
		if strings.Contains(message, "provider") || strings.Contains(message, "registry") || strings.Contains(message, "model") {
			if entry.Level == "WARN" || entry.Level == "ERROR" {
				providerIssueCount++
			}
		}
		if entry.Level == "ERROR" && len(errorSamples) < 10 {
			errorSamples = append(errorSamples, map[string]any{
				"timestamp": entry.Timestamp,
				"message":   entry.Message,
				"fields":    entry.Fields,
			})
		}
	}
	recommendations := []string{}
	if timeoutCount > 0 {
		recommendations = append(recommendations, "Recent timeouts detected. Check upstream provider latency, dispatcher backpressure, and request deadlines.")
	}
	if wsFailureCount > 0 {
		recommendations = append(recommendations, "WebSocket failures detected. Inspect /transport/audit and recent websocket frames for malformed envelopes or dispatch stalls.")
	}
	if providerIssueCount > 0 {
		recommendations = append(recommendations, "Provider or model registry issues detected. Recheck provider health TTL/cooldowns and upstream model registration state.")
	}
	if len(recommendations) == 0 {
		recommendations = append(recommendations, "No dominant failure pattern detected in the recent log window.")
	}
	return map[string]any{
		"status":           "ok",
		"generated_at":     time.Now().UTC(),
		"entries":          entries,
		"level_counts":     levelCounts,
		"component_counts": componentCounts,
		"error_samples":    errorSamples,
		"recommendations":  recommendations,
	}
}

func (w stdlibBridge) Write(p []byte) (int, error) {
	message := strings.TrimSpace(string(p))
	if message == "" {
		return len(p), nil
	}
	Logger("stdlib").Info(message)
	return len(p), nil
}

func newManager(cfg Config) *Manager {
	writer := io.Writer(os.Stdout)
	if fileWriter := openLogFile(cfg.FilePath); fileWriter != nil {
		writer = io.MultiWriter(os.Stdout, fileWriter)
	}
	options := &slog.HandlerOptions{Level: cfg.Level, AddSource: cfg.AddSource}
	var handler slog.Handler
	if cfg.Format == "text" {
		handler = slog.NewTextHandler(writer, options)
	} else {
		handler = slog.NewJSONHandler(writer, options)
	}
	buffer := newRingBuffer(cfg.BufferSize)
	logger := slog.New(&captureHandler{
		inner:  handler,
		buffer: buffer,
		attrs: []slog.Attr{
			slog.String("service", cfg.Service),
		},
	})
	return &Manager{logger: logger, buffer: buffer}
}

func openLogFile(path string) io.Writer {
	if strings.TrimSpace(path) == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil
	}
	return file
}

func newRingBuffer(size int) *ringBuffer {
	return &ringBuffer{size: size, items: make([]Entry, size)}
}

func (b *ringBuffer) append(entry Entry) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.items[b.next] = entry
	b.next = (b.next + 1) % b.size
	if b.next == 0 {
		b.filled = true
	}
}

func (b *ringBuffer) snapshot(limit int) []Entry {
	b.mu.RLock()
	defer b.mu.RUnlock()
	count := b.next
	if b.filled {
		count = b.size
	}
	if count == 0 {
		return nil
	}
	if limit <= 0 || limit > count {
		limit = count
	}
	out := make([]Entry, 0, limit)
	start := 0
	if b.filled {
		start = b.next
	}
	for i := count - limit; i < count; i++ {
		index := (start + i) % b.size
		out = append(out, cloneEntry(b.items[index]))
	}
	return out
}

func cloneEntry(entry Entry) Entry {
	cloned := Entry{
		Timestamp: entry.Timestamp,
		Level:     entry.Level,
		Message:   entry.Message,
	}
	if len(entry.Fields) > 0 {
		cloned.Fields = make(map[string]any, len(entry.Fields))
		for key, value := range entry.Fields {
			cloned.Fields[key] = value
		}
	}
	return cloned
}

func (h *captureHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.inner.Enabled(ctx, level)
}

func (h *captureHandler) Handle(ctx context.Context, record slog.Record) error {
	entry := Entry{
		Timestamp: record.Time.UTC(),
		Level:     record.Level.String(),
		Message:   record.Message,
		Fields:    map[string]any{},
	}
	for _, attr := range h.attrs {
		appendAttr(entry.Fields, h.groups, attr)
	}
	record.Attrs(func(attr slog.Attr) bool {
		appendAttr(entry.Fields, h.groups, attr)
		return true
	})
	h.buffer.append(entry)
	return h.inner.Handle(ctx, record)
}

func (h *captureHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	combined := append([]slog.Attr{}, h.attrs...)
	combined = append(combined, attrs...)
	return &captureHandler{
		inner:  h.inner.WithAttrs(attrs),
		buffer: h.buffer,
		attrs:  combined,
		groups: append([]string{}, h.groups...),
	}
}

func (h *captureHandler) WithGroup(name string) slog.Handler {
	groups := append([]string{}, h.groups...)
	if strings.TrimSpace(name) != "" {
		groups = append(groups, name)
	}
	return &captureHandler{
		inner:  h.inner.WithGroup(name),
		buffer: h.buffer,
		attrs:  append([]slog.Attr{}, h.attrs...),
		groups: groups,
	}
}

func appendAttr(fields map[string]any, groups []string, attr slog.Attr) {
	if attr.Equal(slog.Attr{}) {
		return
	}
	key := strings.TrimSpace(attr.Key)
	if key == "" {
		return
	}
	if len(groups) > 0 {
		key = strings.Join(append(append([]string{}, groups...), key), ".")
	}
	fields[key] = attrValue(attr.Value)
}

func attrValue(value slog.Value) any {
	switch value.Kind() {
	case slog.KindString:
		return value.String()
	case slog.KindInt64:
		return value.Int64()
	case slog.KindUint64:
		return value.Uint64()
	case slog.KindFloat64:
		return value.Float64()
	case slog.KindBool:
		return value.Bool()
	case slog.KindDuration:
		return value.Duration().String()
	case slog.KindTime:
		return value.Time().UTC()
	case slog.KindGroup:
		group := map[string]any{}
		for _, attr := range value.Group() {
			group[attr.Key] = attrValue(attr.Value)
		}
		return group
	case slog.KindAny:
		anyValue := value.Any()
		if payload, err := json.Marshal(anyValue); err == nil {
			var cloned any
			if json.Unmarshal(payload, &cloned) == nil {
				return cloned
			}
		}
		return anyValue
	default:
		return value.String()
	}
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envBool(key string) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	return value == "1" || value == "true" || value == "yes" || value == "on"
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	var out int
	if _, err := fmt.Sscanf(value, "%d", &out); err == nil && out > 0 {
		return out
	}
	return fallback
}

func parseLevel(raw string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
