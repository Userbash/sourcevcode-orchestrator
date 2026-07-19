package ops

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRootComposeMountsRabbitMQConfig(t *testing.T) {
	projectRoot := locateProjectRoot(t)
	content := readFile(t, filepath.Join(projectRoot, "docker-compose.yml"))

	required := []string{
		"./infra/rabbitmq/rabbitmq.conf:/etc/rabbitmq/rabbitmq.conf:Z,ro",
		"./infra/rabbitmq/enabled_plugins:/etc/rabbitmq/enabled_plugins:Z,ro",
	}
	for _, needle := range required {
		if !strings.Contains(content, needle) {
			t.Fatalf("docker-compose.yml missing %q", needle)
		}
	}
}

func TestPodmanStackMountsRabbitMQConfig(t *testing.T) {
	projectRoot := locateProjectRoot(t)
	content := readFile(t, filepath.Join(projectRoot, "scripts", "run-podman-stack.sh"))

	required := []string{
		`-v "$ROOT_DIR/infra/rabbitmq/rabbitmq.conf":/etc/rabbitmq/rabbitmq.conf:ro,Z`,
		`-v "$ROOT_DIR/infra/rabbitmq/enabled_plugins":/etc/rabbitmq/enabled_plugins:ro,Z`,
	}
	for _, needle := range required {
		if !strings.Contains(content, needle) {
			t.Fatalf("run-podman-stack.sh missing %q", needle)
		}
	}
}

func TestGrafanaProvisioningDoesNotUsePlaceholderFiles(t *testing.T) {
	projectRoot := locateProjectRoot(t)
	paths := []string{
		filepath.Join(projectRoot, "infra", "grafana", "provisioning", "alerting", ".keep"),
		filepath.Join(projectRoot, "infra", "grafana", "provisioning", "plugins", ".keep"),
	}
	for _, path := range paths {
		if _, err := os.Stat(path); err == nil {
			t.Fatalf("placeholder provisioning file should not exist: %s", path)
		}
	}
}

func TestGrafanaDockerfileUsesBaseEntrypoint(t *testing.T) {
	projectRoot := locateProjectRoot(t)
	content := readFile(t, filepath.Join(projectRoot, "infra", "grafana", "Dockerfile"))

	forbidden := []string{
		"ENTRYPOINT",
		"docker-entrypoint-clean-plugins.sh",
	}
	for _, needle := range forbidden {
		if strings.Contains(content, needle) {
			t.Fatalf("Grafana Dockerfile should rely on the upstream entrypoint; found %q", needle)
		}
	}
}

func TestLokiSingleBinaryConfigIncludesLocalRingAddress(t *testing.T) {
	projectRoot := locateProjectRoot(t)
	content := readFile(t, filepath.Join(projectRoot, "infra", "loki", "loki-config.yaml"))

	required := []string{
		"instance_addr: 127.0.0.1",
		"active_index_directory: /loki/index",
		"cache_location: /loki/index_cache",
		"directory: /loki/chunks",
	}
	for _, needle := range required {
		if !strings.Contains(content, needle) {
			t.Fatalf("loki-config.yaml missing %q", needle)
		}
	}
}

func locateProjectRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	projectRoot := filepath.Clean(filepath.Join(wd, "..", "..", ".."))
	if _, err := os.Stat(filepath.Join(projectRoot, "docker-compose.yml")); err != nil {
		t.Fatalf("project root missing docker-compose.yml: %v", err)
	}
	return projectRoot
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(data)
}
