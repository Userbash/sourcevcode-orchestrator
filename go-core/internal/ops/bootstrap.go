package ops

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/app"
)

type BootstrapOptions struct {
	ProjectRoot  string
	ComposeFile  string
	Model        string
	SkipLocalLLM bool
	SkipAIKernel bool
	NoBuild      bool
	AgyLogin     bool
}

type BootstrapConfig struct {
	ProjectRoot               string
	ComposeFile               string
	ExecutablePath            string
	OrchestratorImage         string
	OrchestratorContainer     string
	OllamaContainer           string
	MemoryVolume              string
	OllamaVolume              string
	LocalModel                string
	OrchestratorPort          string
	OrchestratorContainerPort string
	OllamaPort                string
	AIKernelPort              string
	HealthPath                string
	ReadyAttempts             int
	ReadySleep                time.Duration
}

func BootstrapStack(ctx context.Context, opts BootstrapOptions) error {
	cfg, err := bootstrapConfigFromEnv(opts)
	if err != nil {
		return err
	}
	if err := EnsureEnvFiles(cfg.ProjectRoot); err != nil {
		return err
	}
	if _, err := LoadEnvFiles(cfg.ProjectRoot); err != nil {
		return err
	}
	if err := RequireCommand("curl"); err != nil {
		return err
	}
	if err := ensureCoreInfra(ctx, cfg); err != nil {
		return err
	}
	if err := RequireCommand("podman"); err != nil {
		return err
	}
	if err := ensureVolume(ctx, cfg.MemoryVolume); err != nil {
		return err
	}
	if !opts.SkipLocalLLM {
		if err := startLocalLLM(ctx, cfg); err != nil {
			return err
		}
	}
	if !opts.SkipAIKernel {
		if err := startAIKernel(ctx, cfg); err != nil {
			return err
		}
	}
	if !opts.NoBuild {
		if err := buildOrchestratorImage(ctx, cfg); err != nil {
			return err
		}
	}
	if err := startOrchestrator(ctx, cfg); err != nil {
		return err
	}
	if err := verifyAgy(ctx, cfg, opts); err != nil {
		return err
	}
	printBootstrapSummary(cfg, opts)
	return nil
}

func bootstrapConfigFromEnv(opts BootstrapOptions) (BootstrapConfig, error) {
	projectRoot := opts.ProjectRoot
	if projectRoot == "" {
		cwd, _ := os.Getwd()
		projectRoot = cwd
	}
	composeFile, err := ResolveComposeFilePath(projectRoot, opts.ComposeFile)
	if err != nil {
		return BootstrapConfig{}, err
	}
	exe, _ := os.Executable()
	cfg := BootstrapConfig{
		ProjectRoot:               projectRoot,
		ComposeFile:               composeFile,
		ExecutablePath:            exe,
		OrchestratorImage:         envOrDefault("ORCHESTRATOR_IMAGE", "localhost/hebrew-orchestrator:latest"),
		OrchestratorContainer:     envOrDefault("ORCHESTRATOR_CONTAINER", "hebrew_ai_orchestrator"),
		OllamaContainer:           envOrDefault("OLLAMA_CONTAINER", "ai_bridge_local_llm"),
		MemoryVolume:              envOrDefault("AI_BRIDGE_MEMORY_VOLUME_NAME", "hebrew_core_memory"),
		OllamaVolume:              envOrDefault("AI_BRIDGE_OLLAMA_VOLUME_NAME", "hebrew_ollama_data"),
		LocalModel:                firstNonEmpty(opts.Model, os.Getenv("AI_BRIDGE_LOCAL_LLM_MODEL")),
		OrchestratorPort:          envOrDefault("ORCHESTRATOR_PORT", "8010"),
		OrchestratorContainerPort: envOrDefault("ORCHESTRATOR_CONTAINER_PORT", "8010"),
		OllamaPort:                envOrDefault("AI_BRIDGE_LOCAL_LLM_PORT", "11434"),
		AIKernelPort:              envOrDefault("AI_KERNEL_PORT", "8012"),
		HealthPath:                envOrDefault("ORCHESTRATOR_HEALTH_PATH", "/health"),
		ReadyAttempts:             120,
		ReadySleep:                2 * time.Second,
	}
	if value := strings.TrimSpace(os.Getenv("ORCHESTRATOR_READY_ATTEMPTS")); value != "" {
		fmt.Sscanf(value, "%d", &cfg.ReadyAttempts)
	}
	if value := strings.TrimSpace(os.Getenv("ORCHESTRATOR_READY_SLEEP_SEC")); value != "" {
		var seconds int
		fmt.Sscanf(value, "%d", &seconds)
		if seconds > 0 {
			cfg.ReadySleep = time.Duration(seconds) * time.Second
		}
	}
	return cfg, nil
}

