package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash/fnv"
	"math"
	"os"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	_ "github.com/jackc/pgx/v5/stdlib"

	"sourcevcode-orchestrator/go-core/internal/app"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type legacyImportStats struct {
	memories        int
	trainedMemories int
	commands        int
	sessions        int
	vfsFiles        int
}

type legacyRawRow struct {
	table string
	data  map[string]any
}

type coreSQLImporter struct {
	store   state.Store
	manager *memory.Manager
	dryRun  bool
	stats   legacyImportStats
}

type coreImportConfig struct {
	repoID          string
	project         string
	branch          string
	scope           string
	repoPath        string
	repoFingerprint string
	limit           int
}

const (
	legacyMemoryBatchSize = 256
	legacyVectorDims      = 64
)

func importCoreSQL(cfg app.Config, args []string) error {
	_ = cfg
	fs := flag.NewFlagSet("import-core-sql", flag.ContinueOnError)
	statePath := fs.String("state-path", "", "state backend path")
	repoID := fs.String("repo-id", "legacy-core", "repository identifier for imported records")
	project := fs.String("project", "legacy-core", "project label for imported records")
	branch := fs.String("branch", "main", "branch label for imported records")
	scope := fs.String("scope", "deep-history", "default memory scope for imported records")
	repoPath := fs.String("repo-path", "legacy-sql://core", "logical repository path for imported records")
	repoFingerprint := fs.String("repo-fingerprint", "legacy-core-sql", "repository fingerprint for imported records")
	limit := fs.Int("limit", 0, "optional per-table limit for import")
	dryRun := fs.Bool("dry-run", false, "scan legacy tables without writing to go_* state")
	if err := fs.Parse(args); err != nil {
		return err
	}

	dbURL := strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	if dbURL == "" {
		connInfo := app.ResolvePostgresConnectionInfo()
		dbURL = connInfo.URL
	}
	if dbURL == "" {
		return errors.New("postgres database URL is empty")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Hour)
	defer cancel()

	rawDB, err := sql.Open("pgx", dbURL)
	if err != nil {
		return fmt.Errorf("open postgres connection: %w", err)
	}
	defer rawDB.Close()
	if err := rawDB.PingContext(ctx); err != nil {
		return fmt.Errorf("ping postgres connection: %w", err)
	}

	var (
		store   state.Store
		manager *memory.Manager
	)
	if !*dryRun {
		store, err = state.OpenStore(*statePath)
		if err != nil {
			return fmt.Errorf("open state store: %w", err)
		}
		manager = memory.NewManager(store)
	}

	importer := &coreSQLImporter{store: store, manager: manager, dryRun: *dryRun}
	plan := []struct {
		table string
		run   func(context.Context, *coreSQLImporter, *sql.DB, coreImportConfig) error
	}{
		{table: "memories", run: importCoreMemories},
		{table: "trained_memories", run: importCoreTrainedMemories},
		{table: "commands", run: importCoreCommands},
		{table: "sessions", run: importCoreSessions},
		{table: "vfs_files", run: importCoreVFSFiles},
	}
	importCfg := coreImportConfig{
		repoID:          *repoID,
		project:         *project,
		branch:          *branch,
		scope:           *scope,
		repoPath:        *repoPath,
		repoFingerprint: *repoFingerprint,
		limit:           *limit,
	}
	for _, item := range plan {
		exists, err := legacyTableExists(ctx, rawDB, item.table)
		if err != nil {
			return err
		}
		if !exists {
			fmt.Printf("skip core.%s: table not found\n", item.table)
			continue
		}
		if err := item.run(ctx, importer, rawDB, importCfg); err != nil {
			return err
		}
	}

	fmt.Printf("import-core-sql complete dry_run=%t memories=%d trained_memories=%d commands=%d sessions=%d vfs_files=%d\n",
		*dryRun,
		importer.stats.memories,
		importer.stats.trainedMemories,
		importer.stats.commands,
		importer.stats.sessions,
		importer.stats.vfsFiles,
	)
	return nil
}

