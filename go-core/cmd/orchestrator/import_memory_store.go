package main

import (
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

const defaultLegacyMemoryStore = "/var/home/sanya/sourcevcode-orchestrator/memory_store"

type legacyImportOptions struct {
	Source          string
	StatePath       string
	Project         string
	RepoPath        string
	RepoID          string
	RepoFingerprint string
	Branch          string
	Scope           string
	DryRun          bool
}

type legacyImportSummary struct {
	RAGMemories       int
	RAGDocuments      int
	RouteMemories     int
	VFSCheckpoints    int
	GlobalVectorLoads int
	SkippedTraining   int
	SkippedFiles      int
	Failures          []string
	ProbeVectorHits   int
	ProbeRouteHits    int
}

type legacyMemoryFile struct {
	MemoryID        any    `json:"memory_id"`
	SessionID       string `json:"session_id"`
	SourceSessionID string `json:"source_session_id"`
	AgentID         string `json:"agent_id"`
	Type            string `json:"type"`
	Content         any    `json:"content"`
	CreatedAt       string `json:"created_at"`
}

type legacyCommandFile struct {
	SessionID       string `json:"session_id"`
	SourceSessionID string `json:"source_session_id"`
	AgentID         string `json:"agent_id"`
	Command         string `json:"command"`
	Result          any    `json:"result"`
	Success         bool   `json:"success"`
	TokensUsed      any    `json:"tokens_used"`
	ExecutedAt      string `json:"executed_at"`
}

type legacyArtifactFile struct {
	ArtifactID string `json:"artifact_id"`
	Kind       string `json:"kind"`
	Title      string `json:"title"`
	Summary    string `json:"summary"`
	Content    any    `json:"content"`
	CreatedAt  string `json:"created_at"`
	UpdatedAt  string `json:"updated_at"`
}

type legacyCheckpointFile struct {
	Checksum    string         `json:"checksum"`
	Content     map[string]any `json:"content"`
	Integrity   string         `json:"integrity"`
	LastUpdated string         `json:"last_updated"`
	Metadata    map[string]any `json:"metadata"`
	OwnerAgent  string         `json:"owner_agent"`
	Path        string         `json:"path"`
}

func importMemoryStore(cfg app.Config, args []string) error {
	flags := flag.NewFlagSet("import-memory-store", flag.ExitOnError)
	source := flags.String("source", defaultLegacyMemoryStore, "path to legacy memory_store directory")
	statePath := flags.String("state-path", cfg.StatePath, "deprecated; ignored because import writes to the database store")
	project := flags.String("project", "legacy-memory-store", "project scope assigned to imported route memories")
	repoPath := flags.String("repo-path", "", "repo path assigned to imported route memories")
	repoID := flags.String("repo-id", "legacy-memory-store", "repo id assigned to imported RAG records")
	repoFingerprint := flags.String("repo-fingerprint", "legacy-memory-store", "repo fingerprint assigned to imported route memories")
	branch := flags.String("branch", "legacy-import", "branch assigned to imported records when missing")
	scope := flags.String("scope", "project", "default RAG scope for imported legacy memories")
	dryRun := flags.Bool("dry-run", false, "parse and classify files without writing to the store")
	_ = flags.Parse(args)

	options := legacyImportOptions{
		Source:          strings.TrimSpace(*source),
		StatePath:       strings.TrimSpace(*statePath),
		Project:         strings.TrimSpace(*project),
		RepoPath:        strings.TrimSpace(*repoPath),
		RepoID:          strings.TrimSpace(*repoID),
		RepoFingerprint: strings.TrimSpace(*repoFingerprint),
		Branch:          normalizeBranchValue(*branch),
		Scope:           strings.TrimSpace(*scope),
		DryRun:          *dryRun,
	}
	if options.Source == "" {
		return fmt.Errorf("source path is required")
	}
	if options.Scope == "" {
		options.Scope = "project"
	}

	ctx := context.Background()
	var (
		store   state.Store
		manager *memory.Manager
		err     error
	)
	if !options.DryRun {
		store, err = state.OpenStore(options.StatePath)
		if err != nil {
			return err
		}
		manager = memory.NewManager(store)
	}

	summary := &legacyImportSummary{}
	if err := importLegacyMemories(ctx, manager, options, summary); err != nil {
		return err
	}
	if err := importLegacyCommands(ctx, manager, store, options, summary); err != nil {
		return err
	}
	if err := importLegacyArtifacts(ctx, manager, options, summary); err != nil {
		return err
	}
	if err := importLegacyCheckpoints(ctx, store, options, summary); err != nil {
		return err
	}
	if err := inspectLegacyTraining(options, summary); err != nil {
		return err
	}
	if !options.DryRun {
		if err := verifyLegacyImport(ctx, manager, store, options, summary); err != nil {
			return err
		}
	}
	reportLegacyImport(store, options, summary)
	return nil
}

func importLegacyMemories(ctx context.Context, manager *memory.Manager, options legacyImportOptions, summary *legacyImportSummary) error {
	files, err := filepath.Glob(filepath.Join(options.Source, "memories", "*.json"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	for _, path := range files {
		var payload legacyMemoryFile
		if err := readJSONFile(path, &payload); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("memory %s: %v", path, err))
			continue
		}
		if options.DryRun {
			summary.RAGMemories++
			continue
		}
		content := asStringMap(payload.Content)
		record := domain.RAGMemoryRecord{
			MemoryID:   legacyStableID("memory", path, payload.MemoryID, payload.SessionID, payload.Type),
			MemoryType: firstNonEmpty(strings.TrimSpace(payload.Type), "legacy"),
			Scope:      inferLegacyMemoryScope(payload.Type, options.Scope),
			OwnerID:    firstNonEmpty(strings.TrimSpace(payload.SourceSessionID), strings.TrimSpace(payload.SessionID), strings.TrimSpace(payload.AgentID), options.RepoID),
			Content:    content,
			Summary:    summarizeLegacyPayload(payload.Type, payload.Content),
			Metadata: map[string]any{
				"legacy_path":       path,
				"legacy_type":       strings.TrimSpace(payload.Type),
				"legacy_session_id": strings.TrimSpace(payload.SessionID),
				"source_session_id": strings.TrimSpace(payload.SourceSessionID),
				"agent_id":          strings.TrimSpace(payload.AgentID),
				"project":           options.Project,
				"repo_path":         options.RepoPath,
			},
			Confidence: inferLegacyConfidence(payload.Type, content),
			Importance: inferLegacyImportance(payload.Type),
			RepoID:     options.RepoID,
			Branch:     options.Branch,
			CreatedAt:  parseLegacyTime(payload.CreatedAt),
		}
		if err := manager.Remember(ctx, record); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("memory %s: %v", path, err))
			continue
		}
		if err := ingestLegacyGlobalText(ctx, manager, options, record.MemoryID, record.Summary, record.Content, record.Metadata); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("memory vector %s: %v", path, err))
			continue
		}
		summary.RAGMemories++
		summary.GlobalVectorLoads++
	}
	return nil
}

