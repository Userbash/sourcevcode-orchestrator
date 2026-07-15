package ops

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"

	"sourcevcode-orchestrator/go-core/internal/app"
)

type ComposeCommand struct {
	Name string
	Args []string
}

func DetectComposeCommand(ctx context.Context) (ComposeCommand, error) {
	candidates := []ComposeCommand{
		{Name: "podman", Args: []string{"compose"}},
		{Name: "podman-compose"},
		{Name: "docker", Args: []string{"compose"}},
		{Name: "docker-compose"},
	}
	for _, candidate := range candidates {
		args := append(append([]string{}, candidate.Args...), "version")
		result := RunCommand(ctx, candidate.Name, args...)
		if result.ReturnCode == 0 {
			return candidate, nil
		}
	}
	return ComposeCommand{}, fmt.Errorf("no supported compose command found (tried podman compose, podman-compose, docker compose, docker-compose)")
}

func ResolveComposeFilePath(projectRoot string, composeFile string) (string, error) {
	resolvedFile := strings.TrimSpace(composeFile)
	if resolvedFile == "" {
		resolvedFile = "docker-compose.ai.yml"
	}
	if !filepath.IsAbs(resolvedFile) {
		resolvedFile = filepath.Join(projectRoot, resolvedFile)
	}
	if _, err := os.Stat(resolvedFile); err != nil {
		return "", fmt.Errorf("compose file not found: %s", resolvedFile)
	}
	return resolvedFile, nil
}

func ComposeUp(ctx context.Context, projectRoot string, composeFile string, services ...string) error {
	command, err := DetectComposeCommand(ctx)
	if err != nil {
		return err
	}
	resolvedFile, err := ResolveComposeFilePath(projectRoot, composeFile)
	if err != nil {
		return err
	}
	args := append(append([]string{}, command.Args...), "-f", resolvedFile, "up", "-d")
	args = append(args, services...)
	return runMust(ctx, command.Name, args...)
}

func EnsureCoreInfra(ctx context.Context, projectRoot string, composeFile string, attempts int, delay time.Duration) error {
	composeErr := ComposeUp(ctx, projectRoot, composeFile, "db", "rabbitmq")
	if composeErr != nil {
		if err := EnsureCoreInfraDirectPodman(ctx, projectRoot); err != nil {
			return fmt.Errorf("compose startup failed: %v; direct podman fallback failed: %w", composeErr, err)
		}
	}
	postgres := app.ResolvePostgresConnectionInfo()
	if err := WaitForTCP(ctx, postgres.Host, postgres.Port, attempts, delay); err != nil {
		return err
	}
	rabbit := app.ResolveRabbitMQConnectionInfo()
	if err := WaitForTCP(ctx, rabbit.Host, rabbit.Port, attempts, delay); err != nil {
		return err
	}
	if err := WaitForRabbitMQAMQP(ctx, rabbit.AMQPURL, attempts, delay); err != nil {
		return err
	}
	if err := WaitForTCP(ctx, rabbit.Host, "15672", attempts, delay); err != nil {
		return err
	}
	return nil
}

func EnsureCoreInfraDirectPodman(ctx context.Context, projectRoot string) error {
	if err := RequireCommand("podman"); err != nil {
		return err
	}
	if result := RunCommand(ctx, "podman", "version"); result.ReturnCode != 0 {
		return fmt.Errorf("podman unavailable: %s", firstNonEmpty(result.Stderr, result.Stdout))
	}
	if err := ensureDirectPodmanPostgres(ctx, projectRoot); err != nil {
		return err
	}
	if err := ensureDirectPodmanRabbitMQ(ctx); err != nil {
		return err
	}
	return nil
}

