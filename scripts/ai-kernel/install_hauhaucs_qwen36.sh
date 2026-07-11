#!/usr/bin/env bash
set -euo pipefail

MODEL_ID="HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive"
BASE_DIR="${AI_KERNEL_MODEL_DIR:-${HOME}/.local/share/ai-kernel/models/hauhaucs-qwen36-35b-a3b-aggressive}"
VENV_DIR="${AI_KERNEL_VENV_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ai-kernel/venvs/llama-cpp}"
MODEL_FILE="Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
MMPROJ_FILE="mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf"

mkdir -p "$BASE_DIR"
mkdir -p "$(dirname "$VENV_DIR")"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3-pip python3-venv build-essential cmake pkg-config git wget
  fi
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
if ! "$VENV_DIR/bin/python" -c 'import llama_cpp, llama_cpp.server' >/dev/null 2>&1; then
  "$VENV_DIR/bin/python" -m pip install 'llama-cpp-python[server]'
fi

cd "$BASE_DIR"
[ -f "$MODEL_FILE" ] || wget -c "https://huggingface.co/${MODEL_ID}/resolve/main/${MODEL_FILE}?download=true" -O "$MODEL_FILE"
[ -f "$MMPROJ_FILE" ] || wget -c "https://huggingface.co/${MODEL_ID}/resolve/main/${MMPROJ_FILE}?download=true" -O "$MMPROJ_FILE"

ls -lh "$BASE_DIR"
