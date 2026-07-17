package memory

import "testing"

func TestNewEmbeddingClientFromEnvFallsBackToAIKernel(t *testing.T) {
	t.Setenv("AI_KERNEL_MODEL_ALIAS", "gemma4-12b-agentic-fable5:q4_k_m")
	t.Setenv("AI_KERNEL_BASE_URL", "http://ai_kernel:8012/v1")

	client := newEmbeddingClientFromEnv()
	if client == nil {
		t.Fatal("expected embedding client")
	}
	if client.model != "gemma4-12b-agentic-fable5:q4_k_m" {
		t.Fatalf("client.model = %q", client.model)
	}
	if client.baseURL != "http://ai_kernel:8012/v1" {
		t.Fatalf("client.baseURL = %q", client.baseURL)
	}
	if !client.configured() {
		t.Fatal("expected client to be configured without API key")
	}
}

func TestNormalizeEmbeddingBaseCanonicalizesEmbeddingsEndpoint(t *testing.T) {
	got := normalizeEmbeddingBase("http://ai_kernel:8012/embeddings")
	if got != "http://ai_kernel:8012" {
		t.Fatalf("normalizeEmbeddingBase() = %q", got)
	}
}
