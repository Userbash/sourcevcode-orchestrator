package kernel

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

const defaultModelCatalogPath = "config/kernel-models.txt"

type modelCatalog struct {
	mu      sync.Mutex
	path    string
	entries map[string]map[string]struct{}
}

func loadModelCatalog(path string) (*modelCatalog, error) {
	path = strings.TrimSpace(path)
	if path == "" {
		path = defaultModelCatalogPath
	}
	catalog := &modelCatalog{
		path:    filepath.Clean(path),
		entries: map[string]map[string]struct{}{},
	}
	file, err := os.Open(catalog.path)
	if err != nil {
		if os.IsNotExist(err) {
			return catalog, nil
		}
		return nil, fmt.Errorf("open model catalog %s: %w", catalog.path, err)
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		provider, model, ok := parseCatalogEntry(line)
		if !ok {
			continue
		}
		catalog.add(provider, model)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan model catalog %s: %w", catalog.path, err)
	}
	return catalog, nil
}

func parseCatalogEntry(line string) (string, string, bool) {
	provider, model, ok := strings.Cut(line, ":")
	if !ok {
		return "", "", false
	}
	provider = strings.ToLower(strings.TrimSpace(provider))
	model = strings.TrimSpace(model)
	if provider == "" || model == "" {
		return "", "", false
	}
	return provider, model, true
}

func (c *modelCatalog) hasProvider(provider string) bool {
	if c == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.entries[strings.ToLower(strings.TrimSpace(provider))]
	return ok
}

func (c *modelCatalog) filter(provider string, models []domain.ProviderModelStatus) []domain.ProviderModelStatus {
	if c == nil {
		return models
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	allowed, ok := c.entries[strings.ToLower(strings.TrimSpace(provider))]
	if !ok || len(allowed) == 0 {
		return models
	}
	filtered := make([]domain.ProviderModelStatus, 0, len(models))
	for _, model := range models {
		if _, ok := allowed[strings.ToLower(strings.TrimSpace(model.ModelName))]; ok {
			filtered = append(filtered, model)
		}
	}
	return filtered
}

func (c *modelCatalog) syncProvider(provider string, models []domain.ProviderModelStatus) error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, model := range models {
		if strings.TrimSpace(model.ModelName) == "" {
			continue
		}
		if model.Status == "missing" || model.Status == "disabled" || model.InventoryStatus == "inventory_missing" {
			continue
		}
		if model.Available || model.InventoryStatus == "inventory_verified" || model.VerificationStatus == "verifying" || model.IsDefault {
			c.add(strings.ToLower(strings.TrimSpace(provider)), model.ModelName)
		}
	}
	return c.writeLocked()
}

func (c *modelCatalog) add(provider string, model string) {
	if c.entries == nil {
		c.entries = map[string]map[string]struct{}{}
	}
	provider = strings.ToLower(strings.TrimSpace(provider))
	model = strings.ToLower(strings.TrimSpace(model))
	if provider == "" || model == "" {
		return
	}
	if _, ok := c.entries[provider]; !ok {
		c.entries[provider] = map[string]struct{}{}
	}
	c.entries[provider][model] = struct{}{}
}

func (c *modelCatalog) writeLocked() error {
	if c == nil || c.path == "" {
		return nil
	}
	dir := filepath.Dir(c.path)
	if dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("mkdir model catalog dir %s: %w", dir, err)
		}
	}
	lines := []string{
		"# provider:model",
		"# This file is maintained by go-core model registry and can be edited manually.",
	}
	providers := make([]string, 0, len(c.entries))
	for provider := range c.entries {
		providers = append(providers, provider)
	}
	sort.Strings(providers)
	for _, provider := range providers {
		models := make([]string, 0, len(c.entries[provider]))
		for model := range c.entries[provider] {
			models = append(models, model)
		}
		sort.Strings(models)
		for _, model := range models {
			lines = append(lines, provider+":"+model)
		}
	}
	content := strings.Join(lines, "\n") + "\n"
	tmp := c.path + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return fmt.Errorf("write model catalog temp %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, c.path); err != nil {
		return fmt.Errorf("replace model catalog %s: %w", c.path, err)
	}
	return nil
}