func importLegacyCommands(ctx context.Context, manager *memory.Manager, store state.Store, options legacyImportOptions, summary *legacyImportSummary) error {
	files, err := filepath.Glob(filepath.Join(options.Source, "commands", "*.json"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	for _, path := range files {
		var payload legacyCommandFile
		if err := readJSONFile(path, &payload); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("command %s: %v", path, err))
			continue
		}
		if options.DryRun {
			summary.RouteMemories++
			continue
		}
		taskType := legacyTaskType(payload.Command, payload.Result)
		capability := legacyCapability(taskType, payload.Result)
		description := summarizeLegacyPayload(payload.Command, payload.Result)
		task := domain.Task{
			ID:                 legacyStableID("task", path, payload.SessionID, payload.Command),
			SessionID:          firstNonEmpty(strings.TrimSpace(payload.SourceSessionID), strings.TrimSpace(payload.SessionID)),
			Type:               taskType,
			RequiredCapability: capability,
			Complexity:         legacyComplexity(payload.Result),
			Input: domain.TaskInput{
				Description: description,
				Files:       legacyFiles(payload.Result),
				Constraints: legacyConstraints(payload.Result),
			},
			Context: domain.TaskContext{
				Project:  options.Project,
				RepoPath: options.RepoPath,
				Branch:   options.Branch,
			},
			RepoFingerprint: options.RepoFingerprint,
			CreatedAt:       parseLegacyTime(payload.ExecutedAt),
		}
		signature := manager.BuildTaskSignature(ctx, task)
		resultMap := asStringMap(payload.Result)
		record := domain.RouteMemoryRecord{
			RouteID:         legacyStableID("route", path, payload.SessionID, payload.Command),
			SessionID:       task.SessionID,
			TaskID:          task.ID,
			RootTaskID:      task.ID,
			TaskSignature:   signature,
			TaskType:        task.Type,
			Capability:      capability,
			Complexity:      task.Complexity,
			Project:         options.Project,
			RepoPath:        options.RepoPath,
			RepoFingerprint: options.RepoFingerprint,
			Branch:          options.Branch,
			AgentID:         firstNonEmpty(strings.TrimSpace(payload.AgentID), "legacy-agent"),
			Provider:        firstNonEmpty(stringValue(resultMap, "provider"), stringValue(resultMap, "assigned_provider"), "legacy"),
			ModelName:       firstNonEmpty(stringValue(resultMap, "model_name"), stringValue(resultMap, "assigned_model"), stringValue(resultMap, "model"), "legacy"),
			PlanMode:        "legacy-import",
			Success:         payload.Success,
			Confidence:      legacyCommandConfidence(payload.Success, resultMap),
			LatencyMS:       int64(numberValue(resultMap, "latency_ms")),
			ReviewPassed:    legacyReviewPassed(taskType, payload.Success, resultMap),
			TestsPassed:     legacyTestsPassed(taskType, payload.Success, resultMap),
			CostEstimate:    float64(numberValue(resultMap, "cost_estimate")),
			ErrorCount:      legacyErrorCount(resultMap, payload.Success),
			Summary:         truncateText(description, 512),
			Embedding:       append([]float64(nil), signature.Embedding...),
			Metadata: map[string]any{
				"legacy_path":       path,
				"legacy_command":    strings.TrimSpace(payload.Command),
				"legacy_session_id": strings.TrimSpace(payload.SessionID),
				"source_session_id": strings.TrimSpace(payload.SourceSessionID),
				"tokens_used":       payload.TokensUsed,
				"result":            resultMap,
			},
			CreatedAt: parseLegacyTime(payload.ExecutedAt),
			UpdatedAt: parseLegacyTime(payload.ExecutedAt),
		}
		if err := store.UpsertRouteMemories(ctx, []domain.RouteMemoryRecord{record}); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("command %s: %v", path, err))
			continue
		}
		summary.RouteMemories++
	}
	return nil
}

