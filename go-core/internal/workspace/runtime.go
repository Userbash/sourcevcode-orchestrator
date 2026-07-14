package workspace

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	defaultCommandTimeout = 30 * time.Second
	maxFileBytes          = 512 << 10
	maxCommandOutputBytes = 256 << 10
	maxListResults        = 200
)

type Runtime struct {
	root string

	filesChanged map[string]struct{}
	commandsRun  []string
	testResults  []map[string]any
}

func New(root string) (*Runtime, error) {
	if strings.TrimSpace(root) == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return nil, fmt.Errorf("resolve workspace root: %w", err)
		}
		root = cwd
	}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve workspace root: %w", err)
	}
	info, err := os.Stat(absRoot)
	if err != nil {
		return nil, fmt.Errorf("stat workspace root: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("workspace root is not a directory: %s", absRoot)
	}
	return &Runtime{
		root:         absRoot,
		filesChanged: make(map[string]struct{}),
	}, nil
}

func (r *Runtime) Root() string {
	return r.root
}

func (r *Runtime) Snapshot() (filesChanged []string, commandsRun []string, testResults []map[string]any) {
	for file := range r.filesChanged {
		filesChanged = append(filesChanged, file)
	}
	sort.Strings(filesChanged)
	commandsRun = append(commandsRun, r.commandsRun...)
	for _, row := range r.testResults {
		testResults = append(testResults, cloneMap(row))
	}
	return filesChanged, commandsRun, testResults
}

func (r *Runtime) ListFiles(path string, limit int) (map[string]any, error) {
	if limit <= 0 || limit > maxListResults {
		limit = maxListResults
	}
	baseAbs, baseRel, err := r.resolve(path, false)
	if err != nil {
		return nil, err
	}
	entries := make([]string, 0, limit)
	walkErr := filepath.WalkDir(baseAbs, func(current string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if current == baseAbs {
			return nil
		}
		rel, err := filepath.Rel(r.root, current)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		entries = append(entries, rel)
		if len(entries) >= limit {
			return fs.SkipAll
		}
		return nil
	})
	if walkErr != nil && !errors.Is(walkErr, fs.SkipAll) {
		return nil, walkErr
	}
	return map[string]any{
		"root":    baseRel,
		"entries": entries,
		"count":   len(entries),
	}, nil
}

func (r *Runtime) ReadFile(path string, startLine int, endLine int) (map[string]any, error) {
	absPath, relPath, err := r.resolve(path, false)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(absPath)
	if err != nil {
		return nil, err
	}
	if len(data) > maxFileBytes {
		return nil, fmt.Errorf("file too large: %s", relPath)
	}
	content := string(data)
	if startLine > 0 || endLine > 0 {
		lines := strings.Split(content, "\n")
		if startLine <= 0 {
			startLine = 1
		}
		if endLine <= 0 || endLine > len(lines) {
			endLine = len(lines)
		}
		if startLine > endLine || startLine > len(lines) {
			content = ""
		} else {
			content = strings.Join(lines[startLine-1:endLine], "\n")
		}
	}
	return map[string]any{
		"path":    relPath,
		"content": content,
		"bytes":   len(content),
	}, nil
}

func (r *Runtime) WriteFile(path string, content string) (map[string]any, error) {
	absPath, relPath, err := r.resolve(path, true)
	if err != nil {
		return nil, err
	}
	if len(content) > maxFileBytes {
		return nil, fmt.Errorf("write exceeds size limit for %s", relPath)
	}
	if err := os.MkdirAll(filepath.Dir(absPath), 0o755); err != nil {
		return nil, err
	}
	if err := os.WriteFile(absPath, []byte(content), 0o644); err != nil {
		return nil, err
	}
	r.filesChanged[relPath] = struct{}{}
	return map[string]any{
		"path":  relPath,
		"bytes": len(content),
	}, nil
}

func (r *Runtime) RunCommand(command []string, cwd string, timeoutSeconds int) (map[string]any, error) {
	if err := validateCommand(command); err != nil {
		return nil, err
	}
	runDir, relDir, err := r.resolveDir(cwd)
	if err != nil {
		return nil, err
	}
	timeout := defaultCommandTimeout
	if timeoutSeconds > 0 && timeoutSeconds < 300 {
		timeout = time.Duration(timeoutSeconds) * time.Second
	}
	runCtx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, command[0], command[1:]...)
	cmd.Dir = runDir
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &limitWriter{buf: &stdout, limit: maxCommandOutputBytes}
	cmd.Stderr = &limitWriter{buf: &stderr, limit: maxCommandOutputBytes}
	started := time.Now()
	err = cmd.Run()
	duration := time.Since(started)
	stdoutText := stdout.String()
	stderrText := stderr.String()
	exitCode := 0
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else if runCtx.Err() != nil {
			exitCode = -1
		} else {
			return nil, err
		}
	}

	commandText := strings.Join(command, " ")
	r.commandsRun = append(r.commandsRun, commandText)
	if testResult := buildTestResult(command, exitCode, duration, stdoutText, stderrText); testResult != nil {
		r.testResults = append(r.testResults, testResult)
	}

	result := map[string]any{
		"command":      command,
		"cwd":          relDir,
		"exit_code":    exitCode,
		"stdout":       stdoutText,
		"stderr":       stderrText,
		"duration_ms":  duration.Milliseconds(),
		"timed_out":    runCtx.Err() == context.DeadlineExceeded,
		"command_text": commandText,
	}
	if err != nil {
		result["error"] = err.Error()
	}
	return result, nil
}

