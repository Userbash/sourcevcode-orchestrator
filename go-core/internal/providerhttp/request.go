package providerhttp

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type RequestConfig struct {
	ProviderID   string
	BaseURL      string
	APIKey       string
	ModelName    string
	TrafficClass string
	Client       *http.Client
}

type Response struct {
	StatusCode int
	Header     http.Header
	Body       []byte
}

var requestGates sync.Map

type requestGate struct {
	slots chan struct{}
}

func DoStream(ctx context.Context, cfg RequestConfig, method, endpoint string, body []byte, contentType string, configure func(*http.Request), handle func(*http.Response) error) error {
	if strings.TrimSpace(endpoint) == "" {
		return fmt.Errorf("%s endpoint is not configured", strings.ToLower(method))
	}
	release, err := acquireRequestSlot(ctx, cfg, endpoint)
	if err != nil {
		return err
	}
	defer release()

	client := cfg.Client
	if client == nil {
		client = http.DefaultClient
	}

	request, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	if contentType != "" {
		request.Header.Set("Content-Type", contentType)
	}
	if key := strings.TrimSpace(cfg.APIKey); key != "" {
		request.Header.Set("Authorization", "Bearer "+key)
	}
	if configure != nil {
		configure(request)
	}

	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("%s request failed: %w", strings.TrimSpace(cfg.ProviderID), err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		return providerHTTPError(response.StatusCode, body)
	}
	if handle == nil {
		return nil
	}
	return handle(response)
}

func Do(ctx context.Context, cfg RequestConfig, method, endpoint string, body []byte, contentType string, limit int64) (Response, error) {
	if strings.TrimSpace(endpoint) == "" {
		return Response{}, fmt.Errorf("%s endpoint is not configured", strings.ToLower(method))
	}
	release, err := acquireRequestSlot(ctx, cfg, endpoint)
	if err != nil {
		return Response{}, err
	}
	defer release()

	maxRetries := intFromEnv("GO_CORE_PROVIDER_MAX_RETRIES", 3, "AI_BRIDGE_PROVIDER_MAX_RETRIES")
	baseBackoff := durationFromEnvAny(500*time.Millisecond, "GO_CORE_PROVIDER_RETRY_BASE", "AI_BRIDGE_PROVIDER_RETRY_BASE")
	maxBackoff := durationFromEnvAny(8*time.Second, "GO_CORE_PROVIDER_RETRY_MAX_BACKOFF", "AI_BRIDGE_PROVIDER_RETRY_MAX_BACKOFF")
	if maxBackoff < baseBackoff {
		maxBackoff = baseBackoff
	}

	client := cfg.Client
	if client == nil {
		client = http.DefaultClient
	}

	var lastErr error
	for attempt := 0; attempt <= maxRetries; attempt++ {
		request, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(body))
		if err != nil {
			return Response{}, err
		}
		if contentType != "" {
			request.Header.Set("Content-Type", contentType)
		}
		if key := strings.TrimSpace(cfg.APIKey); key != "" {
			request.Header.Set("Authorization", "Bearer "+key)
		}

		response, requestErr := client.Do(request)
		if requestErr != nil {
			lastErr = fmt.Errorf("%s request failed: %w", strings.TrimSpace(cfg.ProviderID), requestErr)
			if !shouldRetryFailure(ctx, 0, requestErr) || attempt == maxRetries {
				return Response{}, lastErr
			}
			if waitErr := sleepWithContext(ctx, retryDelay(nil, attempt, baseBackoff, maxBackoff)); waitErr != nil {
				return Response{}, waitErr
			}
			continue
		}

		responseBody, readErr := io.ReadAll(io.LimitReader(response.Body, limit))
		closeErr := response.Body.Close()
		if readErr != nil {
			return Response{}, fmt.Errorf("read %s response: %w", strings.TrimSpace(cfg.ProviderID), readErr)
		}
		if closeErr != nil {
			return Response{}, fmt.Errorf("close %s response: %w", strings.TrimSpace(cfg.ProviderID), closeErr)
		}
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			return Response{
				StatusCode: response.StatusCode,
				Header:     response.Header.Clone(),
				Body:       responseBody,
			}, nil
		}

		lastErr = providerHTTPError(response.StatusCode, responseBody)
		if !shouldRetryFailure(ctx, response.StatusCode, nil) || attempt == maxRetries {
			return Response{
				StatusCode: response.StatusCode,
				Header:     response.Header.Clone(),
				Body:       responseBody,
			}, nil
		}
		if waitErr := sleepWithContext(ctx, retryDelay(response.Header, attempt, baseBackoff, maxBackoff)); waitErr != nil {
			return Response{}, waitErr
		}
	}
	return Response{}, lastErr
}

func acquireRequestSlot(ctx context.Context, cfg RequestConfig, endpoint string) (func(), error) {
	providerRelease, err := acquireGateSlot(ctx, sharedRequestGate(requestGateKey(cfg, endpoint), requestGateMaxConcurrent(cfg)))
	if err != nil {
		return nil, err
	}
	modelKey := requestModelGateKey(cfg, endpoint)
	if modelKey == "" {
		return providerRelease, nil
	}
	modelRelease, err := acquireGateSlot(ctx, sharedRequestGate(modelKey, requestGateMaxConcurrentPerModel(cfg)))
	if err != nil {
		providerRelease()
		return nil, err
	}
	return func() {
		modelRelease()
		providerRelease()
	}, nil
}