func importLegacyArtifacts(ctx context.Context, manager *memory.Manager, options legacyImportOptions, summary *legacyImportSummary) error {
	files, err := filepath.Glob(filepath.Join(options.Source, "vfs", "artifacts", "*.json"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	for _, path := range files {
		var payload legacyArtifactFile
		if err := readJSONFile(path, &payload); err != nil {
			text, readErr := os.ReadFile(path)
			if readErr != nil {
				summary.Failures = append(summary.Failures, fmt.Sprintf("artifact %s: %v", path, err))
				continue
			}
			payload = legacyArtifactFile{
				ArtifactID: legacyStableID("artifact-raw", path),
				Kind:       "legacy_artifact_text",
				Title:      filepath.Base(path),
				Summary:    truncateText(strings.TrimSpace(string(text)), 512),
				Content:    strings.TrimSpace(string(text)),
				CreatedAt:  time.Now().UTC().Format(time.RFC3339),
				UpdatedAt:  time.Now().UTC().Format(time.RFC3339),
			}
		}
		if options.DryRun {
			summary.RAGDocuments++
			continue
		}
		document := domain.RAGDocument{
			DocumentID:     legacyStableID("artifact", path, payload.ArtifactID, payload.Title),
			Scope:          "artifact",
			OwnerType:      "project",
			OwnerID:        options.RepoID,
			SourceType:     firstNonEmpty(strings.TrimSpace(payload.Kind), "legacy_artifact"),
			SourceRef:      path,
			Title:          firstNonEmpty(strings.TrimSpace(payload.Title), filepath.Base(path)),
			ContentText:    stringifyAny(payload.Content),
			ContentSummary: firstNonEmpty(strings.TrimSpace(payload.Summary), summarizeLegacyPayload(payload.Kind, payload.Content)),
			Metadata: map[string]any{
				"legacy_path": path,
				"project":     options.Project,
				"repo_path":   options.RepoPath,
			},
			Importance: inferLegacyImportance(payload.Kind),
			RepoID:     options.RepoID,
			Branch:     options.Branch,
			CreatedAt:  parseLegacyTime(payload.CreatedAt),
			UpdatedAt:  parseLegacyTime(firstNonEmpty(payload.UpdatedAt, payload.CreatedAt)),
		}
		if err := manager.IngestDocument(ctx, document); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("artifact %s: %v", path, err))
			continue
		}
		if err := ingestLegacyGlobalText(ctx, manager, options, document.DocumentID, document.ContentSummary, map[string]any{"content_text": document.ContentText}, document.Metadata); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("artifact vector %s: %v", path, err))
			continue
		}
		summary.RAGDocuments++
		summary.GlobalVectorLoads++
	}
	return nil
}

