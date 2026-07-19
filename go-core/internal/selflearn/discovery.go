package selflearn

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type Inventory interface {
	RefreshIfStale(ctx context.Context)
	Snapshot(provider string) (domain.ProviderCatalogSnapshot, bool)
}

type DiscoveryConfig struct {
	Provider        string
	TargetModel     string
	LegacyModel     string
	RefreshInterval time.Duration
}

type DiscoveryService struct {
	mu        sync.RWMutex
	inventory Inventory
	config    DiscoveryConfig
	snapshot  domain.SelfLearningRegistrySnapshot
}

func NewDiscoveryService(inventory Inventory, cfg DiscoveryConfig) *DiscoveryService {
	if strings.TrimSpace(cfg.Provider) == "" {
		cfg.Provider = domain.TargetKernelProvider
	}
	if strings.TrimSpace(cfg.TargetModel) == "" {
		cfg.TargetModel = domain.TargetReasoningModel
	}
	if strings.TrimSpace(cfg.LegacyModel) == "" {
		cfg.LegacyModel = domain.LegacyReasoningModel
	}
	if cfg.RefreshInterval <= 0 {
		cfg.RefreshInterval = 5 * time.Minute
	}
	return &DiscoveryService{inventory: inventory, config: cfg}
}

func (d *DiscoveryService) Start(ctx context.Context) {
	if d == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	go func() {
		_, _ = d.Refresh(ctx)
		ticker := time.NewTicker(d.config.RefreshInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				_, _ = d.Refresh(ctx)
			}
		}
	}()
}

func (d *DiscoveryService) Refresh(ctx context.Context) (domain.SelfLearningRegistrySnapshot, error) {
	if d == nil || d.inventory == nil {
		return domain.SelfLearningRegistrySnapshot{}, fmt.Errorf("selflearn discovery inventory is not configured")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	d.inventory.RefreshIfStale(ctx)
	providerSnapshot, ok := d.inventory.Snapshot(d.config.Provider)
	if !ok {
		return domain.SelfLearningRegistrySnapshot{}, fmt.Errorf("provider snapshot %q not found", d.config.Provider)
	}
	snapshot, err := buildRegistrySnapshot(providerSnapshot, d.config)
	if err != nil {
		return domain.SelfLearningRegistrySnapshot{}, err
	}
	d.mu.Lock()
	d.snapshot = snapshot
	d.mu.Unlock()
	return snapshot, nil
}

func (d *DiscoveryService) Snapshot() domain.SelfLearningRegistrySnapshot {
	if d == nil {
		return domain.SelfLearningRegistrySnapshot{}
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	return cloneRegistrySnapshot(d.snapshot)
}

func buildRegistrySnapshot(providerSnapshot domain.ProviderCatalogSnapshot, cfg DiscoveryConfig) (domain.SelfLearningRegistrySnapshot, error) {
	now := time.Now().UTC()
	result := domain.SelfLearningRegistrySnapshot{
		Provider:    providerSnapshot.Provider,
		TargetModel: cfg.TargetModel,
		ObservedAt:  providerSnapshot.ObservedAt,
		UpdatedAt:   now,
	}
	for _, model := range providerSnapshot.Models {
		candidate := toSelfLearningModel(model)
		if strings.EqualFold(model.ModelName, cfg.LegacyModel) {
			result.LegacyPresent = true
		}
		if !model.Available || !strings.EqualFold(strings.TrimSpace(model.Status), "ready") {
			continue
		}
		if strings.EqualFold(model.ModelName, cfg.TargetModel) {
			result.Preferred = candidate
			continue
		}
		result.Alternatives = append(result.Alternatives, candidate)
	}
	if strings.TrimSpace(result.Preferred.ModelName) == "" {
		return result, fmt.Errorf("target model %q is not registered as ready on provider %q", cfg.TargetModel, providerSnapshot.Provider)
	}
	return result, nil
}

func toSelfLearningModel(status domain.ProviderModelStatus) domain.SelfLearningModel {
	model := domain.SelfLearningModel{
		Provider:      status.Provider,
		ModelName:     status.ModelName,
		Roles:         []domain.SelfLearningRole{domain.SelfLearningRoleReasoning, domain.SelfLearningRoleCoding, domain.SelfLearningRoleCritic, domain.SelfLearningRoleSyntheticData},
		Available:     status.Available,
		Status:        status.Status,
		ObservedAt:    status.ObservedAt,
		Metadata:      cloneMap(status.Metadata),
		HotReloadable: true,
	}
	model.ContextWindow = metadataInt(status.Metadata, "context_window", "context_length", "num_ctx", "n_ctx")
	model.MaxOutputTokens = metadataInt(status.Metadata, "max_output_tokens", "num_predict")
	model.Quantization = metadataString(status.Metadata, "quantization", "quant")
	model.Format = metadataString(status.Metadata, "format", "family")
	if family := metadataString(status.Metadata, "model_family", "family"); family != "" {
		model.Specialization = append(model.Specialization, family)
	}
	return model
}

func metadataInt(metadata map[string]any, keys ...string) int {
	for _, key := range keys {
		value, ok := metadata[key]
		if !ok {
			continue
		}
		switch typed := value.(type) {
		case int:
			return typed
		case int64:
			return int(typed)
		case float64:
			return int(typed)
		case string:
			if parsed, err := strconv.Atoi(strings.TrimSpace(typed)); err == nil {
				return parsed
			}
		}
	}
	return 0
}

func metadataString(metadata map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := metadata[key]; ok {
			text := strings.TrimSpace(fmt.Sprint(value))
			if text != "" && text != "<nil>" {
				return text
			}
		}
	}
	return ""
}

func cloneMap(metadata map[string]any) map[string]any {
	if len(metadata) == 0 {
		return nil
	}
	cloned := make(map[string]any, len(metadata))
	for key, value := range metadata {
		cloned[key] = value
	}
	return cloned
}

func cloneRegistrySnapshot(snapshot domain.SelfLearningRegistrySnapshot) domain.SelfLearningRegistrySnapshot {
	snapshot.Preferred.Metadata = cloneMap(snapshot.Preferred.Metadata)
	snapshot.Preferred.Roles = append([]domain.SelfLearningRole(nil), snapshot.Preferred.Roles...)
	snapshot.Preferred.Specialization = append([]string(nil), snapshot.Preferred.Specialization...)
	if len(snapshot.Alternatives) == 0 {
		return snapshot
	}
	cloned := make([]domain.SelfLearningModel, 0, len(snapshot.Alternatives))
	for _, model := range snapshot.Alternatives {
		model.Metadata = cloneMap(model.Metadata)
		model.Roles = append([]domain.SelfLearningRole(nil), model.Roles...)
		model.Specialization = append([]string(nil), model.Specialization...)
		cloned = append(cloned, model)
	}
	snapshot.Alternatives = cloned
	return snapshot
}