func acquireGateSlot(ctx context.Context, gate *requestGate) (func(), error) {
	select {
	case gate.slots <- struct{}{}:
		return func() {
			<-gate.slots
		}, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func sharedRequestGate(key string, maxConcurrent int) *requestGate {
	if maxConcurrent <= 0 {
		maxConcurrent = 1
	}
	if gate, ok := requestGates.Load(key); ok {
		return gate.(*requestGate)
	}
	created := &requestGate{slots: make(chan struct{}, maxConcurrent)}
	actual, _ := requestGates.LoadOrStore(key, created)
	return actual.(*requestGate)
}

func requestGateKey(cfg RequestConfig, endpoint string) string {
	base := sanitizedBaseURL(cfg.BaseURL)
	if base == "" {
		base = sanitizedBaseURL(endpoint)
	}
	return strings.ToLower(strings.TrimSpace(cfg.ProviderID)) + "|" + base + "|" + strings.TrimSpace(cfg.APIKey) + "|" + requestTrafficClass(cfg)
}

func requestModelGateKey(cfg RequestConfig, endpoint string) string {
	modelName := strings.TrimSpace(cfg.ModelName)
	if modelName == "" {
		return ""
	}
	base := sanitizedBaseURL(cfg.BaseURL)
	if base == "" {
		base = sanitizedBaseURL(endpoint)
	}
	return strings.ToLower(strings.TrimSpace(cfg.ProviderID)) + "|" + base + "|model|" + strings.ToLower(modelName) + "|" + requestTrafficClass(cfg)
}

func requestTrafficClass(cfg RequestConfig) string {
	trafficClass := strings.ToLower(strings.TrimSpace(cfg.TrafficClass))
	if trafficClass == "" {
		return "primary"
	}
	return trafficClass
}

func requestGateMaxConcurrent(cfg RequestConfig) int {
	switch requestTrafficClass(cfg) {
	case "probe":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_PROBE_PER_KEY", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_PROBE_PER_KEY")
	case "inventory":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_INVENTORY_PER_KEY", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_INVENTORY_PER_KEY")
	case "helper":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_HELPER_PER_KEY", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_HELPER_PER_KEY")
	default:
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY", 1, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_PER_KEY")
	}
}

func requestGateMaxConcurrentPerModel(cfg RequestConfig) int {
	switch requestTrafficClass(cfg) {
	case "probe":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_PROBE_PER_MODEL", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_PROBE_PER_MODEL")
	case "inventory":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_INVENTORY_PER_MODEL", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_INVENTORY_PER_MODEL")
	case "helper":
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_HELPER_PER_MODEL", 2, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_HELPER_PER_MODEL")
	default:
		return intFromEnv("GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL", 1, "AI_BRIDGE_PROVIDER_MAX_CONCURRENT_PER_MODEL")
	}
}

func shouldRetryFailure(ctx context.Context, status int, err error) bool {
	if ctx != nil && ctx.Err() != nil {
		return false
	}
	if err != nil {
		return true
	}
	switch status {
	case http.StatusRequestTimeout, http.StatusConflict, http.StatusTooEarly, http.StatusTooManyRequests,
		http.StatusInternalServerError, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	default:
		return false
	}
}

func retryDelay(header http.Header, attempt int, baseBackoff, maxBackoff time.Duration) time.Duration {
	if retryAfter := parseRetryAfter(header); retryAfter > 0 {
		return retryAfter
	}
	if baseBackoff <= 0 {
		baseBackoff = 500 * time.Millisecond
	}
	delay := baseBackoff
	for i := 0; i < attempt; i++ {
		delay *= 2
		if delay >= maxBackoff {
			return maxBackoff
		}
	}
	if maxBackoff > 0 && delay > maxBackoff {
		return maxBackoff
	}
	return delay
}

func parseRetryAfter(header http.Header) time.Duration {
	if header == nil {
		return 0
	}
	value := strings.TrimSpace(header.Get("Retry-After"))
	if value == "" {
		return 0
	}
	if seconds, err := strconv.Atoi(value); err == nil {
		if seconds <= 0 {
			return 0
		}
		return time.Duration(seconds) * time.Second
	}
	when, err := http.ParseTime(value)
	if err != nil {
		return 0
	}
	delay := time.Until(when)
	if delay < 0 {
		return 0
	}
	return delay
}

func sleepWithContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func providerHTTPError(status int, body []byte) error {
	message := strings.TrimSpace(string(body))
	if len(message) > 1000 {
		message = message[:1000]
	}
	return fmt.Errorf("provider returned HTTP %d: %s", status, message)
}

func sanitizedBaseURL(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	parsed.User = nil
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}

func durationFromEnvAny(fallback time.Duration, keys ...string) time.Duration {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			parsed, err := time.ParseDuration(value)
			if err == nil && parsed > 0 {
				return parsed
			}
		}
	}
	return fallback
}

func intFromEnv(key string, fallback int, aliases ...string) int {
	keys := append([]string{key}, aliases...)
	for _, envKey := range keys {
		value := strings.TrimSpace(os.Getenv(envKey))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err == nil {
			return parsed
		}
	}
	return fallback
}
