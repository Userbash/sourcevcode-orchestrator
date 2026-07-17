package main

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/app"
	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
	"sourcevcode-orchestrator/go-core/internal/state"
)

const fableDatasetID = "Glint-Research/Fable-5-traces"

type fableImportOptions struct {
	Source    string
	StatePath string
	Project   string
	RepoID    string
	Branch    string
	Scope     string
	OwnerID   string
	DryRun    bool
}

type fableImportSummary struct {
	Rows            int
	Documents       int
	ReasoningTraces int
	Skipped         int
	Failures        []string
}

func importFableTraces(cfg app.Config, args []string) error {
	flags := flag.NewFlagSet("import-fable-traces", flag.ExitOnError)
	source := flags.String("source", "", "path to Fable traces JSON, JSONL, or directory")
	statePath := flags.String("state-path", cfg.StatePath, "deprecated; ignored because import writes to the database store")
	project := flags.String("project", "fable-traces", "project label attached to imported traces")
	repoID := flags.String("repo-id", "fable-traces", "repo id assigned to imported RAG records")
	branch := flags.String("branch", "fable-import", "branch assigned to imported records when missing")
	scope := flags.String("scope", "project", "RAG scope for imported traces")
	ownerID := flags.String("owner-id", "fable-traces", "owner id assigned to imported documents and memories")
	dryRun := flags.Bool("dry-run", false, "parse traces without writing them to the store")
	_ = flags.Parse(args)

	options := fableImportOptions{
		Source:    strings.TrimSpace(*source),
		StatePath: strings.TrimSpace(*statePath),
		Project:   strings.TrimSpace(*project),
		RepoID:    strings.TrimSpace(*repoID),
		Branch:    normalizeBranchValue(*branch),
		Scope:     strings.TrimSpace(*scope),
		OwnerID:   strings.TrimSpace(*ownerID),
		DryRun:    *dryRun,
	}
	if options.Source == "" {
		return fmt.Errorf("source path is required")
	}
	if options.Scope == "" {
		options.Scope = "project"
	}
	if options.OwnerID == "" {
		options.OwnerID = options.RepoID
	}

	rows, err := loadFableTraceRows(options.Source)
	if err != nil {
		return err
	}

	ctx := context.Background()
	var manager *memory.Manager
	if !options.DryRun {
		store, err := state.OpenStore(options.StatePath)
		if err != nil {
			return err
		}
		manager = memory.NewManager(store)
	}

	summary := &fableImportSummary{}
	for index, row := range rows {
		summary.Rows++
		doc, trace, skipReason := buildFableArtifacts(row, index, options)
		if skipReason != "" {
			summary.Skipped++
			summary.Failures = append(summary.Failures, skipReason)
			continue
		}
		if options.DryRun {
			summary.Documents++
			summary.ReasoningTraces++
			continue
		}
		if err := manager.IngestDocument(ctx, doc); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("document %s: %v", doc.DocumentID, err))
			continue
		}
		if err := manager.RecordReasoningTrace(ctx, trace); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("trace %s: %v", trace.TraceID, err))
			continue
		}
		summary.Documents++
		summary.ReasoningTraces++
	}

	fmt.Printf("fable trace import: rows=%d documents=%d reasoning_traces=%d skipped=%d failures=%d dry_run=%t dataset=%s\n",
		summary.Rows,
		summary.Documents,
		summary.ReasoningTraces,
		summary.Skipped,
		len(summary.Failures),
		options.DryRun,
		fableDatasetID,
	)
	for _, failure := range summary.Failures {
		fmt.Printf("warning: %s\n", failure)
	}
	return nil
}

