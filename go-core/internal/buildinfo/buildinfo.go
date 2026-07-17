package buildinfo

import "fmt"

var (
	Version   = "dev"
	Commit    = "unknown"
	BuildTime = "unknown"
)

func EffectiveVersion() string {
	if Version == "" {
		return "dev"
	}
	return Version
}

func EffectiveCommit() string {
	if Commit == "" {
		return "unknown"
	}
	return Commit
}

func EffectiveBuildTime() string {
	if BuildTime == "" {
		return "unknown"
	}
	return BuildTime
}

func String() string {
	return fmt.Sprintf("version=%s commit=%s built=%s", EffectiveVersion(), EffectiveCommit(), EffectiveBuildTime())
}

func Snapshot() map[string]any {
	return map[string]any{
		"version":    EffectiveVersion(),
		"commit":     EffectiveCommit(),
		"build_time": EffectiveBuildTime(),
	}
}
