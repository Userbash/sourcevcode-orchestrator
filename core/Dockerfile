FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY core/requirements-ai.txt /app/core/requirements-ai.txt
RUN pip install --no-cache-dir -r /app/core/requirements-ai.txt

COPY core /app/core

ENV PYTHONPATH=/app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=8s --retries=20 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"

CMD ["python", "-m", "core.scripts.orchestrator_daemon"]
