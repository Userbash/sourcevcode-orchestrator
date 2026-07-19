package ops

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBootstrapConfigDefaultsMatchComposeStack(t *testing.T) {
	t.Setenv("OLLAMA_CONTAINER", "")
	t.Setenv("AI_BRIDGE_OLLAMA_VOLUME_NAME", "")

	composePath := filepath.Join(t.TempDir(), "docker-compose.ai.yml")
	if err := os.WriteFile(composePath, []byte("version: \"3.9\"\nservices: {}\n"), 0o644); err != nil {
		t.Fatalf("write compose file: %v", err)
	}

	cfg, err := bootstrapConfigFromEnv(BootstrapOptions{ProjectRoot: filepath.Dir(composePath), ComposeFile: composePath})
	if err != nil {
		t.Fatalf("bootstrapConfigFromEnv: %v", err)
	}

	if cfg.OllamaContainer != "ai_bridge_local_llm" {
		t.Fatalf("unexpected OllamaContainer default: %q", cfg.OllamaContainer)
	}

	if cfg.OllamaVolume != "hebrew_ollama_data" {
		t.Fatalf("unexpected OllamaVolume default: %q", cfg.OllamaVolume)
	}
}

func TestRedactEmptyMasksConfiguredSecrets(t *testing.T) {
	if got := redactEmpty(""); got != "[empty]" {
		t.Fatalf("expected empty marker, got %q", got)
	}
	if got := redactEmpty("super-secret"); got != "[redacted]" {
		t.Fatalf("expected redacted marker, got %q", got)
	}
}

func TestRedactCredentialURLMasksUserInfo(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{name: "postgres", input: "postgresql://bridge:super-secret@127.0.0.1:5432/ai_bridge?sslmode=disable", want: "postgresql://%5Bredacted%5D@127.0.0.1:5432/ai_bridge?sslmode=disable"},
		{name: "amqp", input: "amqp://bridge:super-secret@127.0.0.1:5672/", want: "amqp://%5Bredacted%5D@127.0.0.1:5672/"},
		{name: "no credentials", input: "http://127.0.0.1:8010/health", want: "http://127.0.0.1:8010/health"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := redactCredentialURL(tc.input); got != tc.want {
				t.Fatalf("redactCredentialURL(%q) = %q, want %q", tc.input, got, tc.want)
			}
		})
	}
}

func TestPrintBootstrapSummaryDoesNotLeakSecrets(t *testing.T) {
	t.Setenv("POSTGRES_HOST", "127.0.0.1")
	t.Setenv("POSTGRES_PORT", "5432")
	t.Setenv("POSTGRES_DB", "ai_bridge")
	t.Setenv("POSTGRES_USER", "bridge")
	t.Setenv("POSTGRES_PASSWORD", "super-secret")
	t.Setenv("POSTGRES_URL", "postgresql://bridge:super-secret@127.0.0.1:5432/ai_bridge?sslmode=disable")
	t.Setenv("RABBITMQ_HOST", "127.0.0.1")
	t.Setenv("RABBITMQ_PORT", "5672")
	t.Setenv("RABBITMQ_USER", "bridge")
	t.Setenv("RABBITMQ_PASSWORD", "super-secret")
	t.Setenv("RABBITMQ_URL", "amqp://bridge:super-secret@127.0.0.1:5672/")
	t.Setenv("RABBITMQ_MANAGEMENT_URL", "http://127.0.0.1:15672")

	printed := captureStdout(t, func() {
		printBootstrapSummary(BootstrapConfig{ComposeFile: "docker-compose.yml", OrchestratorPort: "8010"}, BootstrapOptions{})
	})

	blocked := []string{
		"super-secret",
		"postgresql://bridge:super-secret@127.0.0.1:5432/ai_bridge?sslmode=disable",
		"amqp://bridge:super-secret@127.0.0.1:5672/",
	}
	for _, secret := range blocked {
		if strings.Contains(printed, secret) {
			t.Fatalf("bootstrap summary leaked secret %q in output %q", secret, printed)
		}
	}

	required := []string{
		"password=[redacted]",
		"postgresql://%5Bredacted%5D@127.0.0.1:5432/ai_bridge?sslmode=disable",
		"amqp://%5Bredacted%5D@127.0.0.1:5672/",
	}
	for _, marker := range required {
		if !strings.Contains(printed, marker) {
			t.Fatalf("bootstrap summary missing %q in output %q", marker, printed)
		}
	}
}

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	oldStdout := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("create stdout pipe: %v", err)
	}
	os.Stdout = w

	outputCh := make(chan string, 1)
	go func() {
		var buf bytes.Buffer
		_, _ = io.Copy(&buf, r)
		outputCh <- buf.String()
	}()

	fn()
	_ = w.Close()
	os.Stdout = oldStdout
	printed := <-outputCh
	_ = r.Close()
	return printed
}