func ensureCoreInfra(ctx context.Context, cfg BootstrapConfig) error {
	return EnsureCoreInfra(ctx, cfg.ProjectRoot, cfg.ComposeFile, cfg.ReadyAttempts, cfg.ReadySleep)
}

func startLocalLLM(ctx context.Context, cfg BootstrapConfig) error {
	backend := detectLocalLLMGPUBackend()
	if !exists(ctx, cfg.OllamaContainer) {
		if err := recreateOllamaContainer(ctx, cfg, backend); err != nil {
			return err
		}
	} else if backend == "nvidia" && !ollamaContainerHasNvidiaGPU(ctx, cfg.OllamaContainer) {
		if err := recreateOllamaContainer(ctx, cfg, backend); err != nil {
			return err
		}
	} else {
		_ = runMust(ctx, "podman", "start", cfg.OllamaContainer)
	}
	if err := waitForHTTP(ctx, "http://127.0.0.1:"+cfg.OllamaPort+"/api/tags", 45, time.Second); err != nil {
		return err
	}
	if strings.TrimSpace(cfg.LocalModel) == "" {
		return nil
	}
	result := RunCommand(ctx, "podman", "exec", cfg.OllamaContainer, "ollama", "pull", cfg.LocalModel)
	if result.ReturnCode != 0 && !strings.Contains(result.Stderr, "already exists") {
		return fmt.Errorf("ollama pull %s: %s", cfg.LocalModel, result.Stderr)
	}
	return nil
}

func startAIKernel(ctx context.Context, cfg BootstrapConfig) error {
	ai := AIKernelConfigFromEnv(cfg.ProjectRoot)
	if err := WaitForAIKernel(ctx, ai.Port, 3, time.Second); err == nil {
		return nil
	}
	if err := ai.Provision(ctx); err != nil {
		return err
	}
	if err := ai.InstallUserService(cfg.ExecutablePath); err != nil {
		return err
	}
	return WaitForAIKernel(ctx, ai.Port, 90, 2*time.Second)
}

func buildOrchestratorImage(ctx context.Context, cfg BootstrapConfig) error {
	return runMust(ctx, "podman", "build", "-f", filepath.Join(cfg.ProjectRoot, "go-core", "Dockerfile"), "-t", cfg.OrchestratorImage, filepath.Join(cfg.ProjectRoot, "go-core"))
}

