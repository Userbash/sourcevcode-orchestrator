package kernel

import (
	"os"
	"runtime"
	"strconv"
	"strings"
)

type executionProfile struct {
	LogicalCPUs       int
	MaxProcs          int
	ReservedProcs     int
	UsableParallelism int
}

func detectExecutionProfile() executionProfile {
	if override, ok := firstIntEnv("AI_BRIDGE_GOMAXPROCS", "GO_CORE_GOMAXPROCS"); ok && override > 0 {
		runtime.GOMAXPROCS(override)
	}

	logicalCPUs := runtime.NumCPU()
	if logicalCPUs < 1 {
		logicalCPUs = 1
	}
	maxProcs := runtime.GOMAXPROCS(0)
	if maxProcs < 1 {
		maxProcs = 1
	}

	reserved := 1
	if reserve, ok := firstIntEnv("AI_BRIDGE_CPU_RESERVE", "GO_CORE_CPU_RESERVE"); ok {
		reserved = reserve
	}
	if reserved < 0 {
		reserved = 0
	}
	if reserved >= maxProcs {
		reserved = maxProcs - 1
		if reserved < 0 {
			reserved = 0
		}
	}

	usable := maxProcs - reserved
	if usable < 1 {
		usable = 1
	}
	if capValue, ok := firstIntEnv("AI_BRIDGE_MAX_PARALLELISM", "GO_CORE_MAX_PARALLELISM"); ok && capValue > 0 && capValue < usable {
		usable = capValue
	}

	return executionProfile{
		LogicalCPUs:       logicalCPUs,
		MaxProcs:          maxProcs,
		ReservedProcs:     reserved,
		UsableParallelism: usable,
	}
}

func (p executionProfile) Metadata() map[string]any {
	return map[string]any{
		"logical_cpus":       p.LogicalCPUs,
		"gomaxprocs":         p.MaxProcs,
		"reserved_procs":     p.ReservedProcs,
		"usable_parallelism": p.UsableParallelism,
	}
}

func firstIntEnv(keys ...string) (int, bool) {
	for _, key := range keys {
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err != nil {
			continue
		}
		return parsed, true
	}
	return 0, false
}