func loadFableTraceRows(source string) ([]map[string]any, error) {
	info, err := os.Stat(source)
	if err != nil {
		return nil, err
	}
	if info.IsDir() {
		files, err := filepath.Glob(filepath.Join(source, "*.json*"))
		if err != nil {
			return nil, err
		}
		sort.Strings(files)
		var rows []map[string]any
		for _, path := range files {
			loaded, err := loadFableTraceRows(path)
			if err != nil {
				return nil, err
			}
			rows = append(rows, loaded...)
		}
		return rows, nil
	}
	if strings.HasSuffix(strings.ToLower(source), ".jsonl") {
		return loadFableJSONL(source)
	}
	return loadFableJSON(source)
}

func loadFableJSON(path string) ([]map[string]any, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var list []map[string]any
	if err := json.Unmarshal(data, &list); err == nil {
		return list, nil
	}
	var single map[string]any
	if err := json.Unmarshal(data, &single); err == nil {
		return []map[string]any{single}, nil
	}
	return nil, fmt.Errorf("unsupported JSON structure in %s", path)
}

func loadFableJSONL(path string) ([]map[string]any, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	buffer := make([]byte, 0, 1024*1024)
	scanner.Buffer(buffer, 8*1024*1024)
	rows := make([]map[string]any, 0)
	line := 0
	for scanner.Scan() {
		line++
		text := strings.TrimSpace(scanner.Text())
		if text == "" {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal([]byte(text), &row); err != nil {
			return nil, fmt.Errorf("decode %s line %d: %w", path, line, err)
		}
		rows = append(rows, row)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return rows, nil
}

func buildFableArtifacts(row map[string]any, index int, options fableImportOptions) (domain.RAGDocument, domain.ReasoningTrace, string) {
	createdAt := fableTime(row["created_at"])
	if createdAt.IsZero() {
		createdAt = time.Now().UTC()
	}
	uid := firstNonEmptyString(row["uid"], row["id"], row["trace_id"], fmt.Sprintf("row-%d", index+1))
	sessionID := firstNonEmptyString(row["session"], row["session_id"], uid)
	modelName := firstNonEmptyString(row["model"], row["model_name"], "unknown")
	contextText := firstNonEmptyString(row["context"], row["input"], row["prompt"])
	reasoningText := firstNonEmptyString(row["cot"], row["reasoning"], row["trace"])
	outputText := firstNonEmptyString(row["output"], row["result"], row["answer"])
	completionText := firstNonEmptyString(row["completion"], row["response"])
	if contextText == "" && reasoningText == "" && outputText == "" && completionText == "" {
		return domain.RAGDocument{}, domain.ReasoningTrace{}, fmt.Sprintf("row %d skipped: no textual content", index+1)
	}

	taskType := domain.TaskType(firstNonEmptyString(row["output_type"], row["task_type"], "analysis"))
	title := fmt.Sprintf("Fable trace %s", uid)
	body := buildFableBody(contextText, reasoningText, outputText, completionText)
	summary := summarizeFableTrace(contextText, reasoningText, outputText, completionText)
	documentID := stableFableID("document", uid, modelName, summary)
	traceID := stableFableID("trace", uid, modelName, summary)
	metadata := map[string]any{
		"dataset":        fableDatasetID,
		"project":        options.Project,
		"repo_id":        options.RepoID,
		"import_source":  options.Source,
		"uid":            uid,
		"session":        sessionID,
		"origin":         firstNonEmptyString(row["origin"], row["source"], "fable"),
		"model_name":     modelName,
		"task_type":      string(taskType),
		"reasoning_mode": "fable_pytrace",
		"raw":            row,
	}

	doc := domain.RAGDocument{
		DocumentID:     documentID,
		Scope:          options.Scope,
		OwnerType:      "dataset",
		OwnerID:        options.OwnerID,
		SourceType:     "reasoning_trace_dataset",
		SourceRef:      fableDatasetID,
		Title:          title,
		ContentText:    body,
		ContentSummary: summary,
		Metadata:       cloneMap(metadata),
		Importance:     0.82,
		RepoID:         options.RepoID,
		Branch:         options.Branch,
		CreatedAt:      createdAt,
		UpdatedAt:      createdAt,
		LastAccessedAt: createdAt,
	}

	trace := domain.ReasoningTrace{
		TraceID:           traceID,
		SessionID:         sessionID,
		TaskID:            stableFableID("task", uid, modelName),
		AgentID:           "fable-import",
		Provider:          "dataset",
		ModelName:         modelName,
		TaskType:          taskType,
		Branch:            options.Branch,
		PromptSummary:     shortenFable(contextText, 280),
		ReflectionSummary: shortenFable(reasoningText, 360),
		ResultSummary:     shortenFable(firstNonEmpty(outputText, completionText), 280),
		ReasoningMode:     "fable_pytrace",
		RetrievalUsed:     false,
		DecisionPoints:    fableDecisionPoints(row),
		Metadata:          cloneMap(metadata),
		CreatedAt:         createdAt,
	}

	return doc, trace, ""
}

func buildFableBody(contextText string, reasoningText string, outputText string, completionText string) string {
	sections := make([]string, 0, 4)
	if strings.TrimSpace(contextText) != "" {
		sections = append(sections, "Context\n"+strings.TrimSpace(contextText))
	}
	if strings.TrimSpace(reasoningText) != "" {
		sections = append(sections, "Reasoning\n"+strings.TrimSpace(reasoningText))
	}
	if strings.TrimSpace(outputText) != "" {
		sections = append(sections, "Output\n"+strings.TrimSpace(outputText))
	}
	if strings.TrimSpace(completionText) != "" {
		sections = append(sections, "Completion\n"+strings.TrimSpace(completionText))
	}
	return strings.Join(sections, "\n\n")
}

func summarizeFableTrace(contextText string, reasoningText string, outputText string, completionText string) string {
	return shortenFable(firstNonEmpty(outputText, completionText, reasoningText, contextText), 220)
}

func fableDecisionPoints(row map[string]any) []domain.ReasoningDecisionPoint {
	points := make([]domain.ReasoningDecisionPoint, 0, 3)
	if text := firstNonEmptyString(row["context"], row["input"], row["prompt"]); text != "" {
		points = append(points, domain.ReasoningDecisionPoint{Kind: "context", Summary: shortenFable(text, 220)})
	}
	if text := firstNonEmptyString(row["cot"], row["reasoning"], row["trace"]); text != "" {
		points = append(points, domain.ReasoningDecisionPoint{Kind: "reasoning", Summary: shortenFable(text, 220)})
	}
	if text := firstNonEmptyString(row["output"], row["result"], row["answer"], row["completion"], row["response"]); text != "" {
		points = append(points, domain.ReasoningDecisionPoint{Kind: "result", Summary: shortenFable(text, 220)})
	}
	return points
}

func stableFableID(parts ...string) string {
	hash := sha1.New()
	for _, part := range parts {
		_, _ = hash.Write([]byte(strings.TrimSpace(part)))
		_, _ = hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil))
}

func firstNonEmptyString(values ...any) string {
	for _, value := range values {
		switch typed := value.(type) {
		case string:
			if trimmed := strings.TrimSpace(typed); trimmed != "" {
				return trimmed
			}
		case fmt.Stringer:
			if trimmed := strings.TrimSpace(typed.String()); trimmed != "" {
				return trimmed
			}
		}
	}
	return ""
}

func shortenFable(value string, limit int) string {
	value = strings.Join(strings.Fields(strings.TrimSpace(value)), " ")
	if limit <= 0 || len(value) <= limit {
		return value
	}
	if limit <= 3 {
		return value[:limit]
	}
	return value[:limit-3] + "..."
}

func fableTime(value any) time.Time {
	parsed := firstNonEmptyString(value)
	if parsed == "" {
		return time.Time{}
	}
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339, "2006-01-02 15:04:05", "2006-01-02"} {
		if ts, err := time.Parse(layout, parsed); err == nil {
			return ts.UTC()
		}
	}
	return time.Time{}
}
