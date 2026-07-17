package ops

import (
	"path/filepath"
	"strings"
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

func TestBuildTableSnapshotRowQueryAvoidsAggregate(t *testing.T) {
	query := buildTableSnapshotRowQuery("go_reasoning_memory", []dbSnapshotTableColumn{{Name: "id", Kind: "scalar"}, {Name: "payload", Kind: "json"}})

	if strings.Contains(query, "jsonb_agg") {
		t.Fatalf("snapshot query must avoid jsonb_agg: %s", query)
	}
	if !strings.Contains(query, "SELECT jsonb_build_object(") {
		t.Fatalf("snapshot query must select one JSON object per row: %s", query)
	}
}