func startOrchestrator(ctx context.Context, cfg BootstrapConfig) error {
	if exists(ctx, cfg.OrchestratorContainer) {
		_ = runMust(ctx, "podman", "rm", "-f", "-v", cfg.OrchestratorContainer)
	}
	postgresURL := containerPostgresURL("host.containers.internal")
	rabbitURL := containerRabbitMQURL("host.containers.internal")
	args := []string{"run", "-d", "--name", cfg.OrchestratorContainer, "-w", "/app", "-p", cfg.OrchestratorPort + ":" + cfg.OrchestratorContainerPort,
		"--env-file", filepath.Join(cfg.ProjectRoot, ".env"),
		"--env-file", filepath.Join(cfg.ProjectRoot, ".env.gemini.local"),
		"--env-file", filepath.Join(cfg.ProjectRoot, ".env.bridge"),
		"-e", "PYTHONPATH=/app",
		"-e", "ORCHESTRATOR_PORT=" + cfg.OrchestratorContainerPort,
		"-e", "PATH=" + firstNonEmpty(os.Getenv("PATH"), "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
		"-e", "NODE_PATH=" + filepath.Join(userHome(), ".npm-packages/lib/node_modules"),
		"-e", "TESTING=false",
		"-e", "AI_BRIDGE_AUTOSTART_LOCAL_LLM=false",
		"-e", "AI_BRIDGE_AUTOSTART_EASY_DIFFUSION=false",
		"-e", "AI_BRIDGE_LOCAL_LLM_AUTO_PROVISION=false",
		"-e", "AI_BRIDGE_LOCAL_LLM_ENDPOINT=http://host.containers.internal:" + cfg.OllamaPort,
		"-e", "AI_BRIDGE_LOCAL_LLM_PORT=" + cfg.OllamaPort,
		"-e", "AI_BRIDGE_LOCAL_LLM_MODEL=" + cfg.LocalModel,
		"-e", "AI_KERNEL_ENABLED=" + firstNonEmpty(os.Getenv("AI_KERNEL_ENABLED"), "true"),
		"-e", "AI_KERNEL_BASE_URL=" + firstNonEmpty(os.Getenv("AI_KERNEL_BASE_URL"), "http://host.containers.internal:"+cfg.AIKernelPort+"/v1"),
		"-e", "AI_KERNEL_REQUIRE_API_KEY=" + firstNonEmpty(os.Getenv("AI_KERNEL_REQUIRE_API_KEY"), "false"),
		"-e", "AI_KERNEL_MODEL_ALIAS=" + firstNonEmpty(os.Getenv("AI_KERNEL_MODEL_ALIAS"), "gemma4-12b-agentic-fable5:q4_k_m"),
		"-e", "AI_KERNEL_TCP_PROBE_HOSTS=" + firstNonEmpty(os.Getenv("AI_KERNEL_TCP_PROBE_HOSTS"), "host.containers.internal:"+cfg.AIKernelPort),
		"-e", "AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE=" + firstNonEmpty(os.Getenv("AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE"), "false"),
		"-e", "AI_BRIDGE_HOST_WORKSPACE_ROOT=" + cfg.ProjectRoot,
		"-e", "AI_KERNEL_HOST_HOME=" + userHome(),
		"-e", "AI_BRIDGE_WORKSPACE_ROOT=/workspace",
		"-e", "AI_BRIDGE_EASY_DIFFUSION_START_ENABLED=false",
		"-e", "AI_BRIDGE_MEMORY_ENABLED=true",
		"-e", "AI_BRIDGE_MEMORY_DATABASE_URL=" + postgresURL,
		"-e", "AI_BRIDGE_RABBITMQ_URL=" + rabbitURL,
		"-e", "AI_BRIDGE_MESSAGE_BUS_BACKEND=rabbitmq",
		"-e", "GO_CORE_DB_BACKUP_DIR=/app/db_backups",
		"-e", "AI_BRIDGE_LIVE_MODEL_PROBE=false",
		"-e", "AI_BRIDGE_REQUIRE_EXTERNAL_SCANNERS=false",
		"-e", "AI_BRIDGE_DISABLE_SOURCECRAFT=" + firstNonEmpty(os.Getenv("AI_BRIDGE_DISABLE_SOURCECRAFT"), "false"),
		"-e", "AI_BRIDGE_ENABLE_VOICE=false",
		"-e", "AI_BRIDGE_AUTO_APPROVE=true",
		"-e", "AI_BRIDGE_CONFIRMATION_POLICY=full_auto",
		"-e", "OPENAI_SESSION_TOKEN_BUDGET=120000",
		"-v", cfg.MemoryVolume + ":/app/db_backups",
		"-v", cfg.ProjectRoot + ":/workspace:z"}
	npmPackages := filepath.Join(userHome(), ".npm-packages")
	if _, err := os.Stat(npmPackages); err == nil {
		args = append(args, "-v", npmPackages+":"+npmPackages+":ro,z")
	}
	toolingDir := filepath.Join(cfg.ProjectRoot, ".tooling")
	if _, err := os.Stat(toolingDir); err == nil {
		args = append(args, "-v", toolingDir+":/app/.tooling:ro,z")
	}
	args = append(args, cfg.OrchestratorImage)
	if err := runMust(ctx, "podman", args...); err != nil {
		return err
	}
	return waitForHTTP(ctx, "http://127.0.0.1:"+cfg.OrchestratorPort+cfg.HealthPath, cfg.ReadyAttempts, cfg.ReadySleep)
}

func containerPostgresURL(host string) string {
	info := app.ResolvePostgresConnectionInfo()
	if info.User == "" {
		return ""
	}
	return fmt.Sprintf("postgresql://%s:%s@%s:%s/%s?sslmode=disable", info.User, info.Password, host, info.Port, info.Database)
}

func containerRabbitMQURL(host string) string {
	info := app.ResolveRabbitMQConnectionInfo()
	return fmt.Sprintf("amqp://%s:%s@%s:%s/", info.User, info.Password, host, info.Port)
}

func detectLocalLLMGPUBackend() string {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("AI_BRIDGE_LOCAL_LLM_GPU_BACKEND"))) {
	case "cpu":
		return "cpu"
	case "nvidia":
		return "nvidia"
	}
	if _, err := exec.LookPath("nvidia-smi"); err == nil {
		return "nvidia"
	}
	return "cpu"
}

func ollamaContainerHasNvidiaGPU(ctx context.Context, container string) bool {
	result := RunCommand(ctx, "podman", "inspect", container, "--format", "{{json .HostConfig.Devices}} {{json .HostConfig.SecurityOpt}} {{json .HostConfig.GroupAdd}}")
	if result.ReturnCode != 0 {
		return false
	}
	inspect := result.Stdout
	return strings.Contains(inspect, "nvidia.com/gpu=all") && strings.Contains(inspect, "label=disable") && strings.Contains(inspect, "keep-groups")
}

