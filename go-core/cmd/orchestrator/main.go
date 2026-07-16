package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"sourcevcode-orchestrator/go-core/internal/api"
	"sourcevcode-orchestrator/go-core/internal/app"
	"sourcevcode-orchestrator/go-core/internal/buildinfo"
	"sourcevcode-orchestrator/go-core/internal/kernel"
	"sourcevcode-orchestrator/go-core/internal/ops"
)

func main() {
	cfg := app.LoadConfig()
	if len(os.Args) < 2 {
		serve(cfg, nil)
		return
	}

	switch os.Args[1] {
	case "serve":
		serve(cfg, os.Args[2:])
	case "version":
		fmt.Println(buildinfo.String())
	case "state":
		dumpState(cfg, os.Args[2:])
	case "healthcheck":
		if err := healthcheck(cfg); err != nil {
			log.Fatalf("healthcheck: %v", err)
		}
	case "bootstrap":
		if err := bootstrap(os.Args[2:]); err != nil {
			log.Fatalf("bootstrap: %v", err)
		}
	case "runtime-preflight":
		if err := runtimePreflight(); err != nil {
			log.Fatalf("runtime-preflight: %v", err)
		}
	case "runtime-agent":
		if err := runtimeAgent(os.Args[2:]); err != nil {
			log.Fatalf("runtime-agent: %v", err)
		}
	case "runtime-agent-auto":
		if err := runtimeAgentAuto(os.Args[2:]); err != nil {
			log.Fatalf("runtime-agent-auto: %v", err)
		}
	case "runtime-agent-docker-privileged":
		if err := runtimeAgentAlias(os.Args[2:], "docker", "privileged"); err != nil {
			log.Fatalf("runtime-agent-docker-privileged: %v", err)
		}
	case "runtime-agent-docker-unconfined":
		if err := runtimeAgentAlias(os.Args[2:], "docker", "unconfined"); err != nil {
			log.Fatalf("runtime-agent-docker-unconfined: %v", err)
		}
	case "runtime-agent-podman-privileged":
		if err := runtimeAgentAlias(os.Args[2:], "podman", "privileged"); err != nil {
			log.Fatalf("runtime-agent-podman-privileged: %v", err)
		}
	case "runtime-agent-podman-unconfined":
		if err := runtimeAgentAlias(os.Args[2:], "podman", "unconfined"); err != nil {
			log.Fatalf("runtime-agent-podman-unconfined: %v", err)
		}
	case "inspect-db":
		if err := inspectDB(); err != nil {
			log.Fatalf("inspect-db: %v", err)
		}
	case "db-backup":
		if err := backupDB(); err != nil {
			log.Fatalf("db-backup: %v", err)
		}
	case "db-restore":
		if err := restoreDB(); err != nil {
			log.Fatalf("db-restore: %v", err)
		}
	case "import-memory-store":
		if err := importMemoryStore(cfg, os.Args[2:]); err != nil {
			log.Fatalf("import-memory-store: %v", err)
		}
	case "ai-kernel-provision":
		if err := aiKernelProvision(os.Args[2:]); err != nil {
			log.Fatalf("ai-kernel-provision: %v", err)
		}
	case "ai-kernel-serve":
		if err := aiKernelServe(os.Args[2:]); err != nil {
			log.Fatalf("ai-kernel-serve: %v", err)
		}
	case "ai-kernel-install-service":
		if err := aiKernelInstallService(os.Args[2:]); err != nil {
			log.Fatalf("ai-kernel-install-service: %v", err)
		}
	default:
		usage()
		os.Exit(1)
	}
}

