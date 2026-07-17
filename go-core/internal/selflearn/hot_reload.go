package selflearn

import (
	"context"
	"os/exec"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ExecHotReloader struct {
	config    ExecConfig
	discovery domain.ModelDiscovery
}

func NewExecHotReloader(config ExecConfig, discovery domain.ModelDiscovery) *ExecHotReloader {
	return &ExecHotReloader{config: config, discovery: discovery}
}

func (r *ExecHotReloader) ReloadModel(ctx context.Context, request domain.HotReloadRequest) error {
	command := expandReloadTemplate(r.config.CommandTemplate, request)
	if len(command) > 0 {
		cmd := exec.CommandContext(ctx, command[0], command[1:]...)
		cmd.Dir = r.config.WorkDir
		cmd.Env = append(cmd.Environ(), r.config.Env...)
		if output, err := cmd.CombinedOutput(); err != nil {
			return execError(err, strings.TrimSpace(string(output)))
		}
	}
	if r.discovery != nil {
		_, err := r.discovery.Refresh(ctx)
		return err
	}
	return nil
}

func expandReloadTemplate(template []string, request domain.HotReloadRequest) []string {
	if len(template) == 0 {
		return nil
	}
	replacer := strings.NewReplacer(
		"{provider}", request.Provider,
		"{model}", request.ModelName,
		"{path}", request.ModelPath,
		"{manifest}", request.ManifestPath,
		"{timestamp}", time.Now().UTC().Format(time.RFC3339),
	)
	out := make([]string, 0, len(template))
	for _, part := range template {
		out = append(out, replacer.Replace(part))
	}
	return out
}

func execError(err error, output string) error {
	if output == "" {
		return err
	}
	return &commandError{cause: err, output: output}
}

type commandError struct {
	cause  error
	output string
}

func (e *commandError) Error() string {
	return e.cause.Error() + ": " + e.output
}
