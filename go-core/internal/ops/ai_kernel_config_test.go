package ops

import "testing"

func TestAIKernelConfigFromEnvThinkingProfile(t *testing.T) {
	t.Setenv("AI_KERNEL_MODEL_ALIAS", "gemma4-12b-agentic-fable5:q4_k_m")
	t.Setenv("AI_KERNEL_MODEL_ID", "yuxinlu1/gemma-4-12B-agentic-fable5-composer2.5-v2-3.5x-tau2-GGUF")
	t.Setenv("AI_KERNEL_MODEL_FILE", "gemma4-v2-Q4_K_M.gguf")
	t.Setenv("AI_KERNEL_MMPROJ_FILE", "")
	t.Setenv("AI_KERNEL_REASONING_PROFILE", "thinking")
	t.Setenv("AI_KERNEL_ENABLE_THINKING", "true")
	t.Setenv("AI_KERNEL_ENABLE_EMBEDDINGS", "true")

	cfg := AIKernelConfigFromEnv("")
	if cfg.ReasoningProfile != "thinking" {
		t.Fatalf("ReasoningProfile = %q", cfg.ReasoningProfile)
	}
	if cfg.ChatTemplateKwargs != `{"enable_thinking": true}` {
		t.Fatalf("ChatTemplateKwargs = %q", cfg.ChatTemplateKwargs)
	}
	if cfg.MMProjPath != "" {
		t.Fatalf("MMProjPath = %q", cfg.MMProjPath)
	}
	if !cfg.EnableEmbeddings {
		t.Fatal("expected embeddings enabled")
	}
}