func importLegacyCheckpoints(ctx context.Context, store state.Store, options legacyImportOptions, summary *legacyImportSummary) error {
	files, err := filepath.Glob(filepath.Join(options.Source, "vfs", "*.json"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	for _, path := range files {
		if filepath.Base(filepath.Dir(path)) == "artifacts" {
			continue
		}
		var payload legacyCheckpointFile
		if err := readJSONFile(path, &payload); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("checkpoint %s: %v", path, err))
			continue
		}
		if strings.TrimSpace(payload.Path) == "" {
			summary.SkippedFiles++
			continue
		}
		if options.DryRun {
			summary.VFSCheckpoints++
			continue
		}
		checkpoint := domain.VFSCheckpointRecord{
			Path:       strings.TrimSpace(payload.Path),
			TaskID:     extractTaskIDFromPath(payload.Path),
			AgentID:    strings.TrimSpace(payload.OwnerAgent),
			Checkpoint: payload.Content,
			Checksum:   strings.TrimSpace(payload.Checksum),
			Integrity:  firstNonEmpty(strings.TrimSpace(payload.Integrity), "unknown"),
			Metadata:   cloneMap(payload.Metadata),
			StorageURI: path,
			CreatedAt:  parseLegacyTime(payload.LastUpdated),
			UpdatedAt:  parseLegacyTime(payload.LastUpdated),
		}
		if err := store.UpsertVFSCheckpoints(ctx, []domain.VFSCheckpointRecord{checkpoint}); err != nil {
			summary.Failures = append(summary.Failures, fmt.Sprintf("checkpoint %s: %v", path, err))
			continue
		}
		summary.VFSCheckpoints++
	}
	return nil
}

func inspectLegacyTraining(options legacyImportOptions, summary *legacyImportSummary) error {
	files, err := filepath.Glob(filepath.Join(options.Source, "training", "*.json"))
	if err != nil {
		return err
	}
	summary.SkippedTraining += len(files)
	return nil
}

