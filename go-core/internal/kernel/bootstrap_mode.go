package kernel

import (
	"os"
	"strings"
)

func bootstrapSafeModeEnabled() bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv("GO_CORE_BOOTSTRAP_SAFE_MODE")))
	switch value {
	case "1", "true", "yes", "on", "enabled":
		return true
	default:
		return false
	}
}
