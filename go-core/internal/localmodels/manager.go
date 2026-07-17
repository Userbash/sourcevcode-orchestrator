package localmodels

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type Record struct {
	Provider         string  `json:"provider"`
	ModelName        string  `json:"model_name"`
	EstimatedMemory  float64 `json:"estimated_memory_gb"`
	ActualMemory     float64 `json:"actual_memory_gb"`
	Resident         bool    `json:"resident"`
	Warmups          int     `json:"warmups"`
	Evictions        int     `json:"evictions"`
	LastError        string  `json:"last_error,omitempty"`
	LastAction       string  `json:"last_action"`
	LastObservedAt   string  `json:"last_observed_at,omitempty"`
	LastTransitionAt string  `json:"last_transition_at,omitempty"`
}

type Manager struct {
	runtime *Runtime

	mu      sync.RWMutex
	records map[string]*Record
}

func NewManager(runtime *Runtime) *Manager {
	manager := &Manager{
		runtime: runtime,
		records: map[string]*Record{},
	}
	manager.touch(runtime.config.ModelName)
	return manager
}

func (m *Manager) Info() domain.ModuleInfo {
	return domain.ModuleInfo{
		Name: "local_model_manager",
		Kind: "runtime",
		Metadata: map[string]any{
			"provider": "local_llm",
			"runtime":  "go-core",
		},
	}
}

func (m *Manager) Snapshot() map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.snapshotLocked()
}

