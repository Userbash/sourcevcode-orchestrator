package ops

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadEnvFilesResolvesSecretFileReferences(t *testing.T) {
	projectRoot := t.TempDir()
	secretPath := filepath.Join(projectRoot, "openai_api_key.txt")
	if err := os.WriteFile(secretPath, []byte("sk-test-secret\n"), 0o600); err != nil {
		t.Fatalf("write secret file: %v", err)
	}

	envPath := filepath.Join(projectRoot, ".env.bridge")
	envData := "OPENAI_API_KEY_FILE=" + secretPath + "\nSOCRATICODE_ENABLED=true\n"
	if err := os.WriteFile(envPath, []byte(envData), 0o600); err != nil {
		t.Fatalf("write env file: %v", err)
	}

	merged, err := LoadEnvFiles(projectRoot)
	if err != nil {
		t.Fatalf("LoadEnvFiles returned error: %v", err)
	}

	if got := merged["OPENAI_API_KEY"]; got != "sk-test-secret" {
		t.Fatalf("OPENAI_API_KEY = %q, want %q", got, "sk-test-secret")
	}
	if got := merged["SOCRATICODE_ENABLED"]; got != "true" {
		t.Fatalf("SOCRATICODE_ENABLED = %q, want %q", got, "true")
	}
}
