package app

import (
	"os"
	"strings"
)

const DefaultStatePath = ""

var DefaultRequiredHTTPEndpoints = []string{
	"/health",
	"/health/full",
	"/api/health",
	"/providers/inventory",
	"/providers/runtime_inventory",
	"/providers/models/index",
	"/providers/ai_kernel/gate",
	"/health/local_models",
	"/sourcecraft",
	"/diagnostics",
	"/control/ws",
}

type Config struct {
	Addr                  string
	StatePath             string
	RequiredHTTPEndpoints []string
}

func LoadConfig() Config {
	return Config{
		Addr:                  resolveAddr(),
		StatePath:             resolveStatePath(),
		RequiredHTTPEndpoints: append([]string(nil), DefaultRequiredHTTPEndpoints...),
	}
}

func resolveAddr() string {
	if value := strings.TrimSpace(os.Getenv("GO_CORE_ADDR")); value != "" {
		return value
	}
	if value := strings.TrimSpace(os.Getenv("AI_BRIDGE_HTTP_ADDR")); value != "" {
		return value
	}

	host := firstNonEmptyEnv("GO_CORE_HOST", "AI_BRIDGE_API_HOST", "ORCHESTRATOR_HOST")
	port := firstNonEmptyEnv("AI_BRIDGE_API_PORT", "ORCHESTRATOR_PORT", "PORT")

	if host == "" {
		host = "0.0.0.0"
	}
	if port == "" {
		port = "8080"
	}
	return host + ":" + port
}

func resolveStatePath() string {
	if value := strings.TrimSpace(os.Getenv("GO_CORE_STATE_PATH")); value != "" {
		return value
	}
	return DefaultStatePath
}

func firstNonEmptyEnv(keys ...string) string {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}
