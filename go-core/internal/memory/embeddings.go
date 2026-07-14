package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const defaultEmbeddingTimeout = 30 * time.Second

type embeddingClient struct {
	baseURL    string
	apiKey     string
	model      string
	dimensions int
	client     *http.Client
}

type embeddingRequest struct {
	Input      string `json:"input"`
	Model      string `json:"model"`
	Dimensions int    `json:"dimensions,omitempty"`
}

type embeddingResponse struct {
	Data []struct {
		Embedding []float64 `json:"embedding"`
	} `json:"data"`
}

func newEmbeddingClientFromEnv() *embeddingClient {
	model := strings.TrimSpace(firstNonEmptyEnv(
		"GO_CORE_RAG_EMBEDDING_MODEL",
		"AI_BRIDGE_RAG_EMBEDDING_MODEL",
	))
	if model == "" {
		return nil
	}
	baseURL := normalizeEmbeddingBase(firstNonEmptyEnv(
		"GO_CORE_RAG_EMBEDDING_BASE_URL",
		"AI_BRIDGE_RAG_EMBEDDING_BASE_URL",
		"OPENAI_BASE_URL",
		"AI_BRIDGE_OPENAI_BASE_URL",
	))
	if baseURL == "" {
		baseURL = "https://api.openai.com/v1"
	}
	apiKey := strings.TrimSpace(firstNonEmptyEnv(
		"GO_CORE_RAG_EMBEDDING_API_KEY",
		"AI_BRIDGE_RAG_EMBEDDING_API_KEY",
		"OPENAI_API_KEY",
		"CODEX_SALE_API_KEY",
	))
	if apiKey == "" {
		return nil
	}
	return &embeddingClient{
		baseURL:    baseURL,
		apiKey:     apiKey,
		model:      model,
		dimensions: envInt("GO_CORE_RAG_EMBEDDING_DIMENSIONS", 0),
		client:     &http.Client{Timeout: defaultEmbeddingTimeout},
	}
}

func (c *embeddingClient) configured() bool {
	return c != nil && c.baseURL != "" && c.apiKey != "" && c.model != ""
}

func (c *embeddingClient) embed(ctx context.Context, text string) ([]float64, error) {
	if !c.configured() {
		return nil, errEmbeddingsNotConfigured
	}
	payload, err := json.Marshal(embeddingRequest{
		Input:      text,
		Model:      c.model,
		Dimensions: c.dimensions,
	})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.baseURL, "/")+"/embeddings", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("embeddings provider returned HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var decoded embeddingResponse
	if err := json.Unmarshal(body, &decoded); err != nil {
		return nil, err
	}
	if len(decoded.Data) == 0 || len(decoded.Data[0].Embedding) == 0 {
		return nil, fmt.Errorf("embeddings provider returned empty embedding")
	}
	return decoded.Data[0].Embedding, nil
}

func normalizeEmbeddingBase(value string) string {
	value = strings.TrimSpace(value)
	value = strings.TrimSuffix(value, "/")
	if value == "" {
		return ""
	}
	if strings.HasSuffix(value, "/embeddings") {
		return strings.TrimSuffix(value, "/embeddings")
	}
	return strings.TrimSuffix(value, "/v1") + "/v1"
}

func firstNonEmptyEnv(keys ...string) string {
	for _, key := range keys {
		if value := strings.TrimSpace(os.Getenv(key)); value != "" {
			return value
		}
	}
	return ""
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}
