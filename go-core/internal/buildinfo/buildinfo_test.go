package buildinfo

import "testing"

func TestEffectiveVersionDefaultsToDev(t *testing.T) {
	originalVersion := Version
	originalCommit := Commit
	originalBuildTime := BuildTime
	t.Cleanup(func() {
		Version = originalVersion
		Commit = originalCommit
		BuildTime = originalBuildTime
	})

	Version = ""
	if got := EffectiveVersion(); got != "dev" {
		t.Fatalf("EffectiveVersion() = %q, want dev", got)
	}
}

func TestStringAndSnapshotNormalizeEmptyMetadata(t *testing.T) {
	originalVersion := Version
	originalCommit := Commit
	originalBuildTime := BuildTime
	t.Cleanup(func() {
		Version = originalVersion
		Commit = originalCommit
		BuildTime = originalBuildTime
	})

	Version = ""
	Commit = ""
	BuildTime = ""

	if got := String(); got != "version=dev commit=unknown built=unknown" {
		t.Fatalf("String() = %q, want normalized unknown metadata", got)
	}

	snapshot := Snapshot()
	if got := snapshot["version"]; got != "dev" {
		t.Fatalf("Snapshot version = %v, want dev", got)
	}
	if got := snapshot["commit"]; got != "unknown" {
		t.Fatalf("Snapshot commit = %v, want unknown", got)
	}
	if got := snapshot["build_time"]; got != "unknown" {
		t.Fatalf("Snapshot build_time = %v, want unknown", got)
	}
}
