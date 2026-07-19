package ops

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"

	"sourcevcode-orchestrator/go-core/internal/app"
)

type DBInspector struct {
	DatabaseURL string
}

func (i DBInspector) Inspect(ctx context.Context) error {
	databaseURL := strings.TrimSpace(i.DatabaseURL)
	if databaseURL == "" {
		databaseURL = strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	}
	if databaseURL == "" {
		databaseURL = app.ResolvePostgresConnectionInfo().URL
	}
	if databaseURL == "" {
		return fmt.Errorf("postgres connection is not configured")
	}
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := verifyRequiredExtensions(ctx, db); err != nil {
		return err
	}
	if err := printWorkflowSample(ctx, db); err != nil {
		return err
	}
	if err := printSessionSample(ctx, db); err != nil {
		return err
	}
	if err := printVectorSample(ctx, db); err != nil {
		return err
	}
	return nil
}

func verifyRequiredExtensions(ctx context.Context, db *sql.DB) error {
	var available bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')`).Scan(&available); err != nil {
		return err
	}
	if !available {
		return fmt.Errorf("pgvector is not installed on this PostgreSQL server; use the pgvector-enabled database from docker-compose.yml or install pgvector packages before bootstrap")
	}

	var enabled bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')`).Scan(&enabled); err != nil {
		return err
	}
	if !enabled {
		return fmt.Errorf("pgvector is available but not enabled in the current database; run CREATE EXTENSION IF NOT EXISTS vector in the target database")
	}
	return nil
}

func printWorkflowSample(ctx context.Context, db *sql.DB) error {
	fmt.Println("--- GO WORKFLOWS SAMPLE (Last 3) ---")
	rows, err := db.QueryContext(ctx, `SELECT workflow_id, updated_at, task_json, result_json FROM go_workflows ORDER BY updated_at DESC LIMIT 3`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var workflowID string
		var updatedAt time.Time
		var taskJSON, resultJSON []byte
		if err := rows.Scan(&workflowID, &updatedAt, &taskJSON, &resultJSON); err != nil {
			return err
		}
		fmt.Printf("Workflow: %s | Updated: %s\n", workflowID, updatedAt.Format(time.RFC3339))
		fmt.Printf("Task snippet: %s\n", truncateJSON(taskJSON, 220))
		fmt.Printf("Result snippet: %s\n", truncateJSON(resultJSON, 220))
		fmt.Println(strings.Repeat("-", 40))
	}
	return rows.Err()
}

func printSessionSample(ctx context.Context, db *sql.DB) error {
	fmt.Println("\n--- SESSION STATES SAMPLE (Last 3) ---")
	rows, err := db.QueryContext(ctx, `SELECT session_id, branch, updated_at, state_json FROM go_session_states ORDER BY updated_at DESC LIMIT 3`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var sessionID, branch string
		var updatedAt time.Time
		var stateJSON []byte
		if err := rows.Scan(&sessionID, &branch, &updatedAt, &stateJSON); err != nil {
			return err
		}
		fmt.Printf("Session: %s | Branch: %s | Updated: %s\n", sessionID, branch, updatedAt.Format(time.RFC3339))
		fmt.Printf("State snippet: %s\n", truncateJSON(stateJSON, 220))
		fmt.Println(strings.Repeat("-", 40))
	}
	return rows.Err()
}

func printVectorSample(ctx context.Context, db *sql.DB) error {
	fmt.Println("\n--- VECTOR CHUNKS SAMPLE (Last 3) ---")
	rows, err := db.QueryContext(ctx, `SELECT session_id, source, chunk_index, text_content, metadata_json, created_at FROM go_vector_chunks ORDER BY created_at DESC LIMIT 3`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var sessionID, source, text string
		var chunkIndex int
		var metadataJSON []byte
		var createdAt time.Time
		if err := rows.Scan(&sessionID, &source, &chunkIndex, &text, &metadataJSON, &createdAt); err != nil {
			return err
		}
		fmt.Printf("Session: %s | Source: %s | Chunk: %d | Added: %s\n", sessionID, source, chunkIndex, createdAt.Format(time.RFC3339))
		fmt.Printf("Text snippet: %s\n", truncateString(text, 220))
		fmt.Printf("Metadata: %s\n", truncateJSON(metadataJSON, 180))
		fmt.Println(strings.Repeat("-", 40))
	}
	return rows.Err()
}

func truncateJSON(value []byte, limit int) string {
	if len(value) == 0 {
		return "{}"
	}
	var decoded any
	if err := json.Unmarshal(value, &decoded); err == nil {
		if normalized, err := json.Marshal(decoded); err == nil {
			return truncateString(string(normalized), limit)
		}
	}
	return truncateString(string(value), limit)
}

func truncateString(value string, limit int) string {
	value = strings.TrimSpace(value)
	if len(value) <= limit || limit <= 0 {
		return value
	}
	if limit <= 3 {
		return value[:limit]
	}
	return value[:limit-3] + "..."
}
