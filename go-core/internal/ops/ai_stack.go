package ops

import (
	"context"
	"fmt"
	"time"
)

// EnsureAIStack starts infra, local_llm, and ai_kernel for the active go-core runtime.
func EnsureAIStack(ctx context.Context, projectRoot string, composeFile string, attempts int, delay time.Duration) error {
	cfg, err := bootstrapConfigFromEnv(BootstrapOptions{
		ProjectRoot: projectRoot,
		ComposeFile: composeFile,
		Model:       envOrDefault("AI_BRIDGE_LOCAL_LLM_MODEL", ""),
	})
	if err != nil {
		return err
	}
	if err := EnsureCoreInfra(ctx, cfg.ProjectRoot, cfg.ComposeFile, attempts, delay); err != nil {
		return err
	}
	if composeErr := ComposeUp(ctx, cfg.ProjectRoot, cfg.ComposeFile, "local_llm", "local_llm_init", "ai_kernel"); composeErr == nil {
		if err := waitForRuntimeModelEndpoints(ctx, cfg, attempts, delay); err == nil {
			return nil
		}
	}
	if err := RequireCommand("curl"); err != nil {
		return err
	}
	if err := RequireCommand("podman"); err != nil {
		return err
	}
	if err := startLocalLLM(ctx, cfg); err != nil {
		return fmt.Errorf("start local_llm: %w", err)
	}
	if err := startAIKernel(ctx, cfg); err != nil {
		return fmt.Errorf("start ai-kernel: %w", err)
	}
	return waitForRuntimeModelEndpoints(ctx, cfg, attempts, delay)
}

func waitForRuntimeModelEndpoints(ctx context.Context, cfg BootstrapConfig, attempts int, delay time.Duration) error {
	if err := waitForHTTP(ctx, "http://127.0.0.1:"+cfg.OllamaPort+"/api/tags", attempts, delay); err != nil {
		return err
	}
	return WaitForAIKernel(ctx, cfg.AIKernelPort, attempts, delay)
}