func serve(cfg app.Config, args []string) {
	flags := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := flags.String("addr", cfg.Addr, "listen address")
	statePath := flags.String("state-path", cfg.StatePath, "deprecated; ignored because CORE requires database storage")
	ensureInfra := flags.Bool("ensure-infra", false, "start PostgreSQL and RabbitMQ from docker-compose.ai.yml before serving")
	ensureAIStack := flags.Bool("ensure-ai-stack", false, "start PostgreSQL, RabbitMQ, local_llm, and ai_kernel before serving")
	projectRoot := flags.String("project-root", "", "repository root for env files and docker-compose.ai.yml")
	composeFile := flags.String("compose-file", envOrDefault("GO_CORE_COMPOSE_FILE", "docker-compose.ai.yml"), "compose file used for infra startup")
	_ = flags.Parse(args)

	if *ensureInfra || *ensureAIStack || envBool("GO_CORE_ENSURE_INFRA") || envBool("GO_CORE_ENSURE_AI_STACK") {
		root := strings.TrimSpace(*projectRoot)
		if root == "" {
			cwd, _ := os.Getwd()
			root = cwd
		}
		if err := ops.EnsureEnvFiles(root); err != nil {
			log.Fatalf("ensure env files: %v", err)
		}
		if _, err := ops.LoadEnvFiles(root); err != nil {
			log.Fatalf("load env files: %v", err)
		}
		loadedCfg := app.LoadConfig()
		if *addr == cfg.Addr {
			*addr = loadedCfg.Addr
		}
		if *statePath == cfg.StatePath {
			*statePath = loadedCfg.StatePath
		}
		if *ensureAIStack || envBool("GO_CORE_ENSURE_AI_STACK") {
			if err := ops.EnsureAIStack(context.Background(), root, *composeFile, 120, 2*time.Second); err != nil {
				log.Fatalf("ensure ai stack: %v", err)
			}
		} else if err := ops.EnsureCoreInfra(context.Background(), root, *composeFile, 120, 2*time.Second); err != nil {
			log.Fatalf("ensure infra: %v", err)
		}
	}

	orchestrator, err := kernel.NewDefault(*statePath)
	if err != nil {
		log.Fatalf("bootstrap orchestrator: %v", err)
	}
	protector := ops.DBProtectorFromEnv()
	if err := protector.EnsureProtected(context.Background()); err != nil {
		log.Fatalf("protect database: %v", err)
	}

	apiServer := api.NewServer(orchestrator, cfg.RequiredHTTPEndpoints)
	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           apiServer.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	signalCtx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	protector.Start(signalCtx)

	serverErrors := make(chan error, 1)
	go func() {
		log.Printf("go-core orchestrator %s listening on %s", buildinfo.String(), *addr)
		serverErrors <- httpServer.ListenAndServe()
	}()

	select {
	case err := <-serverErrors:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("serve: %v", err)
		}
	case <-signalCtx.Done():
		log.Printf("go-core orchestrator shutting down")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			_ = httpServer.Close()
			log.Printf("forced HTTP shutdown: %v", err)
		}
		if err := <-serverErrors; err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("HTTP server stopped with error: %v", err)
		}
	}
}

func dumpState(cfg app.Config, args []string) {
	flags := flag.NewFlagSet("state", flag.ExitOnError)
	statePath := flags.String("state-path", cfg.StatePath, "deprecated; ignored because CORE requires database storage")
	_ = flags.Parse(args)

	orchestrator, err := kernel.NewDefault(*statePath)
	if err != nil {
		log.Fatalf("bootstrap orchestrator: %v", err)
	}

	workflows, err := orchestrator.Workflows(context.Background())
	if err != nil {
		log.Fatalf("list workflows: %v", err)
	}

	fmt.Printf("workflows=%d agents=%d modules=%d\n", len(workflows), len(orchestrator.Agents()), len(orchestrator.Modules()))
}

func healthcheck(cfg app.Config) error {
	_, port, err := net.SplitHostPort(cfg.Addr)
	if err != nil {
		return fmt.Errorf("invalid listen address %q: %w", cfg.Addr, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+port+"/health", nil)
	if err != nil {
		return err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected HTTP status: %s", response.Status)
	}
	return nil
}

func bootstrap(args []string) error {
	flags := flag.NewFlagSet("bootstrap", flag.ExitOnError)
	projectRoot := flags.String("project-root", "", "repository root")
	composeFile := flags.String("compose-file", envOrDefault("GO_CORE_COMPOSE_FILE", "docker-compose.ai.yml"), "compose file used for PostgreSQL and RabbitMQ startup")
	model := flags.String("model", os.Getenv("AI_BRIDGE_LOCAL_LLM_MODEL"), "ollama model to pull")
	skipLocalLLM := flags.Bool("skip-local-llm", false, "skip local ollama startup")
	skipAIKernel := flags.Bool("skip-ai-kernel", false, "skip ai-kernel provisioning/startup")
	noBuild := flags.Bool("no-build", false, "reuse existing orchestrator image")
	agyLogin := flags.Bool("agy-login", false, "run antigravity login helper during bootstrap")
	_ = flags.Parse(args)
	return ops.BootstrapStack(context.Background(), ops.BootstrapOptions{ProjectRoot: *projectRoot, ComposeFile: *composeFile, Model: *model, SkipLocalLLM: *skipLocalLLM, SkipAIKernel: *skipAIKernel, NoBuild: *noBuild, AgyLogin: *agyLogin})
}

func runtimePreflight() error {
	report, err := ops.RunPreflight(context.Background())
	if err != nil {
		return err
	}
	if err := ops.PrintPreflightJSON(report); err != nil {
		return err
	}
	if report.Classification != "READY" {
		return fmt.Errorf("runtime classification: %s", report.Classification)
	}
	return nil
}

func runtimeAgent(args []string) error {
	flags := flag.NewFlagSet("runtime-agent", flag.ExitOnError)
	engine := flags.String("engine", "podman", "runtime engine: docker or podman")
	mode := flags.String("mode", "unconfined", "security mode: privileged or unconfined")
	workspace := flags.String("workspace", "", "workspace path mounted to /workspace")
	_ = flags.Parse(args)
	return runRuntimeAgent(*engine, *mode, *workspace, flags.Args())
}

func runtimeAgentAlias(args []string, engine string, mode string) error {
	flags := flag.NewFlagSet("runtime-agent-alias", flag.ExitOnError)
	workspace := flags.String("workspace", "", "workspace path mounted to /workspace")
	_ = flags.Parse(args)
	return runRuntimeAgent(engine, mode, *workspace, flags.Args())
}

func runtimeAgentAuto(args []string) error {
	flags := flag.NewFlagSet("runtime-agent-auto", flag.ExitOnError)
	workspace := flags.String("workspace", "", "workspace path mounted to /workspace")
	_ = flags.Parse(args)
	remaining := flags.Args()
	if len(remaining) == 0 {
		return fmt.Errorf("usage: orchestrator runtime-agent-auto [flags] IMAGE [command args...]")
	}
	if _, err := ops.RunPreflight(context.Background()); err != nil {
		return err
	}
	engine, err := detectContainerRuntime()
	if err != nil {
		return err
	}
	return runRuntimeAgent(engine, "unconfined", *workspace, remaining)
}

func runRuntimeAgent(engine string, mode string, workspace string, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: orchestrator runtime-agent [flags] IMAGE [command args...]")
	}
	options := ops.RuntimeAgentOptions{
		Engine:      engine,
		Security:    mode,
		Workspace:   workspace,
		Image:       args[0],
		CommandArgs: args[1:],
	}
	return ops.RunRuntimeAgent(context.Background(), options)
}

