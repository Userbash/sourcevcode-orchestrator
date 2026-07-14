#!/usr/bin/env bash
set -euo pipefail

MODEL_ID="${AI_KERNEL_MODEL_ID:-HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive}"
MODEL_DIR="${AI_KERNEL_MODEL_DIR:-/models/hauhaucs-qwen36-35b-a3b-aggressive}"
MODEL_FILE="${AI_KERNEL_MODEL_FILE:-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf}"
MMPROJ_FILE="${AI_KERNEL_MMPROJ_FILE:-mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf}"
MODEL_PATH="${AI_KERNEL_MODEL_PATH:-$MODEL_DIR/$MODEL_FILE}"
MMPROJ_PATH="${AI_KERNEL_MMPROJ_PATH:-$MODEL_DIR/$MMPROJ_FILE}"
HOST="${AI_KERNEL_HOST:-0.0.0.0}"
PORT="${AI_KERNEL_PORT:-8012}"
N_CTX="${AI_KERNEL_N_CTX:-8192}"
N_THREADS="${AI_KERNEL_N_THREADS:-16}"
N_GPU_LAYERS="${AI_KERNEL_N_GPU_LAYERS:-0}"
MODEL_ALIAS="${AI_KERNEL_MODEL_ALIAS:-hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m}"
CHAT_TEMPLATE_KWARGS="${AI_KERNEL_CHAT_TEMPLATE_KWARGS:-{"enable_thinking": false}}"
HF_BASE_URL="${AI_KERNEL_HF_BASE_URL:-https://huggingface.co}"

mkdir -p "$MODEL_DIR"
cd "$MODEL_DIR"

if [ ! -f "$MODEL_FILE" ]; then
  echo "[ai-kernel] downloading $MODEL_FILE"
  wget -c "${HF_BASE_URL}/${MODEL_ID}/resolve/main/${MODEL_FILE}?download=true" -O "$MODEL_FILE"
fi

if [ ! -f "$MMPROJ_FILE" ]; then
  echo "[ai-kernel] downloading $MMPROJ_FILE"
  wget -c "${HF_BASE_URL}/${MODEL_ID}/resolve/main/${MMPROJ_FILE}?download=true" -O "$MMPROJ_FILE"
fi

exec python -m llama_cpp.server   --host "$HOST"   --port "$PORT"   --model "$MODEL_PATH"   --model_alias "$MODEL_ALIAS"   --clip_model_path "$MMPROJ_PATH"   --chat_format chat_template.default   --chat_template_kwargs "$CHAT_TEMPLATE_KWARGS"   --n_ctx "$N_CTX"   --n_threads "$N_THREADS"   --n_gpu_layers "$N_GPU_LAYERS"
