package ops

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type AIKernelConfig struct {
	ProjectRoot        string
	ModelDir           string
	VenvDir            string
	ModelPath          string
	MMProjPath         string
	Host               string
	Port               string
	ModelAlias         string
	NCtx               string
	NThreads           string
	NGPULayers         string
	ChatTemplateKwargs string
	ServiceName        string
	LogDir             string
	PIDPath            string
}

func AIKernelConfigFromEnv(projectRoot string) AIKernelConfig {
	modelDir := envOrDefault("AI_KERNEL_MODEL_DIR", filepath.Join(userHome(), ".local/share/ai-kernel/models/hauhaucs-qwen36-35b-a3b-aggressive"))
	venvDir := envOrDefault("AI_KERNEL_VENV_DIR", filepath.Join(cacheHome(), "ai-kernel/venvs/llama-cpp"))
	return AIKernelConfig{
		ProjectRoot:        projectRoot,
		ModelDir:           modelDir,
		VenvDir:            venvDir,
		ModelPath:          envOrDefault("AI_KERNEL_MODEL_PATH", filepath.Join(modelDir, "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf")),
		MMProjPath:         envOrDefault("AI_KERNEL_MMPROJ_PATH", filepath.Join(modelDir, "mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf")),
		Host:               envOrDefault("AI_KERNEL_HOST", "0.0.0.0"),
		Port:               envOrDefault("AI_KERNEL_PORT", "8012"),
		ModelAlias:         envOrDefault("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"),
		NCtx:               envOrDefault("AI_KERNEL_N_CTX", "8192"),
		NThreads:           envOrDefault("AI_KERNEL_N_THREADS", "16"),
		NGPULayers:         envOrDefault("AI_KERNEL_N_GPU_LAYERS", "0"),
		ChatTemplateKwargs: envOrDefault("AI_KERNEL_CHAT_TEMPLATE_KWARGS", `{"enable_thinking": false}`),
		ServiceName:        envOrDefault("AI_KERNEL_SERVICE_NAME", "ai-kernel.service"),
		LogDir:             envOrDefault("AI_KERNEL_LOG_DIR", filepath.Join(stateHome(), "ai-kernel")),
		PIDPath:            envOrDefault("AI_KERNEL_PID_PATH", "/tmp/ai-kernel-server.pid"),
	}
}

func (c AIKernelConfig) Provision(ctx context.Context) error {
	if err := os.MkdirAll(c.ModelDir, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(c.VenvDir), 0o755); err != nil {
		return err
	}
	python := envOrDefault("PYTHON", "python3")
	if _, err := os.Stat(filepath.Join(c.VenvDir, "bin/python")); err != nil {
		ensureSystemPythonDeps(ctx)
		if result := RunCommand(ctx, python, "-m", "venv", c.VenvDir); result.ReturnCode != 0 {
			return fmt.Errorf("create ai-kernel venv: %s", result.Stderr)
		}
	}
	venvPython := filepath.Join(c.VenvDir, "bin/python")
	RunCommand(ctx, venvPython, "-m", "ensurepip", "--upgrade")
	if result := RunCommand(ctx, venvPython, "-m", "pip", "--version"); result.ReturnCode != 0 {
		if err := os.RemoveAll(c.VenvDir); err != nil {
			return err
		}
		ensureSystemPythonDeps(ctx)
		if result := RunCommand(ctx, python, "-m", "venv", c.VenvDir); result.ReturnCode != 0 {
			return fmt.Errorf("recreate ai-kernel venv: %s", result.Stderr)
		}
		RunCommand(ctx, venvPython, "-m", "ensurepip", "--upgrade")
	}
	if result := RunCommand(ctx, venvPython, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"); result.ReturnCode != 0 {
		return fmt.Errorf("upgrade ai-kernel pip tools: %s", result.Stderr)
	}
	check := RunCommand(ctx, venvPython, "-c", "import llama_cpp, llama_cpp.server")
	if check.ReturnCode != 0 {
		if result := RunCommand(ctx, venvPython, "-m", "pip", "install", "llama-cpp-python[server]"); result.ReturnCode != 0 {
			return fmt.Errorf("install llama-cpp-python[server]: %s", result.Stderr)
		}
	}
	if err := downloadIfMissing(c.ModelPath, "https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive/resolve/main/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf?download=true"); err != nil {
		return err
	}
	if err := downloadIfMissing(c.MMProjPath, "https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive/resolve/main/mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf?download=true"); err != nil {
		return err
	}
	return nil
}

func (c AIKernelConfig) Serve(ctx context.Context, stdout io.Writer, stderr io.Writer) error {
	python := filepath.Join(c.VenvDir, "bin/python")
	cmd := exec.CommandContext(ctx, python, "-m", "llama_cpp.server",
		"--host", c.Host,
		"--port", c.Port,
		"--model", c.ModelPath,
		"--model_alias", c.ModelAlias,
		"--clip_model_path", c.MMProjPath,
		"--chat_format", "chat_template.default",
		"--chat_template_kwargs", c.ChatTemplateKwargs,
		"--n_ctx", c.NCtx,
		"--n_threads", c.NThreads,
		"--n_gpu_layers", c.NGPULayers,
	)
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	cmd.Env = aiKernelServerEnv(os.Environ())
	return cmd.Run()
}