func importCoreMemories(ctx context.Context, importer *coreSQLImporter, db *sql.DB, cfg coreImportConfig) error {
	rows, err := legacyRows(ctx, db, "memories", cfg.limit)
	if err != nil {
		return err
	}
	count := 0
	batch := make([]domain.RAGMemoryRecord, 0, legacyMemoryBatchSize)
	flush := func() error {
		if importer.dryRun || len(batch) == 0 {
			batch = batch[:0]
			return nil
		}
		if importer.store == nil {
			return errors.New("state store is not configured")
		}
		if err := importer.store.UpsertRAGMemories(ctx, batch); err != nil {
			return fmt.Errorf("flush core.memories batch ending at row %d: %w", count, err)
		}
		batch = batch[:0]
		return nil
	}
	for _, row := range rows {
		summary := summarizeLegacyRow(row.data, "summary", "title", "content", "memory_type")
		importance := clampFloat(bestFloat(row.data["importance_score"], row.data["score"]), 0.15, 1.0)
		confidence := clampFloat(bestFloat(row.data["confidence"], row.data["quality_score"]), 0.15, 1.0)
		record := domain.RAGMemoryRecord{
			MemoryID:   stableLegacyRecordID("core.memories", row.data),
			MemoryType: legacyFirstNonEmpty(mapString(row.data["memory_type"]), "legacy_memory"),
			Scope:      normalizeLegacyScope(row.data, cfg.scope),
			OwnerID:    legacyOwnerID(row.data, cfg.project),
			Content: map[string]any{
				"legacy_table": "core.memories",
				"legacy_row":   row.data,
			},
			Summary: summary,
			Metadata: buildLegacyMetadata(row.data, map[string]any{
				"memory_layer":  "cold",
				"legacy_table":  "core.memories",
				"source_kind":   "legacy_memory",
				"imported_from": "core.memories",
				"repo_path":     cfg.repoPath,
				"repo_fprint":   cfg.repoFingerprint,
			}),
			Confidence: confidence,
			Importance: importance,
			RepoID:     cfg.repoID,
			Branch:     normalizeLegacyBranch(row.data, cfg.branch),
			CreatedAt:  parseLegacyRowTime(row.data),
			UpdatedAt:  parseLegacyRowTime(row.data),
		}
		record.Embedding = legacyEmbedText(legacyMemoryEmbeddingInput(record.Summary, row.data), legacyVectorDims)
		batch = append(batch, record)
		count++
		if len(batch) >= legacyMemoryBatchSize {
			if err := flush(); err != nil {
				return err
			}
		}
	}
	if err := flush(); err != nil {
		return err
	}
	importer.stats.memories += count
	fmt.Printf("imported core.memories rows=%d\n", count)
	return nil
}

func importCoreTrainedMemories(ctx context.Context, importer *coreSQLImporter, db *sql.DB, cfg coreImportConfig) error {
	rows, err := legacyRows(ctx, db, "trained_memories", cfg.limit)
	if err != nil {
		return err
	}
	count := 0
	batch := make([]domain.RAGMemoryRecord, 0, legacyMemoryBatchSize)
	flush := func() error {
		if importer.dryRun || len(batch) == 0 {
			batch = batch[:0]
			return nil
		}
		if importer.store == nil {
			return errors.New("state store is not configured")
		}
		if err := importer.store.UpsertRAGMemories(ctx, batch); err != nil {
			return fmt.Errorf("flush core.trained_memories batch ending at row %d: %w", count, err)
		}
		batch = batch[:0]
		return nil
	}
	for _, row := range rows {
		quality := clampFloat(bestFloat(row.data["quality_score"], row.data["importance_score"]), 0.45, 1.0)
		record := domain.RAGMemoryRecord{
			MemoryID:   stableLegacyRecordID("core.trained_memories", row.data),
			MemoryType: legacyFirstNonEmpty(mapString(row.data["memory_domain"]), "trained_memory"),
			Scope:      normalizeLegacyScope(row.data, "distilled"),
			OwnerID:    legacyOwnerID(row.data, cfg.project),
			Content: map[string]any{
				"legacy_table": "core.trained_memories",
				"legacy_row":   row.data,
			},
			Summary: summarizeLegacyRow(row.data, "summary", "memory_domain", "content"),
			Metadata: buildLegacyMetadata(row.data, map[string]any{
				"memory_layer":  "distilled",
				"legacy_table":  "core.trained_memories",
				"source_kind":   "trained_memory",
				"imported_from": "core.trained_memories",
				"quality_score": quality,
				"repo_path":     cfg.repoPath,
				"repo_fprint":   cfg.repoFingerprint,
			}),
			Confidence: quality,
			Importance: quality,
			RepoID:     cfg.repoID,
			Branch:     normalizeLegacyBranch(row.data, cfg.branch),
			CreatedAt:  parseLegacyRowTime(row.data),
			UpdatedAt:  parseLegacyRowTime(row.data),
		}
		record.Embedding = legacyEmbedText(legacyMemoryEmbeddingInput(record.Summary, row.data), legacyVectorDims)
		batch = append(batch, record)
		count++
		if len(batch) >= legacyMemoryBatchSize {
			if err := flush(); err != nil {
				return err
			}
		}
	}
	if err := flush(); err != nil {
		return err
	}
	importer.stats.trainedMemories += count
	fmt.Printf("imported core.trained_memories rows=%d\n", count)
	return nil
}