func (r *Runtime) resolve(path string, allowCreate bool) (string, string, error) {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" {
		return r.root, ".", nil
	}
	candidate := trimmed
	if !filepath.IsAbs(candidate) {
		candidate = filepath.Join(r.root, candidate)
	}
	absPath, err := filepath.Abs(candidate)
	if err != nil {
		return "", "", err
	}
	rel, err := filepath.Rel(r.root, absPath)
	if err != nil {
		return "", "", err
	}
	rel = filepath.ToSlash(rel)
	if rel == ".." || strings.HasPrefix(rel, "../") {
		return "", "", fmt.Errorf("path escapes workspace root: %s", path)
	}
	if rel == "." {
		return absPath, rel, nil
	}
	if !allowCreate {
		if _, err := os.Stat(absPath); err != nil {
			return "", "", err
		}
	}
	return absPath, rel, nil
}

func (r *Runtime) resolveDir(path string) (string, string, error) {
	absPath, relPath, err := r.resolve(path, false)
	if err != nil {
		return "", "", err
	}
	info, err := os.Stat(absPath)
	if err != nil {
		return "", "", err
	}
	if !info.IsDir() {
		return "", "", fmt.Errorf("cwd is not a directory: %s", relPath)
	}
	return absPath, relPath, nil
}

func validateCommand(command []string) error {
	if len(command) == 0 || strings.TrimSpace(command[0]) == "" {
		return errors.New("command is required")
	}
	base := filepath.Base(command[0])
	switch base {
	case "git":
		return allowSubcommand(command, "status", "diff", "log", "show", "branch", "rev-parse")
	case "go":
		return allowSubcommand(command, "test", "build", "vet", "fmt", "list", "env", "version")
	case "gofmt":
		return nil
	case "bwrap", "unshare", "flatpak-spawn", "patch":
		return nil
	case "rg", "sed", "cat", "ls", "pwd", "find", "stat", "which", "env", "printenv", "curl", "node", "npx":
		return nil
	case "pytest":
		return nil
	case "python", "python3":
		if len(command) >= 3 && command[1] == "-m" && (command[2] == "pytest" || command[2] == "unittest") {
			return nil
		}
		return fmt.Errorf("python execution is restricted to test modules")
	case "npm":
		return validateNPM(command)
	default:
		return fmt.Errorf("command %q is not allowed", command[0])
	}
}

func allowSubcommand(command []string, allowed ...string) error {
	if len(command) < 2 {
		return fmt.Errorf("%s subcommand is required", filepath.Base(command[0]))
	}
	sub := command[1]
	for _, candidate := range allowed {
		if sub == candidate {
			return nil
		}
	}
	return fmt.Errorf("%s %s is not allowed", filepath.Base(command[0]), sub)
}

func validateNPM(command []string) error {
	if len(command) < 2 {
		return errors.New("npm subcommand is required")
	}
	switch command[1] {
	case "test", "build":
		return nil
	case "run":
		if len(command) < 3 {
			return errors.New("npm run script is required")
		}
		switch command[2] {
		case "test", "build", "lint", "check", "typecheck":
			return nil
		}
	}
	return fmt.Errorf("npm command is not allowed: %s", strings.Join(command, " "))
}

func buildTestResult(command []string, exitCode int, duration time.Duration, stdout string, stderr string) map[string]any {
	if len(command) == 0 {
		return nil
	}
	base := filepath.Base(command[0])
	kind := ""
	switch base {
	case "go":
		if len(command) >= 2 && command[1] == "test" {
			kind = "go test"
		}
	case "pytest":
		kind = "pytest"
	case "python", "python3":
		if len(command) >= 3 && command[1] == "-m" && command[2] == "pytest" {
			kind = "pytest"
		}
	case "npm":
		if len(command) >= 2 && command[1] == "test" {
			kind = "npm test"
		}
	}
	if kind == "" {
		return nil
	}
	return map[string]any{
		"kind":        kind,
		"command":     strings.Join(command, " "),
		"exit_code":   exitCode,
		"passed":      exitCode == 0,
		"duration_ms": duration.Milliseconds(),
		"stdout":      stdout,
		"stderr":      stderr,
	}
}

type limitWriter struct {
	buf     *bytes.Buffer
	limit   int
	written int
}

func (w *limitWriter) Write(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	remaining := w.limit - w.written
	if remaining > 0 {
		if len(p) > remaining {
			_, _ = w.buf.Write(p[:remaining])
			w.written += remaining
		} else {
			_, _ = w.buf.Write(p)
			w.written += len(p)
		}
	}
	return len(p), nil
}

func cloneMap(input map[string]any) map[string]any {
	output := make(map[string]any, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}