func aiKernelServerEnv(baseEnv []string) []string {
	envMap := make(map[string]string, len(baseEnv))
	for _, entry := range baseEnv {
		key, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		envMap[key] = value
	}

	// Force llama.cpp server auth to use the dedicated AI kernel key and avoid
	// leaking unrelated ambient API_KEY values from the shell or user service.
	delete(envMap, "API_KEY")
	if value := strings.TrimSpace(envMap["AI_KERNEL_API_KEY"]); value != "" {
		envMap["API_KEY"] = value
	}

	normalizedEnv := make([]string, 0, len(envMap))
	for _, entry := range baseEnv {
		key, _, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		value, exists := envMap[key]
		if !exists {
			continue
		}
		normalizedEnv = append(normalizedEnv, key+"="+value)
		delete(envMap, key)
	}
	for key, value := range envMap {
		normalizedEnv = append(normalizedEnv, key+"="+value)
	}
	return normalizedEnv
}

func (c AIKernelConfig) InstallUserService(executablePath string) error {
	unitDir := filepath.Join(configHome(), "systemd/user")
	unitPath := filepath.Join(unitDir, c.ServiceName)
	if err := os.MkdirAll(unitDir, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(c.LogDir, 0o755); err != nil {
		return err
	}
	unit := fmt.Sprintf(`[Unit]
Description=SourceVCode AI Kernel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%s
EnvironmentFile=-%s
EnvironmentFile=-%s
EnvironmentFile=-%s
Environment=AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE=false
Environment=AI_KERNEL_HOST=0.0.0.0
Environment=AI_KERNEL_PORT=%s
Environment=AI_KERNEL_LOG_PATH=%s
Environment=AI_KERNEL_PID_PATH=%s
ExecStart=%s ai-kernel-serve --project-root %s
Restart=on-failure
RestartSec=5
TimeoutStartSec=%s
StandardOutput=append:%s
StandardError=append:%s

[Install]
WantedBy=default.target
`, c.ProjectRoot, filepath.Join(c.ProjectRoot, ".env"), filepath.Join(c.ProjectRoot, ".env.bridge"), filepath.Join(c.ProjectRoot, ".env.gemini.local"), c.Port, filepath.Join(c.LogDir, "server.log"), c.PIDPath, executablePath, c.ProjectRoot, envOrDefault("AI_KERNEL_STARTUP_TIMEOUT_SEC", "300"), filepath.Join(c.LogDir, "service.out.log"), filepath.Join(c.LogDir, "service.err.log"))
	if err := os.WriteFile(unitPath, []byte(unit), 0o644); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, args := range [][]string{{"--user", "daemon-reload"}, {"--user", "enable", "--now", c.ServiceName}} {
		result := RunCommand(ctx, "systemctl", args...)
		if result.ReturnCode != 0 {
			return fmt.Errorf("systemctl %s: %s", strings.Join(args, " "), result.Stderr)
		}
	}
	return nil
}

func WaitForAIKernel(ctx context.Context, port string, attempts int, delay time.Duration) error {
	url := fmt.Sprintf("http://127.0.0.1:%s/v1/models", port)
	for i := 0; i < attempts; i++ {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		resp, err := http.DefaultClient.Do(req)
		if err == nil && resp != nil {
			resp.Body.Close()
			if resp.StatusCode < 500 {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
	return fmt.Errorf("ai-kernel not reachable on %s", url)
}

func downloadIfMissing(targetPath string, rawURL string) error {
	if _, err := os.Stat(targetPath); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(targetPath), 0o755); err != nil {
		return err
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return err
	}
	resp, err := http.Get(parsed.String())
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("download %s: %s", parsed.String(), resp.Status)
	}
	file, err := os.Create(targetPath)
	if err != nil {
		return err
	}
	defer file.Close()
	_, err = io.Copy(file, resp.Body)
	return err
}

func userHome() string {
	value, _ := os.UserHomeDir()
	return value
}

func cacheHome() string {
	if value := strings.TrimSpace(os.Getenv("XDG_CACHE_HOME")); value != "" {
		return value
	}
	return filepath.Join(userHome(), ".cache")
}

func configHome() string {
	if value := strings.TrimSpace(os.Getenv("XDG_CONFIG_HOME")); value != "" {
		return value
	}
	return filepath.Join(userHome(), ".config")
}

func stateHome() string {
	if value := strings.TrimSpace(os.Getenv("XDG_STATE_HOME")); value != "" {
		return value
	}
	return filepath.Join(userHome(), ".local/state")
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func ensureSystemPythonDeps(ctx context.Context) {
	if _, err := exec.LookPath("apt-get"); err != nil {
		return
	}
	_ = RunCommand(ctx, "apt-get", "update")
	_ = RunCommand(ctx, "apt-get", "install", "-y", "python3-pip", "python3-venv", "build-essential", "cmake", "pkg-config", "git", "wget")
}