func importCoreCommands(ctx context.Context, importer *coreSQLImporter, db *sql.DB, cfg coreImportConfig) error {
	rows, err := legacyRows(ctx, db, "commands", cfg.limit)
	if err != nil {
		return err
	}
	count := 0
	for _, row := range rows {
		doc := buildLegacyDocument(row.data, cfg, legacyDocumentSpec{
			idPrefix:     "core.commands",
			defaultScope: "history",
			titleKeys:    []string{"command", "title", "name", "status"},
			summaryKeys:  []string{"description", "command", "status"},
			sourceType:   "legacy_command",
			sourceKind:   "command_log",
			memoryLayer:  "warm",
			contentText:  stringifyAny(row.data),
		})
		if !importer.dryRun {
			if err := importer.manager.IngestDocument(ctx, doc); err != nil {
				return fmt.Errorf("import core.commands %s: %w", doc.DocumentID, err)
			}
		}
		count++
	}
	importer.stats.commands += count
	fmt.Printf("imported core.commands rows=%d\n", count)
	return nil
}

func importCoreSessions(ctx context.Context, importer *coreSQLImporter, db *sql.DB, cfg coreImportConfig) error {
	rows, err := legacyRows(ctx, db, "sessions", cfg.limit)
	if err != nil {
		return err
	}
	count := 0
	for _, row := range rows {
		doc := buildLegacyDocument(row.data, cfg, legacyDocumentSpec{
			idPrefix:     "core.sessions",
			defaultScope: "history",
			titleKeys:    []string{"title", "session_name", "status", "session_id"},
			summaryKeys:  []string{"summary", "status", "session_id"},
			sourceType:   "legacy_session",
			sourceKind:   "session_log",
			memoryLayer:  "cold",
			contentText:  stringifyAny(row.data),
		})
		if !importer.dryRun {
			if err := importer.manager.IngestDocument(ctx, doc); err != nil {
				return fmt.Errorf("import core.sessions %s: %w", doc.DocumentID, err)
			}
		}
		count++
	}
	importer.stats.sessions += count
	fmt.Printf("imported core.sessions rows=%d\n", count)
	return nil
}

func importCoreVFSFiles(ctx context.Context, importer *coreSQLImporter, db *sql.DB, cfg coreImportConfig) error {
	rows, err := legacyRows(ctx, db, "vfs_files", cfg.limit)
	if err != nil {
		return err
	}
	count := 0
	for _, row := range rows {
		doc := buildLegacyDocument(row.data, cfg, legacyDocumentSpec{
			idPrefix:     "core.vfs_files",
			defaultScope: "history",
			titleKeys:    []string{"path", "file_path", "name"},
			summaryKeys:  []string{"summary", "path", "file_path"},
			sourceType:   "legacy_vfs_file",
			sourceKind:   "vfs_file",
			memoryLayer:  "cold",
			contentText:  summarizeLegacyVFSContent(row.data),
		})
		if !importer.dryRun {
			if err := importer.manager.IngestDocument(ctx, doc); err != nil {
				return fmt.Errorf("import core.vfs_files %s: %w", doc.DocumentID, err)
			}
		}
		count++
	}
	importer.stats.vfsFiles += count
	fmt.Printf("imported core.vfs_files rows=%d\n", count)
	return nil
}