func verifyLegacyImport(ctx context.Context, manager *memory.Manager, store state.Store, options legacyImportOptions, summary *legacyImportSummary) error {
	probeTask := domain.Task{
		ID:                 "legacy-import-probe",
		SessionID:          "legacy-import-probe",
		Type:               domain.TaskTypeResearch,
		RequiredCapability: "research",
		Input: domain.TaskInput{
			Description: "validation gate routing outcome lessons learned decomposition result summary",
		},
		Context: domain.TaskContext{
			Project:  options.Project,
			RepoPath: options.RepoPath,
			Branch:   options.Branch,
		},
		RepoFingerprint: options.RepoFingerprint,
	}
	results, err := manager.SearchVectorContext(ctx, probeTask, 5)
	if err != nil {
		return fmt.Errorf("vector probe failed: %w", err)
	}
	summary.ProbeVectorHits = len(results)
	routeResults, err := manager.SearchRouteMemories(ctx, domain.Task{
		ID:                 "legacy-route-probe",
		Type:               domain.TaskTypePlan,
		RequiredCapability: "plan",
		Complexity:         domain.ComplexityMedium,
		Input: domain.TaskInput{
			Description: "plan decomposition and routing outcome",
		},
		Context:         domain.TaskContext{Project: options.Project, RepoPath: options.RepoPath, Branch: options.Branch},
		RepoFingerprint: options.RepoFingerprint,
	}, 5)
	if err != nil {
		return fmt.Errorf("route probe failed: %w", err)
	}
	summary.ProbeRouteHits = len(routeResults)
	return nil
}

func reportLegacyImport(store state.Store, options legacyImportOptions, summary *legacyImportSummary) {
	fmt.Printf("legacy import source=%s dry_run=%t\n", options.Source, options.DryRun)
	fmt.Printf("rag_memories=%d rag_documents=%d route_memories=%d vfs_checkpoints=%d global_vector_loads=%d skipped_training=%d skipped_files=%d failures=%d\n",
		summary.RAGMemories,
		summary.RAGDocuments,
		summary.RouteMemories,
		summary.VFSCheckpoints,
		summary.GlobalVectorLoads,
		summary.SkippedTraining,
		summary.SkippedFiles,
		len(summary.Failures),
	)
	if !options.DryRun {
		fmt.Printf("probe_vector_hits=%d probe_route_hits=%d\n", summary.ProbeVectorHits, summary.ProbeRouteHits)
		if store != nil {
			snapshot := store.Snapshot()
			fmt.Printf("store_snapshot=%s\n", stringifyAny(snapshot))
		}
	}
	for _, failure := range summary.Failures {
		fmt.Printf("failure=%s\n", failure)
	}
}

func ingestLegacyGlobalText(ctx context.Context, manager *memory.Manager, options legacyImportOptions, sourceID string, summary string, content map[string]any, metadata map[string]any) error {
	if manager == nil {
		return nil
	}
	searchable := strings.TrimSpace(strings.Join([]string{summary, stringifyAny(content)}, "\n\n"))
	if searchable == "" {
		return nil
	}
	meta := cloneMap(metadata)
	meta["project"] = options.Project
	meta["repo_path"] = options.RepoPath
	meta["repo_id"] = options.RepoID
	meta["branch"] = options.Branch
	_, err := manager.IngestText(ctx, "", "", "legacy_import", sourceID, searchable, meta)
	return err
}

func inferLegacyMemoryScope(memoryType string, fallback string) string {
	memoryType = strings.TrimSpace(memoryType)
	switch {
	case strings.HasPrefix(memoryType, "ctx:"):
		return "session"
	case strings.HasPrefix(memoryType, "kpi_"):
		return "kpi"
	case memoryType == "plan" || memoryType == "code" || memoryType == "research" || memoryType == "test":
		return "task"
	default:
		return firstNonEmpty(strings.TrimSpace(fallback), "project")
	}
}

func inferLegacyImportance(memoryType string) float64 {
	switch {
	case strings.HasPrefix(memoryType, "ctx:validation_gate"):
		return 0.95
	case strings.HasPrefix(memoryType, "ctx:routing_outcome"), strings.HasPrefix(memoryType, "ctx:lessons_learned"):
		return 0.85
	case strings.HasPrefix(memoryType, "kpi_task:"):
		return 0.80
	default:
		return 0.65
	}
}

