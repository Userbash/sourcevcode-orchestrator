#!/usr/bin/env bash
set -euo pipefail

MODEL_ID="HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive"
BASE_DIR="${AI_KERNEL_MODEL_DIR:-/var/home/sanya/.local/share/ai-kernel/models/hauhaucs-qwen36-35b-a3b-aggressive}"
VENV_DIR="${AI_KERNEL_VENV_DIR:-/var/home/sanya/.local/share/ai-kernel/venvs/llama-cpp}"
MODEL_FILE="Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
MMPROJ_FILE="mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf"

mkdir -p "$BASE_DIR"
mkdir -p "$(dirname "$VENV_DIR")"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3-pip python3-venv build-essential cmake pkg-config git wget
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
  "$VENV_DIR/bin/pip" install 'llama-cpp-python[server]'
fi

cd "$BASE_DIR"
[ -f "$MODEL_FILE" ] || wget -c "https://huggingface.co/${MODEL_ID}/resolve/main/${MODEL_FILE}?download=true" -O "$MODEL_FILE"
[ -f "$MMPROJ_FILE" ] || wget -c "https://huggingface.co/${MODEL_ID}/resolve/main/${MMPROJ_FILE}?download=true" -O "$MMPROJ_FILE"

ls -lh "$BASE_DIR"
