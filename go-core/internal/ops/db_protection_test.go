package ops

import (
	"path/filepath"
	"testing"
)

func TestDBProtectorFromEnvUsesWritableStateHomeByDefault(t *testing.T) {
	t.Setenv("GO_CORE_DB_BACKUP_DIR", "")
	t.Setenv("XDG_STATE_HOME", "/tmp/sourcevcode-state")

	cfg := DBProtectorFromEnv()

	want := filepath.Join("/tmp/sourcevcode-state", "sourcevcode-orchestrator", "db_backups")
	if cfg.SnapshotDir != want {
		t.Fatalf("unexpected snapshot dir: got %q want %q", cfg.SnapshotDir, want)
	}
}

func TestDBProtectorFromEnvHonorsExplicitBackupDir(t *testing.T) {
	t.Setenv("GO_CORE_DB_BACKUP_DIR", "/var/tmp/custom-db-backups")

	cfg := DBProtectorFromEnv()

	if cfg.SnapshotDir != "/var/tmp/custom-db-backups" {
		t.Fatalf("unexpected snapshot dir: %q", cfg.SnapshotDir)
	}
}
