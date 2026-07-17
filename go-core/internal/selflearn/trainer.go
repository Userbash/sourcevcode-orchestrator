package selflearn

import (
	"context"
	"os/exec"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ExecConfig struct {
	CommandTemplate []string
	WorkDir         string
	Env             []string
}

type ExecTrainer struct {
	config ExecConfig
}

func NewExecTrainer(config ExecConfig) *ExecTrainer {
	return &ExecTrainer{config: config}
}

func (t *ExecTrainer) StartTraining(ctx context.Context, job domain.FineTuneJob) (domain.FineTuneJob, error) {
	now := time.Now().UTC()
	job.StartedAt = &now
	job.Status = domain.FineTuneJobStatusRunning
	job.Command = expandTemplate(t.config.CommandTemplate, job)

	if len(job.Command) == 0 {
		job.Status = domain.FineTuneJobStatusFailed
		job.ErrorLog = "trainer command template is empty"
		completed := time.Now().UTC()
		job.CompletedAt = &completed
		return job, nil
	}

	cmd := exec.CommandContext(ctx, job.Command[0], job.Command[1:]...)
	cmd.Dir = t.config.WorkDir
	cmd.Env = append(cmd.Environ(), t.config.Env...)
	output, err := cmd.CombinedOutput()
	completed := time.Now().UTC()
	job.CompletedAt = &completed
	if err != nil {
		job.Status = domain.FineTuneJobStatusFailed
		job.ErrorLog = strings.TrimSpace(string(output))
		if job.ErrorLog == "" {
			job.ErrorLog = err.Error()
		}
		return job, nil
	}
	job.Status = domain.FineTuneJobStatusSucceeded
	if job.Metadata == nil {
		job.Metadata = map[string]any{}
	}
	job.Metadata["trainer_output"] = strings.TrimSpace(string(output))
	return job, nil
}

func expandTemplate(template []string, job domain.FineTuneJob) []string {
	if len(template) == 0 {
		return nil
	}
	replacer := strings.NewReplacer(
		"{dataset}", job.DatasetPath,
		"{model}", firstNonEmpty(job.ModelName, job.BaseModel),
		"{base_model}", job.BaseModel,
		"{adapter}", job.AdapterPath,
		"{merged}", job.MergedPath,
	)
	out := make([]string, 0, len(template))
	for _, part := range template {
		out = append(out, replacer.Replace(part))
	}
	return out
}
