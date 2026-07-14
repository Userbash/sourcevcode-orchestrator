FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1

RUN apt-get update     && apt-get install -y --no-install-recommends build-essential cmake git wget curl ca-certificates     && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel     && CMAKE_ARGS="-DGGML_NATIVE=OFF" python -m pip install "llama-cpp-python[server]"

COPY docker/ai-kernel/entrypoint.sh /usr/local/bin/ai-kernel-entrypoint
RUN chmod +x /usr/local/bin/ai-kernel-entrypoint

WORKDIR /models
EXPOSE 8012
ENTRYPOINT ["/usr/local/bin/ai-kernel-entrypoint"]
