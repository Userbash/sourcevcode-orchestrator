package modules

import "sourcevcode-orchestrator/go-core/internal/domain"

type Module interface {
	Info() domain.ModuleInfo
	Snapshot() map[string]any
}

type BasicModule struct {
	info domain.ModuleInfo
}

func NewBasicModule(name string, kind string, metadata map[string]any) *BasicModule {
	return &BasicModule{
		info: domain.ModuleInfo{
			Name:     name,
			Kind:     kind,
			Metadata: cloneMap(metadata),
		},
	}
}

func (m *BasicModule) Info() domain.ModuleInfo {
	return domain.ModuleInfo{
		Name:     m.info.Name,
		Kind:     m.info.Kind,
		Metadata: cloneMap(m.info.Metadata),
	}
}

func (m *BasicModule) Snapshot() map[string]any {
	return map[string]any{
		"name":     m.info.Name,
		"kind":     m.info.Kind,
		"metadata": cloneMap(m.info.Metadata),
	}
}

func cloneMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