func (m *Manager) Residents(ctx context.Context) (map[string]any, error) {
	if err := m.Sync(ctx); err != nil {
		return nil, err
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	return map[string]any{
		"status":  "ok",
		"data":    residentRecords(m.records),
		"runtime": runtimeStatus(m.records),
	}, nil
}

func (m *Manager) Connect(ctx context.Context, modelName string) (map[string]any, error) {
	target := effectiveModel(modelName, m.runtime.config.ModelName)
	endpoint, err := m.runtime.PullModel(ctx, target)
	if err != nil {
		m.markFailure(target, "connect_failed", err)
		return nil, err
	}
	if _, err := m.runtime.WarmModel(ctx, target); err != nil {
		m.markFailure(target, "warm_failed", err)
		return nil, err
	}
	if err := m.Sync(ctx); err != nil {
		return nil, err
	}
	m.mu.Lock()
	record := m.touch(target)
	record.Warmups++
	record.LastAction = "connected"
	record.LastTransitionAt = time.Now().UTC().Format(time.RFC3339)
	record.LastError = ""
	m.mu.Unlock()
	return map[string]any{"status": "ok", "model_name": target, "endpoint": endpoint, "snapshot": m.Snapshot()}, nil
}

func (m *Manager) Warm(ctx context.Context, modelName string) (map[string]any, error) {
	target := effectiveModel(modelName, m.runtime.config.ModelName)
	endpoint, err := m.runtime.WarmModel(ctx, target)
	if err != nil {
		m.markFailure(target, "warm_failed", err)
		return nil, err
	}
	if err := m.Sync(ctx); err != nil {
		return nil, err
	}
	m.mu.Lock()
	record := m.touch(target)
	record.Warmups++
	record.LastAction = "warmed"
	record.LastTransitionAt = time.Now().UTC().Format(time.RFC3339)
	record.LastError = ""
	m.mu.Unlock()
	return map[string]any{"status": "ok", "model_name": target, "endpoint": endpoint, "snapshot": m.Snapshot()}, nil
}

func (m *Manager) Disconnect(ctx context.Context, modelName string) (map[string]any, error) {
	target := effectiveModel(modelName, m.runtime.config.ModelName)
	endpoint, err := m.runtime.UnloadModel(ctx, target)
	if err != nil {
		m.markFailure(target, "disconnect_failed", err)
		return nil, err
	}
	if err := m.Sync(ctx); err != nil {
		return nil, err
	}
	m.mu.Lock()
	record := m.touch(target)
	record.Evictions++
	record.LastAction = "disconnected"
	record.LastTransitionAt = time.Now().UTC().Format(time.RFC3339)
	record.LastError = ""
	m.mu.Unlock()
	return map[string]any{"status": "ok", "model_name": target, "endpoint": endpoint, "snapshot": m.Snapshot()}, nil
}

func (m *Manager) Sync(ctx context.Context) error {
	residents, _, err := m.runtime.ListResidentModels(ctx)
	now := time.Now().UTC().Format(time.RFC3339)
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, record := range m.records {
		record.Resident = false
		record.ActualMemory = 0
	}
	if err != nil {
		for _, record := range m.records {
			record.LastError = err.Error()
			record.LastAction = "probe_failed"
			record.LastObservedAt = now
		}
		return err
	}
	seen := map[string]struct{}{}
	for _, resident := range residents {
		record := m.touch(resident.Name)
		record.Resident = true
		record.EstimatedMemory = residentMemoryGB(resident)
		record.ActualMemory = residentMemoryGB(resident)
		record.LastAction = "resident_probe"
		record.LastObservedAt = now
		record.LastError = ""
		record.Provider = "local"
		seen[resident.Name] = struct{}{}
	}
	for name, record := range m.records {
		if _, ok := seen[name]; ok {
			continue
		}
		record.LastObservedAt = now
		if record.LastAction == "" {
			record.LastAction = "observed"
		}
	}
	return nil
}

func (m *Manager) touch(modelName string) *Record {
	key := strings.TrimSpace(modelName)
	record := m.records[key]
	if record == nil {
		record = &Record{
			Provider:        "local",
			ModelName:       key,
			EstimatedMemory: 0,
			LastAction:      "observed",
		}
		m.records[key] = record
	}
	return record
}

func (m *Manager) markFailure(modelName string, action string, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	record := m.touch(modelName)
	record.LastError = err.Error()
	record.LastAction = action
	record.LastObservedAt = time.Now().UTC().Format(time.RFC3339)
}

func (m *Manager) snapshotLocked() map[string]any {
	models := make([]map[string]any, 0, len(m.records))
	residents := make([]map[string]any, 0)
	blocked := make([]string, 0)
	warmups := 0
	evictions := 0
	residentMemory := 0.0
	for _, record := range m.records {
		row := map[string]any{
			"provider":            record.Provider,
			"model_name":          record.ModelName,
			"estimated_memory_gb": record.EstimatedMemory,
			"actual_memory_gb":    record.ActualMemory,
			"resident":            record.Resident,
			"warmups":             record.Warmups,
			"evictions":           record.Evictions,
			"last_action":         record.LastAction,
			"last_error":          record.LastError,
			"last_observed_at":    record.LastObservedAt,
			"last_transition_at":  record.LastTransitionAt,
		}
		models = append(models, row)
		warmups += record.Warmups
		evictions += record.Evictions
		if record.Resident {
			residents = append(residents, row)
			residentMemory += record.ActualMemory
		}
		if record.LastError != "" {
			blocked = append(blocked, record.ModelName)
		}
	}
	sort.Slice(models, func(i, j int) bool {
		return stringValue(models[i]["model_name"]) < stringValue(models[j]["model_name"])
	})
	sort.Slice(residents, func(i, j int) bool {
		return stringValue(residents[i]["model_name"]) < stringValue(residents[j]["model_name"])
	})
	sort.Strings(blocked)
	return map[string]any{
		"status":             runtimeStatus(m.records),
		"resident_models":    residents,
		"blocked_models":     blocked,
		"models":             models,
		"warmups":            warmups,
		"evictions":          evictions,
		"memory_pressure":    false,
		"resident_memory_gb": residentMemory,
	}
}

func residentRecords(records map[string]*Record) []map[string]any {
	rows := make([]map[string]any, 0)
	for _, record := range records {
		if !record.Resident {
			continue
		}
		rows = append(rows, map[string]any{
			"name":                record.ModelName,
			"model_name":          record.ModelName,
			"actual_memory_gb":    record.ActualMemory,
			"estimated_memory_gb": record.EstimatedMemory,
			"last_action":         record.LastAction,
		})
	}
	sort.Slice(rows, func(i, j int) bool {
		return stringValue(rows[i]["model_name"]) < stringValue(rows[j]["model_name"])
	})
	return rows
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func runtimeStatus(records map[string]*Record) string {
	for _, record := range records {
		if record.LastError != "" {
			return "degraded"
		}
	}
	for _, record := range records {
		if record.Resident {
			return "ready"
		}
	}
	return "idle"
}

func residentMemoryGB(model ResidentModel) float64 {
	size := model.SizeVRAM
	if size == 0 {
		size = model.Size
	}
	if size == 0 {
		return 0
	}
	return float64(size) / (1024 * 1024 * 1024)
}