func inferLegacyConfidence(memoryType string, content map[string]any) float64 {
	if value := float64(numberValue(content, "confidence")); value > 0 {
		return clampUnit(value)
	}
	if strings.HasPrefix(memoryType, "ctx:validation_gate") {
		return 0.90
	}
	if strings.HasPrefix(memoryType, "ctx:routing_outcome") {
		return 0.80
	}
	return 0.70
}

func legacyTaskType(command string, payload any) domain.TaskType {
	command = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(command), "task:"))
	switch command {
	case string(domain.TaskTypePlan):
		return domain.TaskTypePlan
	case string(domain.TaskTypeCode):
		return domain.TaskTypeCode
	case string(domain.TaskTypeReview):
		return domain.TaskTypeReview
	case string(domain.TaskTypeTest):
		return domain.TaskTypeTest
	case string(domain.TaskTypeDocs):
		return domain.TaskTypeDocs
	case string(domain.TaskTypeFix):
		return domain.TaskTypeFix
	case string(domain.TaskTypeResearch):
		return domain.TaskTypeResearch
	}
	text := strings.ToLower(strings.Join([]string{command, stringifyAny(payload)}, " "))
	switch {
	case strings.Contains(text, "review"):
		return domain.TaskTypeReview
	case strings.Contains(text, "test"):
		return domain.TaskTypeTest
	case strings.Contains(text, "fix"):
		return domain.TaskTypeFix
	case strings.Contains(text, "doc"):
		return domain.TaskTypeDocs
	case strings.Contains(text, "research"):
		return domain.TaskTypeResearch
	case strings.Contains(text, "code"):
		return domain.TaskTypeCode
	default:
		return domain.TaskTypePlan
	}
}

func legacyCapability(taskType domain.TaskType, payload any) string {
	values := asStringMap(payload)
	return firstNonEmpty(
		stringValue(values, "capability"),
		stringValue(values, "required_capability"),
		string(taskType),
	)
}

func legacyComplexity(payload any) domain.Complexity {
	values := asStringMap(payload)
	switch strings.ToLower(firstNonEmpty(stringValue(values, "complexity"), stringValue(values, "severity"))) {
	case string(domain.ComplexityLow):
		return domain.ComplexityLow
	case string(domain.ComplexityHigh):
		return domain.ComplexityHigh
	case string(domain.ComplexityCritical):
		return domain.ComplexityCritical
	default:
		return domain.ComplexityMedium
	}
}

func legacyFiles(payload any) []string {
	values := asStringMap(payload)
	return stringSliceValue(values, "files")
}

func legacyConstraints(payload any) []string {
	values := asStringMap(payload)
	constraints := stringSliceValue(values, "constraints")
	if len(constraints) > 0 {
		return constraints
	}
	return stringSliceValue(values, "acceptance_criteria")
}

func legacyCommandConfidence(success bool, payload map[string]any) float64 {
	if value := float64(numberValue(payload, "confidence")); value > 0 {
		return clampUnit(value)
	}
	if success {
		return 0.80
	}
	return 0.35
}

func legacyReviewPassed(taskType domain.TaskType, success bool, payload map[string]any) bool {
	if taskType == domain.TaskTypeReview {
		return success
	}
	return boolValue(payload, "review_passed")
}

func legacyTestsPassed(taskType domain.TaskType, success bool, payload map[string]any) bool {
	if taskType == domain.TaskTypeTest {
		return success
	}
	return boolValue(payload, "tests_passed")
}

func legacyErrorCount(payload map[string]any, success bool) int {
	if count := numberValue(payload, "error_count"); count > 0 {
		return count
	}
	if errorsValue := stringSliceValue(payload, "errors"); len(errorsValue) > 0 {
		return len(errorsValue)
	}
	if success {
		return 0
	}
	return 1
}