func ensureDirectPodmanPostgres(ctx context.Context, projectRoot string) error {
	const containerName = "ai_bridge_db"
	if ensureContainerRunning(ctx, containerName) {
		return nil
	}
	if err := ensureVolume(ctx, envOrDefaultOps("AI_BRIDGE_PG_DATA_VOLUME_NAME", "hebrew_pg_data")); err != nil {
		return err
	}
	postgres := app.ResolvePostgresConnectionInfo()
	args := []string{
		"run", "-d",
		"--name", containerName,
		"--restart", "unless-stopped",
		"-p", firstNonEmpty(postgres.Port, "5432") + ":5432",
		"-e", "POSTGRES_USER=" + postgres.User,
		"-e", "POSTGRES_PASSWORD=" + postgres.Password,
		"-e", "POSTGRES_DB=" + postgres.Database,
		"-v", envOrDefaultOps("AI_BRIDGE_PG_DATA_VOLUME_NAME", "hebrew_pg_data") + ":/var/lib/postgresql/data",
	}
	initDir := filepath.Join(projectRoot, "docker", "postgres", "init")
	if stat, err := os.Stat(initDir); err == nil && stat.IsDir() {
		args = append(args, "-v", initDir+":/docker-entrypoint-initdb.d:ro,z")
	}
	args = append(args,
		"docker.io/pgvector/pgvector:pg16",
		"postgres",
		"-c", "shared_buffers=256MB",
		"-c", "max_connections=200",
		"-c", "effective_cache_size=768MB",
		"-c", "work_mem=16MB",
		"-c", "wal_level=logical",
		"-c", "track_commit_timestamp=on",
	)
	return runMust(ctx, "podman", args...)
}

func ensureDirectPodmanRabbitMQ(ctx context.Context) error {
	const containerName = "ai_bridge_rabbitmq"
	if ensureContainerRunning(ctx, containerName) {
		return nil
	}
	rabbit := app.ResolveRabbitMQConnectionInfo()
	args := []string{
		"run", "-d",
		"--name", containerName,
		"--restart", "unless-stopped",
		"-p", firstNonEmpty(rabbit.Port, "5672") + ":5672",
		"-p", "15672:15672",
		"-e", "RABBITMQ_DEFAULT_USER=" + rabbit.User,
		"-e", "RABBITMQ_DEFAULT_PASS=" + rabbit.Password,
		"docker.io/library/rabbitmq:3-management",
	}
	return runMust(ctx, "podman", args...)
}

func ensureContainerRunning(ctx context.Context, container string) bool {
	if !exists(ctx, container) {
		return false
	}
	result := RunCommand(ctx, "podman", "inspect", "--format", "{{.State.Running}}", container)
	if result.ReturnCode == 0 && strings.EqualFold(strings.TrimSpace(result.Stdout), "true") {
		return true
	}
	_ = runMust(ctx, "podman", "start", container)
	result = RunCommand(ctx, "podman", "inspect", "--format", "{{.State.Running}}", container)
	return result.ReturnCode == 0 && strings.EqualFold(strings.TrimSpace(result.Stdout), "true")
}

func WaitForTCP(ctx context.Context, host string, port string, attempts int, delay time.Duration) error {
	address := strings.TrimSpace(host) + ":" + strings.TrimSpace(port)
	for i := 0; i < attempts; i++ {
		dialer := net.Dialer{Timeout: 3 * time.Second}
		conn, err := dialer.DialContext(ctx, "tcp", address)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
	return fmt.Errorf("tcp endpoint not ready: %s", address)
}

func WaitForRabbitMQAMQP(ctx context.Context, amqpURL string, attempts int, delay time.Duration) error {
	for i := 0; i < attempts; i++ {
		conn, err := amqp.DialConfig(amqpURL, amqp.Config{
			Dial: amqp.DefaultDial(3 * time.Second),
		})
		if err == nil {
			_ = conn.Close()
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
	return fmt.Errorf("rabbitmq amqp endpoint not ready: %s", amqpURL)
}

func envOrDefaultOps(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value != "" {
		return value
	}
	return fallback
}
