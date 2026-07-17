package selflearn

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type EvalConfig struct {
	GoBinary string
	Timeout  time.Duration
}

type GoCodeEvaluator struct {
	config EvalConfig
}

func NewGoCodeEvaluator(config EvalConfig) *GoCodeEvaluator {
	if strings.TrimSpace(config.GoBinary) == "" {
		config.GoBinary = "go"
	}
	if config.Timeout <= 0 {
		config.Timeout = 30 * time.Second
	}
	return &GoCodeEvaluator{config: config}
}

func (e *GoCodeEvaluator) Evaluate(ctx context.Context, trace domain.TraceRecord) (domain.CodeExecutionResult, error) {
	startedAt := time.Now().UTC()
	code := strings.TrimSpace(trace.GeneratedCode)
	if code == "" {
		return domain.CodeExecutionResult{
			Status:      domain.TraceRecordStatusFail,
			Compiler:    e.config.GoBinary,
			Command:     []string{e.config.GoBinary, "build", "./..."},
			Score:       0,
			ErrorLog:    "generated code is empty",
			DurationMS:  time.Since(startedAt).Milliseconds(),
			CompletedAt: time.Now().UTC(),
		}, nil
	}

	workDir, err := os.MkdirTemp("", "selflearn-eval-*")
	if err != nil {
		return domain.CodeExecutionResult{}, err
	}
	defer os.RemoveAll(workDir)

	if err := os.WriteFile(filepath.Join(workDir, "go.mod"), []byte("module selflearn/eval\n\ngo 1.22\n"), 0o644); err != nil {
		return domain.CodeExecutionResult{}, err
	}
	if err := os.WriteFile(filepath.Join(workDir, "generated.go"), []byte(code), 0o644); err != nil {
		return domain.CodeExecutionResult{}, err
	}

	buildResult := e.runCommand(ctx, workDir, []string{e.config.GoBinary, "build", "./..."}, startedAt)
	if buildResult.Status == domain.TraceRecordStatusSuccess {
		return buildResult, nil
	}
	testResult := e.runCommand(ctx, workDir, []string{e.config.GoBinary, "test", "./..."}, startedAt)
	if testResult.Status == domain.TraceRecordStatusSuccess {
		return testResult, nil
	}
	if strings.TrimSpace(testResult.ErrorLog) == "" {
		testResult.ErrorLog = buildResult.ErrorLog
	}
	return testResult, nil
}

func (e *GoCodeEvaluator) runCommand(parent context.Context, workDir string, command []string, startedAt time.Time) domain.CodeExecutionResult {
	timeoutCtx, cancel := context.WithTimeout(parent, e.config.Timeout)
	defer cancel()

	cmd := exec.CommandContext(timeoutCtx, command[0], command[1:]...)
	cmd.Dir = workDir
	output, err := cmd.CombinedOutput()
	completedAt := time.Now().UTC()
	result := domain.CodeExecutionResult{
		Compiler:    e.config.GoBinary,
		Command:     append([]string(nil), command...),
		CompletedAt: completedAt,
		DurationMS:  completedAt.Sub(startedAt).Milliseconds(),
		Stdout:      string(output),
	}
	if err == nil {
		result.Status = domain.TraceRecordStatusSuccess
		result.Score = 1
		return result
	}
	result.Status = domain.TraceRecordStatusFail
	result.Score = 0
	result.Stderr = string(output)
	result.ErrorLog = strings.TrimSpace(string(output))
	if timeoutCtx.Err() == context.DeadlineExceeded {
		result.ErrorLog = fmt.Sprintf("evaluation timeout after %s", e.config.Timeout)
	}
	return result
}
