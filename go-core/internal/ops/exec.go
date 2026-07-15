package ops

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type CommandResult struct {
	Command    []string `json:"command"`
	ReturnCode int      `json:"returncode"`
	Stdout     string   `json:"stdout"`
	Stderr     string   `json:"stderr"`
}

func RunCommand(ctx context.Context, name string, args ...string) CommandResult {
	cmdArgs := append([]string{name}, args...)
	cmd := exec.CommandContext(ctx, commandName(), commandArgs(name, args...)...)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	result := CommandResult{Command: cmdArgs, ReturnCode: 0, Stdout: strings.TrimSpace(stdout.String()), Stderr: strings.TrimSpace(stderr.String())}
	if err == nil {
		return result
	}
	var exitErr *exec.ExitError
	if ok := errorAs(err, &exitErr); ok {
		result.ReturnCode = exitErr.ExitCode()
		return result
	}
	result.ReturnCode = 127
	if result.Stderr == "" {
		result.Stderr = err.Error()
		if strings.Contains(err.Error(), "executable file not found") {
			result.ReturnCode = 127
		}
	}
	return result
}

func errorAs(err error, target any) bool {
	switch typed := target.(type) {
	case **exec.ExitError:
		exitErr, ok := err.(*exec.ExitError)
		if ok {
			*typed = exitErr
			return true
		}
	}
	return false
}

func commandName() string {
	if _, err := exec.LookPath("flatpak-spawn"); err == nil {
		return "flatpak-spawn"
	}
	return "__direct__"
}

func commandArgs(name string, args ...string) []string {
	if commandName() == "flatpak-spawn" {
		return append([]string{"--host", name}, args...)
	}
	return append([]string{name}, args...)
}

func RequireCommand(name string) error {
	lookup := name
	if commandName() == "flatpak-spawn" {
		lookup = "flatpak-spawn"
	}
	if _, err := exec.LookPath(lookup); err != nil {
		return fmt.Errorf("required command %s not found", name)
	}
	return nil
}
