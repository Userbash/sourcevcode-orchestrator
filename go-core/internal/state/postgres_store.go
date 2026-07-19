package state

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type PostgresStore struct {
	db          *sql.DB
	storageMode string
}

func NewPostgresStore(databaseURL string) (*PostgresStore, error) {
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return nil, err
	}
	safeMode := stateEnvBool("GO_CORE_BOOTSTRAP_SAFE_MODE", false)
	maxOpenFallback := 16
	maxIdleFallback := 4
	if safeMode {
		maxOpenFallback = 2
		maxIdleFallback = 1
	}
	maxOpenConns := stateEnvInt("GO_CORE_PG_MAX_OPEN_CONNS", maxOpenFallback)
	maxIdleConns := stateEnvInt("GO_CORE_PG_MAX_IDLE_CONNS", maxInt(maxIdleFallback, maxOpenConns/2))
	if maxIdleConns > maxOpenConns {
		maxIdleConns = maxOpenConns
	}
	db.SetMaxOpenConns(maxOpenConns)
	db.SetMaxIdleConns(maxIdleConns)
	db.SetConnMaxLifetime(stateEnvDuration("GO_CORE_PG_CONN_MAX_LIFETIME", 30*time.Minute))
	db.SetConnMaxIdleTime(stateEnvDuration("GO_CORE_PG_CONN_MAX_IDLE_TIME", 10*time.Minute))
	store := &PostgresStore{db: db, storageMode: "go_postgres_store"}
	if stateEnvBool("GO_CORE_PG_SKIP_SCHEMA_ENSURE", safeMode) {
		return store, nil
	}
	schemaTimeout := stateEnvDuration("GO_CORE_PG_SCHEMA_TIMEOUT", 2*time.Minute)
	if safeMode {
		schemaTimeout = stateEnvDuration("GO_CORE_PG_SCHEMA_TIMEOUT", 20*time.Second)
	}
	schemaCtx, cancel := context.WithTimeout(context.Background(), schemaTimeout)
	defer cancel()
	if err := store.ensureSchema(schemaCtx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *PostgresStore) ensureSchema(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS go_workflows (workflow_id TEXT PRIMARY KEY, task_json JSONB NOT NULL, plan_json JSONB NOT NULL, acceptance_json JSONB NOT NULL, result_json JSONB, updated_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_session_states (session_id TEXT NOT NULL, branch TEXT NOT NULL, version INTEGER NOT NULL, prompt_version TEXT NOT NULL, context_version TEXT NOT NULL, state_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL, storage_mode TEXT NOT NULL, PRIMARY KEY (session_id, branch))`,
		`CREATE TABLE IF NOT EXISTS go_invalidations (id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, branch TEXT NOT NULL, reason TEXT NOT NULL, payload_json JSONB NOT NULL, logged_at TIMESTAMPTZ NOT NULL, storage_mode TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_vector_chunks (chunk_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, branch TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, text_content TEXT NOT NULL, normalized_text TEXT NOT NULL, terms_json JSONB NOT NULL, embedding_json JSONB NOT NULL, embedding_vector VECTOR(64), metadata_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_rag_documents (document_id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT NOT NULL, title TEXT NOT NULL, content_text TEXT NOT NULL, content_summary TEXT NOT NULL, metadata_json JSONB NOT NULL, importance DOUBLE PRECISION NOT NULL DEFAULT 0, repo_id TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT 'default', commit_sha TEXT NOT NULL DEFAULT '', artifact_ids_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, last_accessed_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_rag_memories (memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, scope TEXT NOT NULL, owner_id TEXT NOT NULL, content_json JSONB NOT NULL, summary TEXT NOT NULL, embedding_json JSONB NOT NULL, embedding_vector VECTOR(64), metadata_json JSONB NOT NULL, confidence DOUBLE PRECISION NOT NULL DEFAULT 0, importance DOUBLE PRECISION NOT NULL DEFAULT 0, repo_id TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT 'default', commit_sha TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_route_memories (route_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL, parent_task_id TEXT NOT NULL, root_task_id TEXT NOT NULL, task_type TEXT NOT NULL, capability TEXT NOT NULL, complexity TEXT NOT NULL, project TEXT NOT NULL, repo_path TEXT NOT NULL, repo_fingerprint TEXT NOT NULL, branch TEXT NOT NULL, agent_id TEXT NOT NULL, provider TEXT NOT NULL, model_name TEXT NOT NULL, plan_mode TEXT NOT NULL, success BOOLEAN NOT NULL, confidence DOUBLE PRECISION NOT NULL DEFAULT 0, latency_ms BIGINT NOT NULL DEFAULT 0, review_passed BOOLEAN NOT NULL DEFAULT FALSE, tests_passed BOOLEAN NOT NULL DEFAULT FALSE, cost_estimate DOUBLE PRECISION NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL, task_signature_json JSONB NOT NULL, embedding_json JSONB NOT NULL, metadata_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_vfs_artifacts (artifact_id TEXT PRIMARY KEY, storage_uri TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes BIGINT NOT NULL, mime_type TEXT NOT NULL, content_bytes BYTEA NOT NULL DEFAULT ''::bytea, metadata_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS go_vfs_checkpoints (path TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT NOT NULL, checkpoint_json JSONB NOT NULL, checksum TEXT NOT NULL, integrity TEXT NOT NULL, metadata_json JSONB NOT NULL, storage_uri TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)`,
		`CREATE INDEX IF NOT EXISTS idx_go_invalidations_session_branch_logged_at ON go_invalidations (session_id, branch, logged_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_vector_chunks_session_branch_created_at ON go_vector_chunks (session_id, branch, created_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_rag_documents_scope_owner_updated_at ON go_rag_documents (scope, owner_id, updated_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_rag_documents_search ON go_rag_documents USING GIN (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_summary, '') || ' ' || coalesce(content_text, '')))`,
		`CREATE INDEX IF NOT EXISTS idx_go_rag_memories_scope_owner_updated_at ON go_rag_memories (scope, owner_id, updated_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_rag_memories_search ON go_rag_memories USING GIN (to_tsvector('simple', coalesce(summary, '')))`,
		`CREATE INDEX IF NOT EXISTS idx_go_route_memories_scope_updated_at ON go_route_memories (project, repo_fingerprint, capability, updated_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_route_memories_route_updated_at ON go_route_memories (agent_id, provider, model_name, updated_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_go_vfs_checkpoints_task_updated_at ON go_vfs_checkpoints (task_id, updated_at DESC)`,
	}
	if !stateEnvBool("GO_CORE_PG_SKIP_VECTOR_EXTENSION", stateEnvBool("GO_CORE_BOOTSTRAP_SAFE_MODE", false)) {
		statements = append([]string{`CREATE EXTENSION IF NOT EXISTS vector`}, statements...)
	}
	for _, stmt := range statements {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return wrapSchemaError(err, stmt)
		}
	}
	if _, err := s.db.ExecContext(ctx, `ALTER TABLE go_vfs_artifacts ADD COLUMN IF NOT EXISTS content_bytes BYTEA NOT NULL DEFAULT ''::bytea`); err != nil {
		return err
	}
	if _, err := s.db.ExecContext(ctx, `ALTER TABLE go_vector_chunks ADD COLUMN IF NOT EXISTS embedding_vector VECTOR(64)`); err != nil {
		return err
	}
	if err := s.ensureRAGMemoryVectorCompatibility(ctx); err != nil {
		return err
	}
	if !stateEnvBool("GO_CORE_PG_SKIP_VECTOR_INDEXES", stateEnvBool("GO_CORE_BOOTSTRAP_SAFE_MODE", false)) {
		if err := s.ensureVectorIndexes(ctx); err != nil {
			return err
		}
	}
	return nil
}

func stateEnvInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func stateEnvDuration(key string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func stateEnvBool(key string, fallback bool) bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if value == "" {
		return fallback
	}
	switch value {
	case "1", "true", "yes", "on", "enabled":
		return true
	case "0", "false", "no", "off", "disabled":
		return false
	default:
		return fallback
	}
}

func maxInt(a int, b int) int {
	if a > b {
		return a
	}
	return b
}

func wrapSchemaError(err error, stmt string) error {
	if err == nil {
		return nil
	}
	if strings.Contains(strings.ToLower(stmt), "create extension if not exists vector") {
		message := strings.ToLower(err.Error())
		if strings.Contains(message, `extension "vector" is not available`) || strings.Contains(message, `could not open extension control file`) {
			return fmt.Errorf("pgvector extension is required but not installed on the PostgreSQL server: %w; use the pgvector-enabled database from docker-compose.yml or install pgvector before running CREATE EXTENSION IF NOT EXISTS vector", err)
		}
	}
	return err
}

func (s *PostgresStore) ensureRAGMemoryVectorCompatibility(ctx context.Context) error {
	var formatType string
	err := s.db.QueryRowContext(ctx, `
		SELECT COALESCE(format_type(a.atttypid, a.atttypmod), '')
		FROM pg_attribute a
		JOIN pg_class c ON c.oid = a.attrelid
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = current_schema()
		  AND c.relname = 'go_rag_memories'
		  AND a.attname = 'embedding_vector'
		  AND a.attnum > 0
		  AND NOT a.attisdropped
		LIMIT 1`,
	).Scan(&formatType)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	formatType = strings.TrimSpace(strings.ToLower(formatType))
	if formatType == "" || formatType == "vector" {
		return nil
	}
	if strings.HasPrefix(formatType, "vector(") {
		_, err = s.db.ExecContext(ctx, `ALTER TABLE go_rag_memories ALTER COLUMN embedding_vector TYPE vector USING embedding_vector::vector`)
		return err
	}
	return nil
}

func (s *PostgresStore) ensureVectorIndexes(ctx context.Context) error {
	statements := []string{
		`CREATE INDEX IF NOT EXISTS idx_go_rag_memories_embedding_hnsw ON go_rag_memories USING hnsw (embedding_vector vector_cosine_ops)`,
		`CREATE INDEX IF NOT EXISTS idx_go_vector_chunks_embedding_hnsw ON go_vector_chunks USING hnsw (embedding_vector vector_cosine_ops)`,
	}
	for _, stmt := range statements {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			message := strings.ToLower(err.Error())
			if strings.Contains(message, "access method") || strings.Contains(message, "operator class") || strings.Contains(message, "does not exist") || strings.Contains(message, "dimensions") {
				continue
			}
			return err
		}
	}
	return nil
}

func (s *PostgresStore) SaveWorkflow(ctx context.Context, record domain.WorkflowRecord) error {
	resultJSON, err := marshalNullable(record.Result)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO go_workflows (workflow_id, task_json, plan_json, acceptance_json, result_json, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (workflow_id) DO UPDATE
		SET task_json = EXCLUDED.task_json,
		    plan_json = EXCLUDED.plan_json,
		    acceptance_json = EXCLUDED.acceptance_json,
		    result_json = EXCLUDED.result_json,
		    updated_at = EXCLUDED.updated_at`,
		record.Task.ID,
		mustJSON(record.Task),
		mustJSON(record.Plan),
		mustJSON(record.Acceptance),
		resultJSON,
		record.UpdatedAt,
	)
	return err
}

func (s *PostgresStore) GetWorkflow(ctx context.Context, workflowID string) (domain.WorkflowRecord, bool, error) {
	var taskJSON, planJSON, acceptanceJSON []byte
	var resultJSON []byte
	var updatedAt time.Time
	err := s.db.QueryRowContext(ctx, `SELECT task_json, plan_json, acceptance_json, COALESCE(result_json, '{}'::jsonb), updated_at FROM go_workflows WHERE workflow_id = $1`, workflowID).Scan(&taskJSON, &planJSON, &acceptanceJSON, &resultJSON, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.WorkflowRecord{}, false, nil
	}
	if err != nil {
		return domain.WorkflowRecord{}, false, err
	}
	record := domain.WorkflowRecord{UpdatedAt: updatedAt}
	if err := json.Unmarshal(taskJSON, &record.Task); err != nil {
		return domain.WorkflowRecord{}, false, err
	}
	if err := json.Unmarshal(planJSON, &record.Plan); err != nil {
		return domain.WorkflowRecord{}, false, err
	}
	if err := json.Unmarshal(acceptanceJSON, &record.Acceptance); err != nil {
		return domain.WorkflowRecord{}, false, err
	}
	if string(resultJSON) != "{}" && string(resultJSON) != "null" {
		var result domain.AgentResult
		if err := json.Unmarshal(resultJSON, &result); err != nil {
			return domain.WorkflowRecord{}, false, err
		}
		record.Result = &result
	}
	return record, true, nil
}

func (s *PostgresStore) ListWorkflows(ctx context.Context) ([]domain.WorkflowRecord, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT workflow_id FROM go_workflows ORDER BY updated_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var records []domain.WorkflowRecord
	for rows.Next() {
		var workflowID string
		if err := rows.Scan(&workflowID); err != nil {
			return nil, err
		}
		record, ok, err := s.GetWorkflow(ctx, workflowID)
		if err != nil {
			return nil, err
		}
		if ok {
			records = append(records, record)
		}
	}
	return records, rows.Err()
}

func (s *PostgresStore) WorkflowCount(ctx context.Context) (int, error) {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM go_workflows`).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

func (s *PostgresStore) GetSessionState(ctx context.Context, sessionID string, branch string) (domain.SessionState, bool, error) {
	var stateJSON []byte
	state := domain.SessionState{}
	err := s.db.QueryRowContext(ctx, `SELECT version, prompt_version, context_version, state_json, updated_at, storage_mode FROM go_session_states WHERE session_id = $1 AND branch = $2`, sessionID, branch).Scan(&state.Version, &state.PromptVersion, &state.ContextVersion, &stateJSON, &state.UpdatedAt, &state.StorageMode)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.SessionState{}, false, nil
	}
	if err != nil {
		return domain.SessionState{}, false, err
	}
	state.SessionID = sessionID
	state.Branch = branch
	if err := json.Unmarshal(stateJSON, &state.State); err != nil {
		return domain.SessionState{}, false, err
	}
	return state, true, nil
}

func (s *PostgresStore) SaveSessionState(ctx context.Context, sessionID string, branch string, stateValue map[string]any, promptVersion string, contextVersion string, expectedVersion *int) (domain.SessionState, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.SessionState{}, err
	}
	defer tx.Rollback()
	currentVersion := 0
	var exists bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM go_session_states WHERE session_id = $1 AND branch = $2)`, sessionID, branch).Scan(&exists); err != nil {
		return domain.SessionState{}, err
	}
	if exists {
		if err := tx.QueryRowContext(ctx, `SELECT version FROM go_session_states WHERE session_id = $1 AND branch = $2 FOR UPDATE`, sessionID, branch).Scan(&currentVersion); err != nil {
			return domain.SessionState{}, err
		}
	}
	if expectedVersion != nil && *expectedVersion != currentVersion {
		return domain.SessionState{}, errors.New("session state version conflict")
	}
	next := domain.SessionState{SessionID: sessionID, Branch: branch, Version: currentVersion + 1, PromptVersion: promptVersion, ContextVersion: contextVersion, State: cloneMap(stateValue), UpdatedAt: time.Now().UTC(), StorageMode: s.storageMode}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO go_session_states (session_id, branch, version, prompt_version, context_version, state_json, updated_at, storage_mode)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		ON CONFLICT (session_id, branch) DO UPDATE
		SET version = EXCLUDED.version,
		    prompt_version = EXCLUDED.prompt_version,
		    context_version = EXCLUDED.context_version,
		    state_json = EXCLUDED.state_json,
		    updated_at = EXCLUDED.updated_at,
		    storage_mode = EXCLUDED.storage_mode`,
		next.SessionID, next.Branch, next.Version, next.PromptVersion, next.ContextVersion, mustJSON(next.State), next.UpdatedAt, next.StorageMode,
	)
	if err != nil {
		return domain.SessionState{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.SessionState{}, err
	}
	return cloneSessionState(next), nil
}

func (s *PostgresStore) RecordInvalidation(ctx context.Context, sessionID string, branch string, reason string, payload map[string]any) (domain.InvalidationEvent, error) {
	event := domain.InvalidationEvent{SessionID: sessionID, Branch: branch, Reason: reason, Payload: cloneMap(payload), LoggedAt: time.Now().UTC(), StorageMode: s.storageMode}
	_, err := s.db.ExecContext(ctx, `INSERT INTO go_invalidations (session_id, branch, reason, payload_json, logged_at, storage_mode) VALUES ($1, $2, $3, $4, $5, $6)`, event.SessionID, event.Branch, event.Reason, mustJSON(event.Payload), event.LoggedAt, event.StorageMode)
	return cloneInvalidation(event), err
}

func (s *PostgresStore) RecentInvalidations(ctx context.Context, sessionID string, branch string, limit int) ([]domain.InvalidationEvent, error) {
	if limit <= 0 {
		limit = 20
	}
	rows, err := s.db.QueryContext(ctx, `SELECT reason, payload_json, logged_at, storage_mode FROM go_invalidations WHERE session_id = $1 AND branch = $2 ORDER BY logged_at DESC LIMIT $3`, sessionID, branch, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]domain.InvalidationEvent, 0, limit)
	for rows.Next() {
		var payloadJSON []byte
		item := domain.InvalidationEvent{SessionID: sessionID, Branch: branch}
		if err := rows.Scan(&item.Reason, &payloadJSON, &item.LoggedAt, &item.StorageMode); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(payloadJSON, &item.Payload); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) UpsertVectorChunks(ctx context.Context, chunks []domain.VectorChunk) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, chunk := range chunks {
		copyChunk := cloneVectorChunk(chunk)
		if strings.TrimSpace(copyChunk.ChunkID) == "" {
			continue
		}
		vectorValue := ragMemoryVectorLiteral(copyChunk.Embedding)
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_vector_chunks (chunk_id, session_id, branch, source, source_id, chunk_index, text_content, normalized_text, terms_json, embedding_json, embedding_vector, metadata_json, created_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CASE WHEN $11 = '' THEN NULL ELSE $11::vector END, $12, $13)
			ON CONFLICT (chunk_id) DO UPDATE
			SET session_id = EXCLUDED.session_id,
			    branch = EXCLUDED.branch,
			    source = EXCLUDED.source,
			    source_id = EXCLUDED.source_id,
			    chunk_index = EXCLUDED.chunk_index,
			    text_content = EXCLUDED.text_content,
			    normalized_text = EXCLUDED.normalized_text,
			    terms_json = EXCLUDED.terms_json,
			    embedding_json = EXCLUDED.embedding_json,
			    embedding_vector = EXCLUDED.embedding_vector,
			    metadata_json = EXCLUDED.metadata_json,
			    created_at = EXCLUDED.created_at`,
			copyChunk.ChunkID, copyChunk.SessionID, copyChunk.Branch, copyChunk.Source, copyChunk.SourceID, copyChunk.ChunkIndex, copyChunk.Text, copyChunk.NormalizedText, mustJSON(copyChunk.Terms), mustJSON(copyChunk.Embedding), vectorValue, mustJSON(copyChunk.Metadata), copyChunk.CreatedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) ListVectorChunks(ctx context.Context, sessionID string, branch string, limit int) ([]domain.VectorChunk, error) {
	if limit <= 0 {
		limit = 200
	}
	rows, err := s.db.QueryContext(ctx, `SELECT chunk_id, session_id, branch, source, source_id, chunk_index, text_content, normalized_text, terms_json, embedding_json, metadata_json, created_at FROM go_vector_chunks WHERE ($1 = '' OR session_id = $1) AND ($2 = '' OR branch = $2) ORDER BY created_at DESC LIMIT $3`, strings.TrimSpace(sessionID), strings.TrimSpace(branch), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	chunks := make([]domain.VectorChunk, 0, limit)
	for rows.Next() {
		var termsJSON, embeddingJSON, metadataJSON []byte
		chunk := domain.VectorChunk{}
		if err := rows.Scan(&chunk.ChunkID, &chunk.SessionID, &chunk.Branch, &chunk.Source, &chunk.SourceID, &chunk.ChunkIndex, &chunk.Text, &chunk.NormalizedText, &termsJSON, &embeddingJSON, &metadataJSON, &chunk.CreatedAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(termsJSON, &chunk.Terms); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(embeddingJSON, &chunk.Embedding); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(metadataJSON, &chunk.Metadata); err != nil {
			return nil, err
		}
		chunks = append(chunks, chunk)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sort.Slice(chunks, func(i, j int) bool { return chunks[i].CreatedAt.After(chunks[j].CreatedAt) })
	return chunks, nil
}

func (s *PostgresStore) UpsertRAGDocuments(ctx context.Context, documents []domain.RAGDocument) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, document := range documents {
		copyDocument := cloneRAGDocument(document)
		copyDocument.DocumentID = strings.TrimSpace(copyDocument.DocumentID)
		if copyDocument.DocumentID == "" {
			continue
		}
		if copyDocument.CreatedAt.IsZero() {
			copyDocument.CreatedAt = time.Now().UTC()
		}
		if copyDocument.UpdatedAt.IsZero() {
			copyDocument.UpdatedAt = copyDocument.CreatedAt
		}
		if copyDocument.LastAccessedAt.IsZero() {
			copyDocument.LastAccessedAt = copyDocument.UpdatedAt
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_rag_documents (document_id, scope, owner_type, owner_id, source_type, source_ref, title, content_text, content_summary, metadata_json, importance, repo_id, branch, commit_sha, artifact_ids_json, created_at, updated_at, last_accessed_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
			ON CONFLICT (document_id) DO UPDATE
			SET scope = EXCLUDED.scope,
			    owner_type = EXCLUDED.owner_type,
			    owner_id = EXCLUDED.owner_id,
			    source_type = EXCLUDED.source_type,
			    source_ref = EXCLUDED.source_ref,
			    title = EXCLUDED.title,
			    content_text = EXCLUDED.content_text,
			    content_summary = EXCLUDED.content_summary,
			    metadata_json = EXCLUDED.metadata_json,
			    importance = EXCLUDED.importance,
			    repo_id = EXCLUDED.repo_id,
			    branch = EXCLUDED.branch,
			    commit_sha = EXCLUDED.commit_sha,
			    artifact_ids_json = EXCLUDED.artifact_ids_json,
			    created_at = EXCLUDED.created_at,
			    updated_at = EXCLUDED.updated_at,
			    last_accessed_at = EXCLUDED.last_accessed_at`,
			copyDocument.DocumentID,
			defaultString(copyDocument.Scope, "session"),
			strings.TrimSpace(copyDocument.OwnerType),
			strings.TrimSpace(copyDocument.OwnerID),
			strings.TrimSpace(copyDocument.SourceType),
			strings.TrimSpace(copyDocument.SourceRef),
			strings.TrimSpace(copyDocument.Title),
			copyDocument.ContentText,
			copyDocument.ContentSummary,
			mustJSON(copyDocument.Metadata),
			copyDocument.Importance,
			strings.TrimSpace(copyDocument.RepoID),
			defaultString(copyDocument.Branch, "default"),
			strings.TrimSpace(copyDocument.CommitSHA),
			mustJSON(copyDocument.ArtifactIDs),
			copyDocument.CreatedAt,
			copyDocument.UpdatedAt,
			copyDocument.LastAccessedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) ListRAGDocuments(ctx context.Context, scope string, ownerID string, limit int) ([]domain.RAGDocument, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx, `SELECT document_id, scope, owner_type, owner_id, source_type, source_ref, title, content_text, content_summary, metadata_json, importance, repo_id, branch, commit_sha, artifact_ids_json, created_at, updated_at, last_accessed_at FROM go_rag_documents WHERE ($1 = '' OR scope = $1) AND ($2 = '' OR owner_id = $2) ORDER BY updated_at DESC LIMIT $3`, strings.TrimSpace(scope), strings.TrimSpace(ownerID), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]domain.RAGDocument, 0, limit)
	for rows.Next() {
		var metadataJSON, artifactIDsJSON []byte
		item := domain.RAGDocument{}
		if err := rows.Scan(&item.DocumentID, &item.Scope, &item.OwnerType, &item.OwnerID, &item.SourceType, &item.SourceRef, &item.Title, &item.ContentText, &item.ContentSummary, &metadataJSON, &item.Importance, &item.RepoID, &item.Branch, &item.CommitSHA, &artifactIDsJSON, &item.CreatedAt, &item.UpdatedAt, &item.LastAccessedAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(metadataJSON, &item.Metadata); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(artifactIDsJSON, &item.ArtifactIDs); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) UpsertRAGMemories(ctx context.Context, memories []domain.RAGMemoryRecord) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, memory := range memories {
		copyMemory := cloneRAGMemory(memory)
		copyMemory.MemoryID = strings.TrimSpace(copyMemory.MemoryID)
		if copyMemory.MemoryID == "" {
			continue
		}
		if copyMemory.CreatedAt.IsZero() {
			copyMemory.CreatedAt = time.Now().UTC()
		}
		if copyMemory.UpdatedAt.IsZero() {
			copyMemory.UpdatedAt = copyMemory.CreatedAt
		}
		vectorValue := ragMemoryVectorLiteral(copyMemory.Embedding)
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_rag_memories (memory_id, memory_type, scope, owner_id, content_json, summary, embedding_json, embedding_vector, metadata_json, confidence, importance, repo_id, branch, commit_sha, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, CASE WHEN $8 = '' THEN NULL ELSE $8::vector END, $9, $10, $11, $12, $13, $14, $15, $16)
			ON CONFLICT (memory_id) DO UPDATE
			SET memory_type = EXCLUDED.memory_type,
			    scope = EXCLUDED.scope,
			    owner_id = EXCLUDED.owner_id,
			    content_json = EXCLUDED.content_json,
			    summary = EXCLUDED.summary,
			    embedding_json = EXCLUDED.embedding_json,
			    embedding_vector = EXCLUDED.embedding_vector,
			    metadata_json = EXCLUDED.metadata_json,
			    confidence = EXCLUDED.confidence,
			    importance = EXCLUDED.importance,
			    repo_id = EXCLUDED.repo_id,
			    branch = EXCLUDED.branch,
			    commit_sha = EXCLUDED.commit_sha,
			    created_at = EXCLUDED.created_at,
			    updated_at = EXCLUDED.updated_at`,
			copyMemory.MemoryID,
			defaultString(copyMemory.MemoryType, "episodic"),
			defaultString(copyMemory.Scope, "session"),
			strings.TrimSpace(copyMemory.OwnerID),
			mustJSON(copyMemory.Content),
			strings.TrimSpace(copyMemory.Summary),
			mustJSON(copyMemory.Embedding),
			vectorValue,
			mustJSON(copyMemory.Metadata),
			copyMemory.Confidence,
			copyMemory.Importance,
			strings.TrimSpace(copyMemory.RepoID),
			defaultString(copyMemory.Branch, "default"),
			strings.TrimSpace(copyMemory.CommitSHA),
			copyMemory.CreatedAt,
			copyMemory.UpdatedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) ListRAGMemories(ctx context.Context, scope string, ownerID string, limit int) ([]domain.RAGMemoryRecord, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx, `SELECT memory_id, memory_type, scope, owner_id, content_json, summary, embedding_json, metadata_json, confidence, importance, repo_id, branch, commit_sha, created_at, updated_at FROM go_rag_memories WHERE ($1 = '' OR scope = $1) AND ($2 = '' OR owner_id = $2) ORDER BY updated_at DESC LIMIT $3`, strings.TrimSpace(scope), strings.TrimSpace(ownerID), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]domain.RAGMemoryRecord, 0, limit)
	for rows.Next() {
		var contentJSON, embeddingJSON, metadataJSON []byte
		item := domain.RAGMemoryRecord{}
		if err := rows.Scan(&item.MemoryID, &item.MemoryType, &item.Scope, &item.OwnerID, &contentJSON, &item.Summary, &embeddingJSON, &metadataJSON, &item.Confidence, &item.Importance, &item.RepoID, &item.Branch, &item.CommitSHA, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(contentJSON, &item.Content); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(embeddingJSON, &item.Embedding); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(metadataJSON, &item.Metadata); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) UpsertRouteMemories(ctx context.Context, memories []domain.RouteMemoryRecord) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, memory := range memories {
		copyMemory := cloneRouteMemory(memory)
		copyMemory.RouteID = strings.TrimSpace(copyMemory.RouteID)
		if copyMemory.RouteID == "" {
			continue
		}
		if copyMemory.CreatedAt.IsZero() {
			copyMemory.CreatedAt = time.Now().UTC()
		}
		if copyMemory.UpdatedAt.IsZero() {
			copyMemory.UpdatedAt = copyMemory.CreatedAt
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_route_memories (route_id, session_id, task_id, parent_task_id, root_task_id, task_type, capability, complexity, project, repo_path, repo_fingerprint, branch, agent_id, provider, model_name, plan_mode, success, confidence, latency_ms, review_passed, tests_passed, cost_estimate, error_count, summary, task_signature_json, embedding_json, metadata_json, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29)
			ON CONFLICT (route_id) DO UPDATE
			SET session_id = EXCLUDED.session_id,
			    task_id = EXCLUDED.task_id,
			    parent_task_id = EXCLUDED.parent_task_id,
			    root_task_id = EXCLUDED.root_task_id,
			    task_type = EXCLUDED.task_type,
			    capability = EXCLUDED.capability,
			    complexity = EXCLUDED.complexity,
			    project = EXCLUDED.project,
			    repo_path = EXCLUDED.repo_path,
			    repo_fingerprint = EXCLUDED.repo_fingerprint,
			    branch = EXCLUDED.branch,
			    agent_id = EXCLUDED.agent_id,
			    provider = EXCLUDED.provider,
			    model_name = EXCLUDED.model_name,
			    plan_mode = EXCLUDED.plan_mode,
			    success = EXCLUDED.success,
			    confidence = EXCLUDED.confidence,
			    latency_ms = EXCLUDED.latency_ms,
			    review_passed = EXCLUDED.review_passed,
			    tests_passed = EXCLUDED.tests_passed,
			    cost_estimate = EXCLUDED.cost_estimate,
			    error_count = EXCLUDED.error_count,
			    summary = EXCLUDED.summary,
			    task_signature_json = EXCLUDED.task_signature_json,
			    embedding_json = EXCLUDED.embedding_json,
			    metadata_json = EXCLUDED.metadata_json,
			    created_at = EXCLUDED.created_at,
			    updated_at = EXCLUDED.updated_at`,
			copyMemory.RouteID,
			strings.TrimSpace(copyMemory.SessionID),
			strings.TrimSpace(copyMemory.TaskID),
			strings.TrimSpace(copyMemory.ParentTaskID),
			strings.TrimSpace(copyMemory.RootTaskID),
			defaultString(string(copyMemory.TaskType), string(domain.TaskTypeCode)),
			strings.TrimSpace(copyMemory.Capability),
			defaultString(string(copyMemory.Complexity), string(domain.ComplexityMedium)),
			strings.TrimSpace(copyMemory.Project),
			strings.TrimSpace(copyMemory.RepoPath),
			strings.TrimSpace(copyMemory.RepoFingerprint),
			defaultString(copyMemory.Branch, "default"),
			strings.TrimSpace(copyMemory.AgentID),
			strings.TrimSpace(copyMemory.Provider),
			strings.TrimSpace(copyMemory.ModelName),
			strings.TrimSpace(copyMemory.PlanMode),
			copyMemory.Success,
			copyMemory.Confidence,
			copyMemory.LatencyMS,
			copyMemory.ReviewPassed,
			copyMemory.TestsPassed,
			copyMemory.CostEstimate,
			copyMemory.ErrorCount,
			strings.TrimSpace(copyMemory.Summary),
			mustJSON(copyMemory.TaskSignature),
			mustJSON(copyMemory.Embedding),
			mustJSON(copyMemory.Metadata),
			copyMemory.CreatedAt,
			copyMemory.UpdatedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) ListRouteMemories(ctx context.Context, project string, repoFingerprint string, capability string, limit int) ([]domain.RouteMemoryRecord, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `SELECT route_id, session_id, task_id, parent_task_id, root_task_id, task_type, capability, complexity, project, repo_path, repo_fingerprint, branch, agent_id, provider, model_name, plan_mode, success, confidence, latency_ms, review_passed, tests_passed, cost_estimate, error_count, summary, task_signature_json, embedding_json, metadata_json, created_at, updated_at FROM go_route_memories WHERE ($1 = '' OR project = $1) AND ($2 = '' OR repo_fingerprint = $2) AND ($3 = '' OR capability = $3) ORDER BY updated_at DESC LIMIT $4`, strings.TrimSpace(project), strings.TrimSpace(repoFingerprint), strings.TrimSpace(capability), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]domain.RouteMemoryRecord, 0, limit)
	for rows.Next() {
		var taskType string
		var complexity string
		var taskSignatureJSON, embeddingJSON, metadataJSON []byte
		item := domain.RouteMemoryRecord{}
		if err := rows.Scan(&item.RouteID, &item.SessionID, &item.TaskID, &item.ParentTaskID, &item.RootTaskID, &taskType, &item.Capability, &complexity, &item.Project, &item.RepoPath, &item.RepoFingerprint, &item.Branch, &item.AgentID, &item.Provider, &item.ModelName, &item.PlanMode, &item.Success, &item.Confidence, &item.LatencyMS, &item.ReviewPassed, &item.TestsPassed, &item.CostEstimate, &item.ErrorCount, &item.Summary, &taskSignatureJSON, &embeddingJSON, &metadataJSON, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, err
		}
		item.TaskType = domain.TaskType(taskType)
		item.Complexity = domain.Complexity(complexity)
		if err := json.Unmarshal(taskSignatureJSON, &item.TaskSignature); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(embeddingJSON, &item.Embedding); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(metadataJSON, &item.Metadata); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *PostgresStore) UpsertVFSArtifacts(ctx context.Context, artifacts []domain.VFSArtifact) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, artifact := range artifacts {
		copyArtifact := cloneVFSArtifact(artifact)
		copyArtifact.ArtifactID = strings.TrimSpace(copyArtifact.ArtifactID)
		if copyArtifact.ArtifactID == "" {
			continue
		}
		if copyArtifact.CreatedAt.IsZero() {
			copyArtifact.CreatedAt = time.Now().UTC()
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_vfs_artifacts (artifact_id, storage_uri, sha256, size_bytes, mime_type, content_bytes, metadata_json, created_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
			ON CONFLICT (artifact_id) DO UPDATE
			SET storage_uri = EXCLUDED.storage_uri,
			    sha256 = EXCLUDED.sha256,
			    size_bytes = EXCLUDED.size_bytes,
			    mime_type = EXCLUDED.mime_type,
			    content_bytes = EXCLUDED.content_bytes,
			    metadata_json = EXCLUDED.metadata_json,
			    created_at = EXCLUDED.created_at`,
			copyArtifact.ArtifactID,
			copyArtifact.StorageURI,
			copyArtifact.SHA256,
			copyArtifact.SizeBytes,
			copyArtifact.MIMEType,
			copyArtifact.Content,
			mustJSON(copyArtifact.Metadata),
			copyArtifact.CreatedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) GetVFSArtifact(ctx context.Context, artifactID string) (domain.VFSArtifact, bool, error) {
	artifactID = strings.TrimSpace(artifactID)
	if artifactID == "" {
		return domain.VFSArtifact{}, false, nil
	}
	var metadataJSON []byte
	item := domain.VFSArtifact{}
	err := s.db.QueryRowContext(ctx, `SELECT artifact_id, storage_uri, sha256, size_bytes, mime_type, content_bytes, metadata_json, created_at FROM go_vfs_artifacts WHERE artifact_id = $1`, artifactID).Scan(&item.ArtifactID, &item.StorageURI, &item.SHA256, &item.SizeBytes, &item.MIMEType, &item.Content, &metadataJSON, &item.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.VFSArtifact{}, false, nil
	}
	if err != nil {
		return domain.VFSArtifact{}, false, err
	}
	if err := json.Unmarshal(metadataJSON, &item.Metadata); err != nil {
		return domain.VFSArtifact{}, false, err
	}
	return item, true, nil
}

func (s *PostgresStore) UpsertVFSCheckpoints(ctx context.Context, checkpoints []domain.VFSCheckpointRecord) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, checkpoint := range checkpoints {
		copyCheckpoint := cloneVFSCheckpoint(checkpoint)
		copyCheckpoint.Path = strings.TrimSpace(copyCheckpoint.Path)
		if copyCheckpoint.Path == "" {
			continue
		}
		if copyCheckpoint.CreatedAt.IsZero() {
			copyCheckpoint.CreatedAt = time.Now().UTC()
		}
		if copyCheckpoint.UpdatedAt.IsZero() {
			copyCheckpoint.UpdatedAt = copyCheckpoint.CreatedAt
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO go_vfs_checkpoints (path, task_id, agent_id, checkpoint_json, checksum, integrity, metadata_json, storage_uri, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
			ON CONFLICT (path) DO UPDATE
			SET task_id = EXCLUDED.task_id,
			    agent_id = EXCLUDED.agent_id,
			    checkpoint_json = EXCLUDED.checkpoint_json,
			    checksum = EXCLUDED.checksum,
			    integrity = EXCLUDED.integrity,
			    metadata_json = EXCLUDED.metadata_json,
			    storage_uri = EXCLUDED.storage_uri,
			    created_at = EXCLUDED.created_at,
			    updated_at = EXCLUDED.updated_at`,
			copyCheckpoint.Path,
			strings.TrimSpace(copyCheckpoint.TaskID),
			strings.TrimSpace(copyCheckpoint.AgentID),
			mustJSON(copyCheckpoint.Checkpoint),
			strings.TrimSpace(copyCheckpoint.Checksum),
			defaultString(copyCheckpoint.Integrity, "ok"),
			mustJSON(copyCheckpoint.Metadata),
			strings.TrimSpace(copyCheckpoint.StorageURI),
			copyCheckpoint.CreatedAt,
			copyCheckpoint.UpdatedAt,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *PostgresStore) GetVFSCheckpoint(ctx context.Context, path string) (domain.VFSCheckpointRecord, bool, error) {
	path = strings.TrimSpace(path)
	if path == "" {
		return domain.VFSCheckpointRecord{}, false, nil
	}
	var checkpointJSON, metadataJSON []byte
	item := domain.VFSCheckpointRecord{}
	err := s.db.QueryRowContext(ctx, `SELECT path, task_id, agent_id, checkpoint_json, checksum, integrity, metadata_json, storage_uri, created_at, updated_at FROM go_vfs_checkpoints WHERE path = $1`, path).Scan(&item.Path, &item.TaskID, &item.AgentID, &checkpointJSON, &item.Checksum, &item.Integrity, &metadataJSON, &item.StorageURI, &item.CreatedAt, &item.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.VFSCheckpointRecord{}, false, nil
	}
	if err != nil {
		return domain.VFSCheckpointRecord{}, false, err
	}
	if err := json.Unmarshal(checkpointJSON, &item.Checkpoint); err != nil {
		return domain.VFSCheckpointRecord{}, false, err
	}
	if err := json.Unmarshal(metadataJSON, &item.Metadata); err != nil {
		return domain.VFSCheckpointRecord{}, false, err
	}
	return item, true, nil
}

func (s *PostgresStore) Snapshot() map[string]any {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return map[string]any{
		"storage_mode": s.storageMode,
		"db_stats": map[string]any{
			"open_connections": s.db.Stats().OpenConnections,
			"in_use":           s.db.Stats().InUse,
			"idle":             s.db.Stats().Idle,
		},
		"workflow_count":       s.count(ctx, "go_workflows"),
		"session_count":        s.count(ctx, "go_session_states"),
		"invalidation_count":   s.count(ctx, "go_invalidations"),
		"vector_chunk_count":   s.count(ctx, "go_vector_chunks"),
		"rag_document_count":   s.count(ctx, "go_rag_documents"),
		"rag_memory_count":     s.count(ctx, "go_rag_memories"),
		"route_memory_count":   s.count(ctx, "go_route_memories"),
		"vfs_artifact_count":   s.count(ctx, "go_vfs_artifacts"),
		"vfs_checkpoint_count": s.count(ctx, "go_vfs_checkpoints"),
	}
}

func (s *PostgresStore) count(ctx context.Context, table string) int {
	if strings.TrimSpace(table) == "" {
		return 0
	}
	var n int
	if err := s.db.QueryRowContext(ctx, fmt.Sprintf("SELECT COUNT(*) FROM %s", table)).Scan(&n); err != nil {
		return 0
	}
	return n
}

func mustJSON(value any) []byte {
	data, _ := json.Marshal(value)
	if len(data) == 0 {
		return []byte("{}")
	}
	return data
}

func marshalNullable(value any) ([]byte, error) {
	if value == nil {
		return nil, nil
	}
	return json.Marshal(value)
}

func defaultString(value string, fallback string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return fallback
	}
	return trimmed
}

func ragMemoryVectorLiteral(values []float64) string {
	if len(values) == 0 || len(values) > 64 {
		return ""
	}
	return vectorLiteral(values)
}

func vectorLiteral(values []float64) string {
	if len(values) == 0 {
		return ""
	}
	parts := make([]string, 0, len(values))
	for _, value := range values {
		parts = append(parts, strconv.FormatFloat(value, 'f', 6, 64))
	}
	return "[" + strings.Join(parts, ",") + "]"
}