type legacyDocumentSpec struct {
	idPrefix     string
	defaultScope string
	titleKeys    []string
	summaryKeys  []string
	sourceType   string
	sourceKind   string
	memoryLayer  string
	contentText  string
}

func buildLegacyDocument(data map[string]any, cfg coreImportConfig, spec legacyDocumentSpec) domain.RAGDocument {
	createdAt := parseLegacyRowTime(data)
	updatedAt := parseLegacyRowTime(data)
	return domain.RAGDocument{
		DocumentID:     stableLegacyRecordID(spec.idPrefix, data),
		Scope:          normalizeLegacyScope(data, spec.defaultScope),
		OwnerType:      "legacy-core",
		OwnerID:        legacyOwnerID(data, cfg.project),
		SourceType:     spec.sourceType,
		SourceRef:      legacyExternalRef(spec.idPrefix, data),
		Title:          summarizeLegacyRow(data, spec.titleKeys...),
		ContentText:    spec.contentText,
		ContentSummary: summarizeLegacyRow(data, spec.summaryKeys...),
		Metadata:       buildLegacyMetadata(data, map[string]any{"memory_layer": spec.memoryLayer, "legacy_table": spec.idPrefix, "source_kind": spec.sourceKind, "imported_from": spec.idPrefix, "repo_path": cfg.repoPath, "repo_fprint": cfg.repoFingerprint}),
		Importance:     clampFloat(bestFloat(data["importance_score"], data["quality_score"]), 0.2, 1.0),
		RepoID:         cfg.repoID,
		Branch:         normalizeLegacyBranch(data, cfg.branch),
		CreatedAt:      createdAt,
		UpdatedAt:      updatedAt,
		LastAccessedAt: updatedAt,
	}
}

func legacyTableExists(ctx context.Context, db *sql.DB, table string) (bool, error) {
	var exists bool
	query := `SELECT EXISTS (
		SELECT 1
		FROM information_schema.tables
		WHERE table_schema = 'core' AND table_name = $1
	)`
	if err := db.QueryRowContext(ctx, query, table).Scan(&exists); err != nil {
		return false, fmt.Errorf("check core.%s existence: %w", table, err)
	}
	return exists, nil
}

func legacyRows(ctx context.Context, db *sql.DB, table string, limit int) ([]legacyRawRow, error) {
	query := fmt.Sprintf("SELECT row_to_json(t) FROM core.%s t", table)
	if limit > 0 {
		query = fmt.Sprintf("%s LIMIT %d", query, limit)
	}
	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query core.%s rows: %w", table, err)
	}
	defer rows.Close()

	results := make([]legacyRawRow, 0, 128)
	for rows.Next() {
		var payload []byte
		if err := rows.Scan(&payload); err != nil {
			return nil, fmt.Errorf("scan core.%s row: %w", table, err)
		}
		var data map[string]any
		if err := json.Unmarshal(payload, &data); err != nil {
			return nil, fmt.Errorf("decode core.%s row: %w", table, err)
		}
		data = sanitizeLegacyMap(data)
		results = append(results, legacyRawRow{table: table, data: data})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate core.%s rows: %w", table, err)
	}
	return results, nil
}

func summarizeLegacyVFSContent(data map[string]any) string {
	text := summarizeLegacyRow(data, "content", "body", "text")
	if strings.TrimSpace(text) != "" {
		return text
	}
	return stringifyAny(data)
}

func stableLegacyRecordID(prefix string, data map[string]any) string {
	keys := []string{"memory_id", "trained_memory_id", "command_id", "session_id", "file_id", "id"}
	for _, key := range keys {
		if value := strings.TrimSpace(mapString(data[key])); value != "" {
			return legacyStableID(prefix, value)
		}
	}
	payload := stringifyAny(data)
	if strings.TrimSpace(payload) == "" {
		payload = fmt.Sprintf("%s:%d", prefix, time.Now().UnixNano())
	}
	return legacyStableID(prefix, payload)
}

