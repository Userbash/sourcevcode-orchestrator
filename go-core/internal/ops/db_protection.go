package ops

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"

	"sourcevcode-orchestrator/go-core/internal/app"
)

type DBProtector struct {
	DatabaseURL    string
	SnapshotDir    string
	BackupInterval time.Duration
	GuardInterval  time.Duration
	MaxSnapshots   int
}

type dbSnapshot struct {
	CreatedAt time.Time         `json:"created_at"`
	Tables    []dbSnapshotTable `json:"tables"`
}

type dbSnapshotTable struct {
	Name    string                   `json:"name"`
	Columns []dbSnapshotTableColumn  `json:"columns"`
	Rows    []map[string]interface{} `json:"rows"`
}

type dbSnapshotTableColumn struct {
	Name string `json:"name"`
	Kind string `json:"kind"`
}

func DBProtectorFromEnv() DBProtector {
	databaseURL := strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	if databaseURL == "" {
		databaseURL = app.ResolvePostgresConnectionInfo().URL
	}
	snapshotDir := strings.TrimSpace(os.Getenv("GO_CORE_DB_BACKUP_DIR"))
	if snapshotDir == "" {
		snapshotDir = defaultDBBackupDir()
	}
	backupInterval := 10 * time.Minute
	if value := strings.TrimSpace(os.Getenv("GO_CORE_DB_BACKUP_INTERVAL")); value != "" {
		if parsed, err := time.ParseDuration(value); err == nil && parsed > 0 {
			backupInterval = parsed
		}
	}
	guardInterval := 30 * time.Second
	if value := strings.TrimSpace(os.Getenv("GO_CORE_DB_GUARD_INTERVAL")); value != "" {
		if parsed, err := time.ParseDuration(value); err == nil && parsed > 0 {
			guardInterval = parsed
		}
	}
	maxSnapshots := 48
	if value := strings.TrimSpace(os.Getenv("GO_CORE_DB_BACKUP_KEEP")); value != "" {
		if parsed, err := strconv.Atoi(value); err == nil && parsed > 0 {
			maxSnapshots = parsed
		}
	}
	return DBProtector{
		DatabaseURL:    databaseURL,
		SnapshotDir:    snapshotDir,
		BackupInterval: backupInterval,
		GuardInterval:  guardInterval,
		MaxSnapshots:   maxSnapshots,
	}
}

func defaultDBBackupDir() string {
	return filepath.Join(stateHome(), "sourcevcode-orchestrator", "db_backups")
}

func (p DBProtector) Enabled() bool {
	return strings.TrimSpace(p.DatabaseURL) != "" && strings.TrimSpace(p.SnapshotDir) != ""
}

func (p DBProtector) EnsureProtected(ctx context.Context) error {
	if !p.Enabled() {
		return nil
	}
	db, err := sql.Open("pgx", p.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	if err := os.MkdirAll(p.SnapshotDir, 0o755); err != nil {
		return err
	}
	hasData, err := p.hasManagedData(ctx, db)
	if err != nil {
		return err
	}
	if !hasData {
		restored, err := p.restoreLatestSnapshot(ctx, db)
		if err != nil {
			return err
		}
		if restored {
			log.Printf("db-protector: restored database from latest snapshot in %s", p.SnapshotDir)
		}
	}
	return p.CreateSnapshot(ctx)
}

func (p DBProtector) Start(ctx context.Context) {
	if !p.Enabled() {
		return
	}
	go func() {
		backupTicker := time.NewTicker(p.BackupInterval)
		guardTicker := time.NewTicker(p.GuardInterval)
		defer backupTicker.Stop()
		defer guardTicker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-backupTicker.C:
				if err := p.CreateSnapshot(context.Background()); err != nil {
					log.Printf("db-protector backup failed: %v", err)
				}
			case <-guardTicker.C:
				if err := p.EnsureProtected(context.Background()); err != nil {
					log.Printf("db-protector guard failed: %v", err)
				}
			}
		}
	}()
}

