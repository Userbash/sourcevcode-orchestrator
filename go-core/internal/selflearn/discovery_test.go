package selflearn

import (
	"context"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type stubInventory struct {
	snapshot  domain.ProviderCatalogSnapshot
	refresh   int
	refreshFn func(context.Context)
}

func (s *stubInventory) RefreshIfStale(ctx context.Context) {
	s.refresh++
	if s.refreshFn != nil {
		s.refreshFn(ctx)
	}
}

func (s *stubInventory) Snapshot(provider string) (domain.ProviderCatalogSnapshot, bool) {
	if provider != s.snapshot.Provider {
		return domain.ProviderCatalogSnapshot{}, false
	}
	return s.snapshot, true
}

func TestDiscoveryServiceRefreshFindsTargetModel(t *testing.T) {
	now := time.Now().UTC()
	inventory := &stubInventory{
		snapshot: domain.ProviderCatalogSnapshot{
			Provider:   "ai_kernel",
			ObservedAt: now,
			Models: []domain.ProviderModelStatus{
				{Provider: "ai_kernel", ModelName: domain.LegacyReasoningModel, Available: true, Status: "ready", ObservedAt: now},
				{Provider: "ai_kernel", ModelName: domain.TargetReasoningModel, Available: true, Status: "ready", ObservedAt: now, Metadata: map[string]any{"context_window": 32768, "model_family": "gemma"}},
			},
		},
	}
	service := NewDiscoveryService(inventory, DiscoveryConfig{})
	snapshot, err := service.Refresh(context.Background())
	if err != nil {
		t.Fatalf("Refresh() error = %v", err)
	}
	if inventory.refresh != 1 {
		t.Fatalf("RefreshIfStale() called %d times, want 1", inventory.refresh)
	}
	if snapshot.Preferred.ModelName != domain.TargetReasoningModel {
		t.Fatalf("preferred model = %q, want %q", snapshot.Preferred.ModelName, domain.TargetReasoningModel)
	}
	if !snapshot.LegacyPresent {
		t.Fatal("expected legacy model to be detected")
	}
	if snapshot.Preferred.ContextWindow != 32768 {
		t.Fatalf("context window = %d, want 32768", snapshot.Preferred.ContextWindow)
	}
}

func TestDiscoveryServiceRefreshFailsWhenTargetMissing(t *testing.T) {
	now := time.Now().UTC()
	inventory := &stubInventory{
		snapshot: domain.ProviderCatalogSnapshot{
			Provider:   "ai_kernel",
			ObservedAt: now,
			Models: []domain.ProviderModelStatus{
				{Provider: "ai_kernel", ModelName: "qwen2.5:0.5b", Available: true, Status: "ready", ObservedAt: now},
			},
		},
	}
	service := NewDiscoveryService(inventory, DiscoveryConfig{})
	if _, err := service.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh() error = nil, want target model missing error")
	}
}

func TestDiscoveryServiceStartDoesNotBlockOnInitialRefresh(t *testing.T) {
	block := make(chan struct{})
	inventory := &stubInventory{
		snapshot: domain.ProviderCatalogSnapshot{Provider: "ai_kernel"},
		refreshFn: func(ctx context.Context) {
			select {
			case <-block:
			case <-ctx.Done():
			}
		},
	}
	service := NewDiscoveryService(inventory, DiscoveryConfig{RefreshInterval: time.Hour})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	startedAt := time.Now()
	service.Start(ctx)
	if elapsed := time.Since(startedAt); elapsed > 100*time.Millisecond {
		t.Fatalf("Start() blocked for %s", elapsed)
	}

	close(block)
}