func recreateOllamaContainer(ctx context.Context, cfg BootstrapConfig, backend string) error {
	if exists(ctx, cfg.OllamaContainer) {
		_ = runMust(ctx, "podman", "rm", "-f", "-v", cfg.OllamaContainer)
	}
	if err := ensureVolume(ctx, cfg.OllamaVolume); err != nil {
		return err
	}
	args := []string{"run", "-d", "--name", cfg.OllamaContainer, "--restart", "unless-stopped", "-p", cfg.OllamaPort + ":11434"}
	if backend == "nvidia" {
		args = append(args, "--security-opt=label=disable", "--group-add", "keep-groups", "--device", "nvidia.com/gpu=all", "-e", "NVIDIA_VISIBLE_DEVICES=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility")
	}
	args = append(args, "-v", cfg.OllamaVolume+":/root/.ollama", "docker.io/ollama/ollama")
	return runMust(ctx, "podman", args...)
}

func verifyAgy(ctx context.Context, cfg BootstrapConfig, opts BootstrapOptions) error {
	agy, err := exec.LookPath("agy")
	if err != nil {
		agy, err = exec.LookPath("antigravity")
		if err != nil {
			return nil
		}
	}
	loginScript := filepath.Join(cfg.ProjectRoot, "core", "scripts", "antigravity_login.py")
	if _, err := os.Stat(loginScript); err != nil {
		return nil
	}
	if opts.AgyLogin {
		result := RunCommand(ctx, "python3", loginScript, "--login", "--timeout", "300")
		if result.ReturnCode != 0 {
			return fmt.Errorf("antigravity login via %s: %s", agy, result.Stderr)
		}
		return nil
	}
	_ = RunCommand(ctx, "python3", loginScript, "--verify")
	return nil
}

func ensureVolume(ctx context.Context, volume string) error {
	result := RunCommand(ctx, "podman", "volume", "exists", volume)
	if result.ReturnCode == 0 {
		return nil
	}
	return runMust(ctx, "podman", "volume", "create", volume)
}

func exists(ctx context.Context, container string) bool {
	result := RunCommand(ctx, "podman", "container", "exists", container)
	return result.ReturnCode == 0
}

func waitForHTTP(ctx context.Context, endpoint string, attempts int, delay time.Duration) error {
	for i := 0; i < attempts; i++ {
		result := RunCommand(ctx, "curl", "--max-time", "5", "-fsS", endpoint)
		if result.ReturnCode == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
	return fmt.Errorf("endpoint not ready: %s", endpoint)
}

func runMust(ctx context.Context, name string, args ...string) error {
	result := RunCommand(ctx, name, args...)
	if result.ReturnCode != 0 {
		return fmt.Errorf("%s failed: %s", strings.Join(append([]string{name}, args...), " "), firstNonEmpty(result.Stderr, result.Stdout))
	}
	return nil
}

func printBootstrapSummary(cfg BootstrapConfig, opts BootstrapOptions) {
	postgres := app.ResolvePostgresConnectionInfo()
	rabbit := app.ResolveRabbitMQConnectionInfo()
	fmt.Println("[bootstrap] AI stack is ready.")
	fmt.Printf("  Compose file:  %s\n", cfg.ComposeFile)
	fmt.Printf("  Orchestrator:  http://127.0.0.1:%s/health\n", cfg.OrchestratorPort)
	fmt.Printf("  PostgreSQL:    host=%s port=%s db=%s user=%s password=%s\n", postgres.Host, postgres.Port, postgres.Database, postgres.User, redactEmpty(postgres.Password))
	fmt.Printf("  PostgreSQL DSN:%s\n", firstNonEmpty(redactCredentialURL(postgres.URL), "[not configured]"))
	fmt.Printf("  RabbitMQ AMQP: %s\n", redactCredentialURL(rabbit.AMQPURL))
	fmt.Printf("  RabbitMQ UI:   %s (user=%s password=%s)\n", rabbit.ManagementURL, rabbit.User, redactEmpty(rabbit.Password))
	fmt.Printf("  RAG backend:   postgres tables go_workflows, go_session_states, go_vector_chunks\n")
	if !opts.SkipLocalLLM {
		fmt.Printf("  Local LLM:     http://127.0.0.1:%s (model: %s)\n", cfg.OllamaPort, cfg.LocalModel)
	}
}

func redactEmpty(value string) string {
	if strings.TrimSpace(value) == "" {
		return "[empty]"
	}
	return "[redacted]"
}

func redactCredentialURL(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}

	parsed, err := url.Parse(trimmed)
	if err != nil {
		return "[redacted]"
	}
	if parsed.User == nil {
		return trimmed
	}

	parsed.User = url.User("[redacted]")
	return parsed.String()
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func binaryExists(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}