func inspectDB() error {
	return ops.DBInspector{}.Inspect(context.Background())
}

func backupDB() error {
	return ops.DBProtectorFromEnv().CreateSnapshot(context.Background())
}

func restoreDB() error {
	return ops.DBProtectorFromEnv().RestoreLatest(context.Background())
}

func aiKernelProvision(args []string) error {
	flags := flag.NewFlagSet("ai-kernel-provision", flag.ExitOnError)
	projectRoot := flags.String("project-root", "", "repository root")
	port := flags.String("port", "", "AI kernel port override")
	modelAlias := flags.String("model-alias", "", "served model alias")
	weightsURL := flags.String("weights-url", "", "GGUF weights URL")
	weightsPath := flags.String("weights-path", "", "local GGUF path")
	_ = flags.Parse(args)
	cfg := ops.AIKernelConfigFromEnv(*projectRoot)
	if *port != "" {
		cfg.Port = *port
	}
	if *modelAlias != "" {
		cfg.ModelAlias = *modelAlias
	}
	if *weightsURL != "" {
		return fmt.Errorf("weights-url override is not supported by current ai-kernel provisioner; place the model locally and use --weights-path or AI_KERNEL_MODEL_PATH")
	}
	if *weightsPath != "" {
		cfg.ModelPath = *weightsPath
	}
	return cfg.Provision(context.Background())
}

func aiKernelServe(args []string) error {
	flags := flag.NewFlagSet("ai-kernel-serve", flag.ExitOnError)
	projectRoot := flags.String("project-root", "", "repository root")
	port := flags.String("port", "", "AI kernel port override")
	_ = flags.Parse(args)
	cfg := ops.AIKernelConfigFromEnv(*projectRoot)
	if *port != "" {
		cfg.Port = *port
	}
	return cfg.Serve(context.Background(), os.Stdout, os.Stderr)
}

func aiKernelInstallService(args []string) error {
	flags := flag.NewFlagSet("ai-kernel-install-service", flag.ExitOnError)
	projectRoot := flags.String("project-root", "", "repository root")
	executable := flags.String("orchestrator-path", "", "path to orchestrator binary for service ExecStart")
	_ = flags.Parse(args)
	cfg := ops.AIKernelConfigFromEnv(*projectRoot)
	binary := strings.TrimSpace(*executable)
	if binary == "" {
		current, err := os.Executable()
		if err != nil {
			return err
		}
		binary = current
	}
	return cfg.InstallUserService(binary)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: orchestrator [serve|state|healthcheck|bootstrap|runtime-preflight|runtime-agent|runtime-agent-auto|runtime-agent-docker-privileged|runtime-agent-docker-unconfined|runtime-agent-podman-privileged|runtime-agent-podman-unconfined|inspect-db|db-backup|db-restore|ai-kernel-provision|ai-kernel-serve|ai-kernel-install-service] [flags]")
}

func detectContainerRuntime() (string, error) {
	for _, candidate := range []string{"podman", "docker"} {
		if _, err := exec.LookPath(candidate); err == nil {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("container runtime unavailable: neither podman nor docker found in PATH")
}

func envBool(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func envOrDefault(key string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