func legacyOwnerID(data map[string]any, fallback string) string {
	for _, key := range []string{"owner_id", "session_id", "user_id", "agent_id", "project_id"} {
		if value := strings.TrimSpace(mapString(data[key])); value != "" {
			return value
		}
	}
	return fallback
}

func legacyExternalRef(prefix string, data map[string]any) string {
	for _, key := range []string{"path", "file_path", "command_id", "session_id", "memory_id", "trained_memory_id", "id"} {
		if value := strings.TrimSpace(mapString(data[key])); value != "" {
			return fmt.Sprintf("%s:%s", prefix, value)
		}
	}
	return prefix
}

func normalizeLegacyBranch(data map[string]any, fallback string) string {
	for _, key := range []string{"branch", "branch_name", "git_branch"} {
		if value := strings.TrimSpace(mapString(data[key])); value != "" {
			return normalizeBranchValue(value)
		}
	}
	return normalizeBranchValue(fallback)
}

func normalizeLegacyScope(data map[string]any, fallback string) string {
	for _, key := range []string{"scope", "memory_scope", "session_scope"} {
		if value := strings.TrimSpace(mapString(data[key])); value != "" {
			return normalizeLegacyScopeValue(value)
		}
	}
	return normalizeLegacyScopeValue(fallback)
}

func normalizeLegacyScopeValue(value string) string {
	switch strings.TrimSpace(strings.ToLower(value)) {
	case "task", "branch", "project", "session", "agent", "capability", "global", "history", "deep-history", "distilled":
		return strings.TrimSpace(strings.ToLower(value))
	default:
		return "history"
	}
}

func parseLegacyRowTime(data map[string]any) time.Time {
	for _, key := range []string{"updated_at", "created_at", "timestamp", "occurred_at", "last_seen_at"} {
		if value := mapString(data[key]); strings.TrimSpace(value) != "" {
			if parsed := parseLegacyTime(value); !parsed.IsZero() {
				return parsed.UTC()
			}
		}
	}
	return time.Now().UTC()
}

func summarizeLegacyRow(data map[string]any, keys ...string) string {
	for _, key := range keys {
		if text := strings.TrimSpace(mapString(data[key])); text != "" {
			return trimSummary(text)
		}
	}
	if content, ok := data["content"].(map[string]any); ok {
		for _, key := range []string{"summary", "text", "body"} {
			if text := strings.TrimSpace(mapString(content[key])); text != "" {
				return trimSummary(text)
			}
		}
	}
	payload := stringifyAny(data)
	return trimSummary(payload)
}

func buildLegacyMetadata(data map[string]any, base map[string]any) map[string]any {
	metadata := sanitizeLegacyMap(cloneMap(base))
	data = sanitizeLegacyMap(data)
	keys := make([]string, 0, len(data))
	for key := range data {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if _, exists := metadata[key]; exists {
			continue
		}
		metadata[key] = data[key]
	}
	return metadata
}

func trimSummary(text string) string {
	text = strings.TrimSpace(sanitizeUTF8String(text))
	runes := []rune(text)
	if len(runes) <= 480 {
		return text
	}
	return strings.TrimSpace(string(runes[:480]))
}

func legacyMemoryEmbeddingInput(summary string, data map[string]any) string {
	parts := make([]string, 0, 3)
	if text := strings.TrimSpace(summary); text != "" {
		parts = append(parts, text)
	}
	for _, key := range []string{"content", "text", "body", "description", "command", "path", "file_path", "memory_type", "memory_domain"} {
		if text := strings.TrimSpace(mapString(data[key])); text != "" {
			parts = append(parts, text)
			break
		}
	}
	if content, ok := data["content"].(map[string]any); ok {
		for _, key := range []string{"summary", "text", "body"} {
			if text := strings.TrimSpace(mapString(content[key])); text != "" {
				parts = append(parts, text)
				break
			}
		}
	}
	if len(parts) == 0 {
		parts = append(parts, stringifyAny(data))
	}
	return strings.TrimSpace(strings.Join(parts, "\n\n"))
}

