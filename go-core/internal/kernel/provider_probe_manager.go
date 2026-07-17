package kernel

import (
	"context"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/agents"
	"sourcevcode-orchestrator/go-core/internal/domain"
)

type providerProbeManager struct {
	backgroundCtx     context.Context
	timeout           time.Duration
	ttl               time.Duration
	failureCooldown   time.Duration
	rateLimitCooldown time.Duration
	queue             chan string
	mu                sync.Mutex
	states            map[string]*providerProbeState
}

type providerProbeState struct {
	reporter      agents.HealthReporter
	fallback      domain.ProviderHealth
	cached        domain.ProviderHealth
	hasCached     bool
	refreshAfter  time.Time
	cooldownUntil time.Time
	inflight      bool
	queued        bool
}

func newProviderProbeManager(backgroundCtx context.Context) *providerProbeManager {
	if backgroundCtx == nil {
		backgroundCtx = context.Background()
	}
	workers := providerProbeIntFromEnv(1, "GO_CORE_PROVIDER_HEALTH_WORKERS", "AI_BRIDGE_PROVIDER_HEALTH_WORKERS")
	if workers < 1 {
		workers = 1
	}
	queueSize := providerProbeIntFromEnv(64, "GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE", "AI_BRIDGE_PROVIDER_HEALTH_QUEUE_SIZE")
	if queueSize < workers {
		queueSize = workers
	}
	m := &providerProbeManager{
		backgroundCtx:     backgroundCtx,
		timeout:           providerProbeDurationFromEnv(3*time.Second, "GO_CORE_PROVIDER_HEALTH_TIMEOUT", "AI_BRIDGE_PROVIDER_HEALTH_TIMEOUT"),
		ttl:               providerProbeDurationFromEnv(30*time.Second, "GO_CORE_PROVIDER_HEALTH_TTL", "AI_BRIDGE_PROVIDER_HEALTH_TTL"),
		failureCooldown:   providerProbeDurationFromEnv(15*time.Second, "GO_CORE_PROVIDER_HEALTH_COOLDOWN", "AI_BRIDGE_PROVIDER_HEALTH_COOLDOWN"),
		rateLimitCooldown: providerProbeDurationFromEnv(45*time.Second, "GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN", "AI_BRIDGE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN"),
		queue:             make(chan string, queueSize),
		states:            map[string]*providerProbeState{},
	}
	for worker := 0; worker < workers; worker++ {
		go m.worker()
	}
	return m
}

func (m *providerProbeManager) Observe(provider string, fallback domain.ProviderHealth, reporter agents.HealthReporter, probe bool) domain.ProviderHealth {
	provider = strings.TrimSpace(provider)
	if provider == "" {
		return fallback
	}
	now := time.Now().UTC()

	m.mu.Lock()
	state := m.stateLocked(provider)
	state.reporter = reporter
	state.fallback = fallback
	if state.fallback.Provider == "" {
		state.fallback.Provider = provider
	}
	current := state.currentHealth(now)
	if probe && reporter != nil && state.shouldQueue(now) {
		select {
		case m.queue <- provider:
			state.queued = true
			current.ProbeQueued = true
		default:
		}
	}
	current = state.currentHealth(now)
	m.mu.Unlock()
	return current
}

func (m *providerProbeManager) worker() {
	for {
		select {
		case <-m.backgroundCtx.Done():
			return
		case provider := <-m.queue:
			m.runProbe(provider)
		}
	}
}

func (m *providerProbeManager) runProbe(provider string) {
	m.mu.Lock()
	state := m.stateLocked(provider)
	reporter := state.reporter
	state.queued = false
	if reporter == nil {
		m.mu.Unlock()
		return
	}
	state.inflight = true
	m.mu.Unlock()

	probeCtx, cancel := context.WithTimeout(m.backgroundCtx, m.timeout)
	health := reporter.Probe(probeCtx)
	cancel()

	now := time.Now().UTC()
	if health.Provider == "" {
		health.Provider = provider
	}
	if health.ObservedAt.IsZero() {
		health.ObservedAt = now
	}

	m.mu.Lock()
	state = m.stateLocked(provider)
	state.cached = health
	state.hasCached = true
	state.inflight = false
	state.queued = false
	refreshDelay, cooldown := m.scheduleFor(health)
	if refreshDelay > 0 {
		state.refreshAfter = now.Add(refreshDelay)
	} else {
		state.refreshAfter = time.Time{}
	}
	if cooldown > 0 {
		state.cooldownUntil = now.Add(cooldown)
	} else {
		state.cooldownUntil = time.Time{}
	}
	m.mu.Unlock()
}

func (m *providerProbeManager) scheduleFor(health domain.ProviderHealth) (time.Duration, time.Duration) {
	status := strings.TrimSpace(strings.ToLower(health.Status))
	switch status {
	case "ready":
		return m.ttl, 0
	case "rate_limited":
		return m.rateLimitCooldown, m.rateLimitCooldown
	case "not_configured":
		return m.failureCooldown, m.failureCooldown
	case "degraded", "unavailable":
		return m.failureCooldown, m.failureCooldown
	default:
		if health.Available {
			return m.ttl, 0
		}
		return m.failureCooldown, m.failureCooldown
	}
}

func (m *providerProbeManager) stateLocked(provider string) *providerProbeState {
	state := m.states[provider]
	if state == nil {
		state = &providerProbeState{}
		m.states[provider] = state
	}
	return state
}

func (s *providerProbeState) shouldQueue(now time.Time) bool {
	if s.reporter == nil || s.queued || s.inflight {
		return false
	}
	if !s.cooldownUntil.IsZero() && now.Before(s.cooldownUntil) {
		return false
	}
	if !s.refreshAfter.IsZero() && now.Before(s.refreshAfter) {
		return false
	}
	return true
}

func (s *providerProbeState) currentHealth(now time.Time) domain.ProviderHealth {
	health := s.fallback
	if s.hasCached {
		health = selectProviderHealth(s.fallback, s.cached)
	}
	if health.Provider == "" {
		health.Provider = s.fallback.Provider
	}
	if health.ObservedAt.IsZero() {
		health.ObservedAt = now
	}
	health.ProbeQueued = s.queued || s.inflight
	if !s.cooldownUntil.IsZero() && now.Before(s.cooldownUntil) {
		cooldownUntil := s.cooldownUntil
		health.CooldownUntil = &cooldownUntil
	} else {
		health.CooldownUntil = nil
	}
	if !s.refreshAfter.IsZero() {
		refreshAfter := s.refreshAfter
		health.RefreshAfter = &refreshAfter
	} else {
		health.RefreshAfter = nil
	}
	return health
}

func selectProviderHealth(fallback domain.ProviderHealth, probe domain.ProviderHealth) domain.ProviderHealth {
	if probe.Provider == "" {
		return fallback
	}
	if fallback.Provider == "" {
		return probe
	}
	if probe.Available {
		return probe
	}
	if fallback.Available {
		return fallback
	}
	if strings.EqualFold(strings.TrimSpace(fallback.Status), "configured") {
		return probe
	}
	if fallback.ObservedAt.After(probe.ObservedAt) {
		return fallback
	}
	return probe
}

func providerProbeDurationFromEnv(fallback time.Duration, keys ...string) time.Duration {
	for _, key := range keys {
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		parsed, err := time.ParseDuration(value)
		if err == nil && parsed > 0 {
			return parsed
		}
	}
	return fallback
}

func providerProbeIntFromEnv(fallback int, keys ...string) int {
	for _, key := range keys {
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err == nil && parsed > 0 {
			return parsed
		}
	}
	return fallback
}
