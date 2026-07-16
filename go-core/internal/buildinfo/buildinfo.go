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

func String() string {
	return fmt.Sprintf("version=%s commit=%s built=%s", EffectiveVersion(), Commit, BuildTime)
}

func Snapshot() map[string]any {
	return map[string]any{
		"version":    EffectiveVersion(),
		"commit":     Commit,
		"build_time": BuildTime,
	}
}