func (p DBProtector) CreateSnapshot(ctx context.Context) error {
	if !p.Enabled() {
		return nil
	}
	db, err := sql.Open("pgx", p.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	snapshot, err := p.captureSnapshot(ctx, db)
	if err != nil {
		return err
	}
	payload, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return err
	}
	filename := filepath.Join(p.SnapshotDir, snapshot.CreatedAt.UTC().Format("20060102T150405Z")+".json")
	if err := os.WriteFile(filename, payload, 0o600); err != nil {
		return err
	}
	return p.pruneSnapshots()
}

func (p DBProtector) RestoreLatest(ctx context.Context) error {
	if !p.Enabled() {
		return fmt.Errorf("database protection is not configured")
	}
	db, err := sql.Open("pgx", p.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	restored, err := p.restoreLatestSnapshot(ctx, db)
	if err != nil {
		return err
	}
	if !restored {
		return fmt.Errorf("no snapshots found in %s", p.SnapshotDir)
	}
	return nil
}

func (p DBProtector) captureSnapshot(ctx context.Context, db *sql.DB) (dbSnapshot, error) {
	tables, err := p.managedTables(ctx, db)
	if err != nil {
		return dbSnapshot{}, err
	}
	snapshot := dbSnapshot{CreatedAt: time.Now().UTC(), Tables: make([]dbSnapshotTable, 0, len(tables))}
	for _, tableName := range tables {
		columns, err := p.tableColumns(ctx, db, tableName)
		if err != nil {
			return dbSnapshot{}, err
		}
		query := buildTableSnapshotRowQuery(tableName, columns)
		rowsResult, err := db.QueryContext(ctx, query)
		if err != nil {
			return dbSnapshot{}, err
		}
		var rows []map[string]interface{}
		for rowsResult.Next() {
			var payload []byte
			if err := rowsResult.Scan(&payload); err != nil {
				rowsResult.Close()
				return dbSnapshot{}, err
			}
			var row map[string]interface{}
			if err := json.Unmarshal(payload, &row); err != nil {
				rowsResult.Close()
				return dbSnapshot{}, err
			}
			rows = append(rows, row)
		}
		if err := rowsResult.Close(); err != nil {
			return dbSnapshot{}, err
		}
		if err := rowsResult.Err(); err != nil {
			return dbSnapshot{}, err
		}
		snapshot.Tables = append(snapshot.Tables, dbSnapshotTable{Name: tableName, Columns: columns, Rows: rows})
	}
	return snapshot, nil
}

func (p DBProtector) hasManagedData(ctx context.Context, db *sql.DB) (bool, error) {
	tables, err := p.managedTables(ctx, db)
	if err != nil {
		return false, err
	}
	for _, tableName := range tables {
		query := fmt.Sprintf(`SELECT EXISTS (SELECT 1 FROM %s LIMIT 1)`, quoteIdentifier(tableName))
		var exists bool
		if err := db.QueryRowContext(ctx, query).Scan(&exists); err != nil {
			return false, err
		}
		if exists {
			return true, nil
		}
	}
	return false, nil
}

func (p DBProtector) restoreLatestSnapshot(ctx context.Context, db *sql.DB) (bool, error) {
	filename, err := p.latestSnapshotPath()
	if err != nil {
		return false, err
	}
	if filename == "" {
		return false, nil
	}
	payload, err := os.ReadFile(filename)
	if err != nil {
		return false, err
	}
	var snapshot dbSnapshot
	if err := json.Unmarshal(payload, &snapshot); err != nil {
		return false, err
	}
	if err := p.restoreSnapshot(ctx, db, snapshot); err != nil {
		return false, err
	}
	return true, nil
}

func (p DBProtector) restoreSnapshot(ctx context.Context, db *sql.DB, snapshot dbSnapshot) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	tableNames := make([]string, 0, len(snapshot.Tables))
	for _, table := range snapshot.Tables {
		tableNames = append(tableNames, quoteIdentifier(table.Name))
	}
	if len(tableNames) > 0 {
		if _, err := tx.ExecContext(ctx, `TRUNCATE TABLE `+strings.Join(tableNames, ", ")+` RESTART IDENTITY CASCADE`); err != nil {
			return err
		}
	}
	for _, table := range snapshot.Tables {
		if len(table.Rows) == 0 {
			continue
		}
		query := fmt.Sprintf(`INSERT INTO %s SELECT * FROM jsonb_populate_record(NULL::%s, $1::jsonb)`, quoteIdentifier(table.Name), quoteIdentifier(table.Name))
		for _, row := range table.Rows {
			rowPayload, err := json.Marshal(row)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, query, rowPayload); err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}

func (p DBProtector) managedTables(ctx context.Context, db *sql.DB) ([]string, error) {
	rows, err := db.QueryContext(ctx, managedTablesQuery())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var tables []string
	for rows.Next() {
		var tableName string
		if err := rows.Scan(&tableName); err != nil {
			return nil, err
		}
		tables = append(tables, tableName)
	}
	return tables, rows.Err()
}

func managedTablesQuery() string {
	return `
		SELECT table_name
		FROM information_schema.tables
		WHERE table_schema = current_schema()
		  AND table_type = 'BASE TABLE'
		  AND table_name LIKE 'go\_%' ESCAPE '\'
		ORDER BY table_name`
}

func (p DBProtector) tableColumns(ctx context.Context, db *sql.DB, tableName string) ([]dbSnapshotTableColumn, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT column_name, data_type, udt_name
		FROM information_schema.columns
		WHERE table_schema = current_schema()
		  AND table_name = $1
		ORDER BY ordinal_position`, tableName)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var columns []dbSnapshotTableColumn
	for rows.Next() {
		var name, dataType, udtName string
		if err := rows.Scan(&name, &dataType, &udtName); err != nil {
			return nil, err
		}
		columns = append(columns, dbSnapshotTableColumn{Name: name, Kind: classifySnapshotColumn(dataType, udtName)})
	}
	return columns, rows.Err()
}

func classifySnapshotColumn(dataType string, udtName string) string {
	switch strings.ToLower(strings.TrimSpace(udtName)) {
	case "bytea":
		return "bytea"
	case "vector":
		return "vector"
	}
	switch strings.ToLower(strings.TrimSpace(dataType)) {
	case "json", "jsonb":
		return "json"
	default:
		return "scalar"
	}
}

func buildTableSnapshotRowQuery(tableName string, columns []dbSnapshotTableColumn) string {
	parts := make([]string, 0, len(columns)*2)
	for _, column := range columns {
		parts = append(parts, "'"+strings.ReplaceAll(column.Name, "'", "''")+"'")
		quotedName := quoteIdentifier(column.Name)
		switch column.Kind {
		case "bytea":
			parts = append(parts, fmt.Sprintf("CASE WHEN %s IS NULL THEN NULL ELSE '\\x' || encode(%s, 'hex') END", quotedName, quotedName))
		case "vector":
			parts = append(parts, fmt.Sprintf("CASE WHEN %s IS NULL THEN NULL ELSE %s::text END", quotedName, quotedName))
		default:
			parts = append(parts, quotedName)
		}
	}
	return fmt.Sprintf(`SELECT jsonb_build_object(%s) FROM %s`, strings.Join(parts, ", "), quoteIdentifier(tableName))
}

func quoteIdentifier(value string) string {
	return `"` + strings.ReplaceAll(value, `"`, `""`) + `"`
}

func (p DBProtector) latestSnapshotPath() (string, error) {
	entries, err := os.ReadDir(p.SnapshotDir)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		names = append(names, entry.Name())
	}
	if len(names) == 0 {
		return "", nil
	}
	slices.Sort(names)
	return filepath.Join(p.SnapshotDir, names[len(names)-1]), nil
}

func (p DBProtector) pruneSnapshots() error {
	entries, err := os.ReadDir(p.SnapshotDir)
	if err != nil {
		return err
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		names = append(names, entry.Name())
	}
	if len(names) <= p.MaxSnapshots {
		return nil
	}
	slices.Sort(names)
	for _, name := range names[:len(names)-p.MaxSnapshots] {
		if err := os.Remove(filepath.Join(p.SnapshotDir, name)); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}