func legacyNormalizeVectorText(input string) string {
	input = strings.ToLower(strings.TrimSpace(input))
	if input == "" {
		return ""
	}
	replacer := strings.NewReplacer("\n", " ", "\t", " ", ",", " ", ";", " ", ":", " ", "(", " ", ")", " ", "[", " ", "]", " ", "{", " ", "}", " ", "_", " ", "-", " ", "/", " ", "\\", " ", ".", " ")
	normalized := replacer.Replace(input)
	return strings.Join(strings.Fields(normalized), " ")
}

func legacyEmbedText(text string, dims int) []float64 {
	if dims <= 0 {
		dims = legacyVectorDims
	}
	vector := make([]float64, dims)
	terms := strings.Fields(legacyNormalizeVectorText(text))
	if len(terms) == 0 {
		return vector
	}
	counts := map[string]int{}
	for _, term := range terms {
		counts[term]++
	}
	for term, count := range counts {
		h := fnv.New64a()
		_, _ = h.Write([]byte(term))
		sum := h.Sum64()
		idx := int(sum % uint64(dims))
		sign := 1.0
		if (sum>>8)&1 == 1 {
			sign = -1.0
		}
		weight := (1.0 + math.Min(2.0, float64(len(term))/12.0)) * (1.0 + math.Log1p(float64(count)))
		vector[idx] += sign * weight
	}
	norm := 0.0
	for _, value := range vector {
		norm += value * value
	}
	if norm <= 0 {
		return vector
	}
	norm = math.Sqrt(norm)
	for i := range vector {
		vector[i] /= norm
	}
	return vector
}

func sanitizeLegacyMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	cleaned, _ := sanitizeLegacyValue(input).(map[string]any)
	if cleaned == nil {
		return map[string]any{}
	}
	return cleaned
}

func sanitizeLegacyValue(value any) any {
	switch typed := value.(type) {
	case nil:
		return nil
	case string:
		return sanitizeUTF8String(typed)
	case []byte:
		return sanitizeUTF8String(string(typed))
	case map[string]any:
		cleaned := make(map[string]any, len(typed))
		for key, item := range typed {
			cleaned[sanitizeUTF8String(key)] = sanitizeLegacyValue(item)
		}
		return cleaned
	case []any:
		cleaned := make([]any, len(typed))
		for i, item := range typed {
			cleaned[i] = sanitizeLegacyValue(item)
		}
		return cleaned
	case []string:
		cleaned := make([]string, len(typed))
		for i, item := range typed {
			cleaned[i] = sanitizeUTF8String(item)
		}
		return cleaned
	default:
		return typed
	}
}

func sanitizeUTF8String(text string) string {
	if utf8.ValidString(text) {
		return text
	}
	buf := make([]rune, 0, len(text))
	for len(text) > 0 {
		r, size := utf8.DecodeRuneInString(text)
		if r == utf8.RuneError && size == 1 {
			buf = append(buf, '�')
			text = text[1:]
			continue
		}
		buf = append(buf, r)
		text = text[size:]
	}
	return string(buf)
}

func mapString(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case json.Number:
		return typed.String()
	case float64:
		return fmt.Sprintf("%.0f", typed)
	case float32:
		return fmt.Sprintf("%.0f", typed)
	case int:
		return fmt.Sprintf("%d", typed)
	case int64:
		return fmt.Sprintf("%d", typed)
	case int32:
		return fmt.Sprintf("%d", typed)
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case nil:
		return ""
	default:
		return stringifyAny(typed)
	}
}

func bestFloat(values ...any) float64 {
	for _, value := range values {
		switch typed := value.(type) {
		case float64:
			if typed > 0 {
				return typed
			}
		case float32:
			if typed > 0 {
				return float64(typed)
			}
		case int:
			if typed > 0 {
				return float64(typed)
			}
		case int64:
			if typed > 0 {
				return float64(typed)
			}
		case json.Number:
			if parsed, err := typed.Float64(); err == nil && parsed > 0 {
				return parsed
			}
		case string:
			if strings.TrimSpace(typed) == "" {
				continue
			}
			if parsed, err := json.Number(typed).Float64(); err == nil && parsed > 0 {
				return parsed
			}
		}
	}
	return 0
}

func clampFloat(value, minValue, maxValue float64) float64 {
	if value < minValue {
		return minValue
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func legacyFirstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}
