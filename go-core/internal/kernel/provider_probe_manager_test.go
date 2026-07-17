package kernel

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type stubHealthReporter struct {
	count  atomic.Int32
	health domain.ProviderHealth
	delay  time.Duration
}

func (s *stubHealthReporter) Probe(ctx context.Context) domain.ProviderHealth {
	s.count.Add(1)
	if s.delay > 0 {
		select {
		case <-time.After(s.delay):
		case <-ctx.Done():
			return domain.ProviderHealth{
				Provider:   s.health.Provider,
				Configured: s.health.Configured,
				Status:     "degraded",
				Error:      ctx.Err().Error(),
				ObservedAt: time.Now().UTC(),
			}
		}
	}
	health := s.health
	if health.ObservedAt.IsZero() {
		health.ObservedAt = time.Now().UTC()
	}
	return health
}

func waitForCondition(t *testing.T, timeout time.Duration, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition was not met before timeout")
}

func TestProviderProbeManagerQueuesAndCachesSuccessfulProbe(t *testing.T) {
	t.Setenv("GO_CORE_PROVIDER_HEALTH_WORKERS", "1")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE", "4")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_TTL", "100ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_COOLDOWN", "20ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN", "40ms")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reporter := &stubHealthReporter{health: domain.ProviderHealth{
		Provider:   "test",
		Configured: true,
		Available:  true,
		Status:     "ready",
	}}
	manager := newProviderProbeManager(ctx)
	fallback := domain.ProviderHealth{Provider: "test", Configured: true, Status: "configured", ObservedAt: time.Now().UTC()}

	health := manager.Observe("test", fallback, reporter, true)
	if !health.ProbeQueued {
		t.Fatalf("expected initial probe to be queued, got %#v", health)
	}
	waitForCondition(t, 300*time.Millisecond, func() bool { return reporter.count.Load() == 1 })

	health = manager.Observe("test", fallback, reporter, true)
	if health.Status != "ready" || !health.Available {
		t.Fatalf("expected cached ready health, got %#v", health)
	}
	if health.ProbeQueued {
		t.Fatalf("did not expect a second queued probe within ttl, got %#v", health)
	}
	if health.RefreshAfter == nil {
		t.Fatalf("expected refresh_after to be set after successful probe, got %#v", health)
	}
	if reporter.count.Load() != 1 {
		t.Fatalf("expected one probe execution within ttl, got %d", reporter.count.Load())
	}
}

func TestProviderProbeManagerAppliesRateLimitCooldown(t *testing.T) {
	t.Setenv("GO_CORE_PROVIDER_HEALTH_WORKERS", "1")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE", "4")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_TTL", "20ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_COOLDOWN", "20ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN", "60ms")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reporter := &stubHealthReporter{health: domain.ProviderHealth{
		Provider:   "test",
		Configured: true,
		Status:     "rate_limited",
		Error:      "429 too many requests",
	}}
	manager := newProviderProbeManager(ctx)
	fallback := domain.ProviderHealth{Provider: "test", Configured: true, Status: "configured", ObservedAt: time.Now().UTC()}

	manager.Observe("test", fallback, reporter, true)
	waitForCondition(t, 300*time.Millisecond, func() bool { return reporter.count.Load() == 1 })

	health := manager.Observe("test", fallback, reporter, true)
	if health.Status != "rate_limited" {
		t.Fatalf("expected cached rate_limited status, got %#v", health)
	}
	if health.CooldownUntil == nil {
		t.Fatalf("expected cooldown to be exposed after rate limit, got %#v", health)
	}
	if health.ProbeQueued {
		t.Fatalf("did not expect immediate requeue during cooldown, got %#v", health)
	}
	time.Sleep(20 * time.Millisecond)
	_ = manager.Observe("test", fallback, reporter, true)
	if reporter.count.Load() != 1 {
		t.Fatalf("expected no re-probe during cooldown, got %d", reporter.count.Load())
	}
	waitForCondition(t, 200*time.Millisecond, func() bool { return time.Now().After(*health.CooldownUntil) })
	health = manager.Observe("test", fallback, reporter, true)
	if !health.ProbeQueued {
		t.Fatalf("expected probe to be queued after cooldown, got %#v", health)
	}
	waitForCondition(t, 300*time.Millisecond, func() bool { return reporter.count.Load() == 2 })
}

func TestProviderProbeManagerPreservesAvailableFallbackOnProbeFailure(t *testing.T) {
	t.Setenv("GO_CORE_PROVIDER_HEALTH_WORKERS", "1")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE", "4")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_TTL", "50ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_COOLDOWN", "20ms")
	t.Setenv("GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN", "40ms")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reporter := &stubHealthReporter{health: domain.ProviderHealth{
		Provider:   "test",
		Configured: true,
		Status:     "degraded",
		Error:      "503 upstream unavailable",
	}}
	manager := newProviderProbeManager(ctx)
	fallback := domain.ProviderHealth{
		Provider:   "test",
		Configured: true,
		Available:  true,
		Status:     "ready",
		ObservedAt: time.Now().UTC(),
	}

	manager.Observe("test", fallback, reporter, true)
	waitForCondition(t, 300*time.Millisecond, func() bool { return reporter.count.Load() == 1 })
	health := manager.Observe("test", fallback, reporter, true)
	if !health.Available || health.Status != "ready" {
		t.Fatalf("expected available fallback health to be preserved, got %#v", health)
	}
}
