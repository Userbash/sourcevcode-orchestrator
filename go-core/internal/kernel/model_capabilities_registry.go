package kernel

import (
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ModelCapabilitiesRegistry struct {
	providerRegistry *ProviderModelRegistry
}

func NewModelCapabilitiesRegistry(providerRegistry *ProviderModelRegistry) *ModelCapabilitiesRegistry {
	return &ModelCapabilitiesRegistry{providerRegistry: providerRegistry}
}

func (r *ModelCapabilitiesRegistry) Lookup(provider string, modelName string) (domain.ModelCapabilities, bool) {
	modelName = strings.TrimSpace(modelName)
	if modelName == "" {
		return domain.ModelCapabilities{}, false
	}
	if r == nil || r.providerRegistry == nil {
		return inferModelCapabilities(modelName), true
	}
	snapshot, ok := r.providerRegistry.Snapshot(provider)
	if !ok {
		return inferModelCapabilities(modelName), true
	}
	for _, model := range snapshot.Models {
		if strings.EqualFold(strings.TrimSpace(model.ModelName), modelName) {
			return modelCapabilitiesFromMetadata(model.ModelName, model.Metadata), true
		}
	}
	return inferModelCapabilities(modelName), true
}

func (r *ModelCapabilitiesRegistry) Snapshot() map[string]map[string]domain.ModelCapabilities {
	result := map[string]map[string]domain.ModelCapabilities{}
	if r == nil || r.providerRegistry == nil {
		return result
	}
	for _, snapshot := range r.providerRegistry.Snapshots() {
		models := map[string]domain.ModelCapabilities{}
		for _, model := range snapshot.Models {
			models[model.ModelName] = modelCapabilitiesFromMetadata(model.ModelName, model.Metadata)
		}
		result[snapshot.Provider] = models
	}
	return result
}
