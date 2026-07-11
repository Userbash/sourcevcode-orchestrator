#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${AI_KERNEL_MODEL_DIR:-${HOME}/.local/share/ai-kernel/models/hauhaucs-qwen36-35b-a3b-aggressive}"
VENV_DIR="${AI_KERNEL_VENV_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ai-kernel/venvs/llama-cpp}"
MODEL_PATH="${AI_KERNEL_MODEL_PATH:-$BASE_DIR/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf}"
MMPROJ_PATH="${AI_KERNEL_MMPROJ_PATH:-$BASE_DIR/mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf}"
HOST="${AI_KERNEL_HOST:-0.0.0.0}"
PORT="${AI_KERNEL_PORT:-8012}"
N_CTX="${AI_KERNEL_N_CTX:-8192}"
N_THREADS="${AI_KERNEL_N_THREADS:-16}"
N_GPU_LAYERS="${AI_KERNEL_N_GPU_LAYERS:-0}"
MODEL_ALIAS="${AI_KERNEL_MODEL_ALIAS:-hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m}"
CHAT_TEMPLATE_KWARGS="${AI_KERNEL_CHAT_TEMPLATE_KWARGS:-{\"enable_thinking\": false}}"

exec "$VENV_DIR/bin/python" -m llama_cpp.server \
  --host "$HOST" \
  --port "$PORT" \
  --model "$MODEL_PATH" \
  --model_alias "$MODEL_ALIAS" \
  --clip_model_path "$MMPROJ_PATH" \
  --chat_format chat_template.default \
  --chat_template_kwargs "$CHAT_TEMPLATE_KWARGS" \
  --n_ctx "$N_CTX" \
  --n_threads "$N_THREADS" \
  --n_gpu_layers "$N_GPU_LAYERS"
