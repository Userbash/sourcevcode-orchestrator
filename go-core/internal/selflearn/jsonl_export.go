package selflearn

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type JSONLExporter struct {
	outputDir string
}

func NewJSONLExporter(outputDir string) *JSONLExporter {
	return &JSONLExporter{outputDir: strings.TrimSpace(outputDir)}
}

func (e *JSONLExporter) ExportPreferenceDataset(ctx context.Context, examples []domain.PreferenceExample) (string, error) {
	if e == nil {
		return "", fmt.Errorf("jsonl exporter is not configured")
	}
	dir := strings.TrimSpace(e.outputDir)
	if dir == "" {
		dir = os.TempDir()
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, fmt.Sprintf("preference-dataset-%s.jsonl", time.Now().UTC().Format("20060102-150405")))
	file, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	writer := bufio.NewWriter(file)
	for _, example := range examples {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}
		record := map[string]any{
			"dataset_id": example.DatasetID,
			"prompt":     example.Prompt,
			"chosen": map[string]any{
				"thought":      example.Chosen.ThoughtProcess,
				"code":         example.Chosen.GeneratedCode,
				"final_answer": example.Chosen.FinalAnswer,
				"score":        example.Chosen.Evaluation.Score,
				"status":       example.Chosen.Evaluation.Status,
			},
			"rejected": map[string]any{
				"thought":      example.Rejected.ThoughtProcess,
				"code":         example.Rejected.GeneratedCode,
				"final_answer": example.Rejected.FinalAnswer,
				"score":        example.Rejected.Evaluation.Score,
				"status":       example.Rejected.Evaluation.Status,
				"error_log":    example.Rejected.Evaluation.ErrorLog,
			},
			"metadata": example.Metadata,
		}
		line, err := json.Marshal(record)
		if err != nil {
			return "", err
		}
		if _, err := writer.Write(append(line, '\n')); err != nil {
			return "", err
		}
	}
	if err := writer.Flush(); err != nil {
		return "", err
	}
	return path, nil
}