func extractTaskIDFromPath(path string) string {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	for i := 0; i < len(parts)-1; i++ {
		if parts[i] == "active_tasks" && i+1 < len(parts) {
			return parts[i+1]
		}
	}
	return ""
}

func summarizeLegacyPayload(kind string, payload any) string {
	values := asStringMap(payload)
	parts := []string{
		stringValue(values, "summary"),
		stringValue(values, "title"),
		stringValue(values, "objective"),
		stringValue(values, "reason"),
		stringValue(values, "status"),
	}
	for _, part := range parts {
		if strings.TrimSpace(part) != "" {
			return truncateText(strings.TrimSpace(part), 512)
		}
	}
	text := strings.TrimSpace(strings.Join([]string{strings.TrimSpace(kind), stringifyAny(payload)}, ": "))
	return truncateText(text, 512)
}

func readJSONFile(path string, target any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, target)
}

func normalizeBranchValue(branch string) string {
	branch = strings.TrimSpace(branch)
	if branch == "" {
		return "legacy-import"
	}
	return branch
}

func parseLegacyTime(value string) time.Time {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Now().UTC()
	}
	formats := []string{time.RFC3339Nano, time.RFC3339, "2006-01-02 15:04:05Z07:00"}
	for _, format := range formats {
		if parsed, err := time.Parse(format, value); err == nil {
			return parsed.UTC()
		}
	}
	return time.Now().UTC()
}

func legacyStableID(kind string, parts ...any) string {
	h := sha1.New()
	_, _ = h.Write([]byte(kind))
	for _, part := range parts {
		_, _ = h.Write([]byte("|"))
		_, _ = h.Write([]byte(strings.TrimSpace(fmt.Sprint(part))))
	}
	return kind + "-" + hex.EncodeToString(h.Sum(nil))[:16]
}

func asStringMap(value any) map[string]any {
	switch typed := value.(type) {
	case map[string]any:
		return cloneMap(typed)
	case nil:
		return map[string]any{}
	default:
		return map[string]any{"value": typed}
	}
}

func cloneMap(value map[string]any) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	cloned := make(map[string]any, len(value))
	for key, item := range value {
		cloned[key] = cloneAny(item)
	}
	return cloned
}

func cloneAny(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		return cloneMap(typed)
	case []any:
		out := make([]any, len(typed))
		for i, item := range typed {
			out[i] = cloneAny(item)
		}
		return out
	case []string:
		return append([]string(nil), typed...)
	default:
		return typed
	}
}

func stringifyAny(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return strings.TrimSpace(text)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return strings.TrimSpace(fmt.Sprint(value))
	}
	return string(encoded)
}

func stringValue(values map[string]any, key string) string {
	if values == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(values[key]))
}

func stringSliceValue(values map[string]any, key string) []string {
	raw, ok := values[key]
	if !ok || raw == nil {
		return nil
	}
	items, ok := raw.([]any)
	if !ok {
		if itemsString, ok := raw.([]string); ok {
			return append([]string(nil), itemsString...)
		}
		return nil
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text := strings.TrimSpace(fmt.Sprint(item))
		if text != "" {
			result = append(result, text)
		}
	}
	return result
}

func numberValue(values map[string]any, key string) int {
	raw, ok := values[key]
	if !ok || raw == nil {
		return 0
	}
	switch typed := raw.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case json.Number:
		parsed, _ := typed.Int64()
		return int(parsed)
	default:
		return 0
	}
}

func boolValue(values map[string]any, key string) bool {
	raw, ok := values[key]
	if !ok || raw == nil {
		return false
	}
	value, ok := raw.(bool)
	return ok && value
}

func clampUnit(value float64) float64 {
	if value > 1 {
		return 1
	}
	if value < 0 {
		return 0
	}
	return value
}

func truncateText(value string, limit int) string {
	value = strings.TrimSpace(value)
	if limit <= 0 || len(value) <= limit {
		return value
	}
	if limit <= 3 {
		return value[:limit]
	}
	return value[:limit-3] + "..."
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}
