package ops

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func EnsureEnvFiles(projectRoot string) error {
	pairs := [][2]string{
		{filepath.Join(projectRoot, ".env"), filepath.Join(projectRoot, ".env.example")},
		{filepath.Join(projectRoot, ".env.bridge"), filepath.Join(projectRoot, ".env.bridge.example")},
		{filepath.Join(projectRoot, ".env.gemini.local"), filepath.Join(projectRoot, ".env.gemini.local.example")},
	}
	for _, pair := range pairs {
		if _, err := os.Stat(pair[0]); err == nil {
			continue
		}
		content, err := os.ReadFile(pair[1])
		if err != nil {
			return fmt.Errorf("read template %s: %w", pair[1], err)
		}
		if err := os.WriteFile(pair[0], content, 0o644); err != nil {
			return fmt.Errorf("write env file %s: %w", pair[0], err)
		}
	}
	return nil
}

func LoadEnvFiles(projectRoot string) (map[string]string, error) {
	merged := map[string]string{}
	for _, name := range []string{".env", ".env.bridge", ".env.gemini.local"} {
		path := filepath.Join(projectRoot, name)
		values, err := parseEnvFile(path)
		if err != nil {
			return nil, err
		}
		for key, value := range values {
			if _, exists := os.LookupEnv(key); exists {
				merged[key] = os.Getenv(key)
				continue
			}
			merged[key] = value
		}
	}
	if err := resolveSecretFileReferences(merged); err != nil {
		return nil, err
	}
	for key, value := range merged {
		_ = os.Setenv(key, value)
	}
	return merged, nil
}

func resolveSecretFileReferences(values map[string]string) error {
	for key, value := range values {
		if !strings.HasSuffix(key, "_FILE") {
			continue
		}

		secretKey := strings.TrimSuffix(key, "_FILE")
		if strings.TrimSpace(values[secretKey]) != "" {
			continue
		}

		secretPath := strings.TrimSpace(value)
		if secretPath == "" {
			continue
		}

		content, err := os.ReadFile(secretPath)
		if err != nil {
			return fmt.Errorf("read secret file %s for %s: %w", secretPath, secretKey, err)
		}
		values[secretKey] = strings.TrimSpace(string(content))
	}
	return nil
}

func parseEnvFile(path string) (map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]string{}, nil
		}
		return nil, fmt.Errorf("open env file %s: %w", path, err)
	}
	defer file.Close()
	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "export ") {
			line = strings.TrimSpace(strings.TrimPrefix(line, "export "))
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		value = strings.Trim(value, "\"'")
		if key != "" {
			values[key] = value
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan env file %s: %w", path, err)
	}
	return values, nil
}
