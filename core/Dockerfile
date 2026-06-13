FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY core/requirements-ai.txt /app/core/requirements-ai.txt
RUN pip install --no-cache-dir -r /app/core/requirements-ai.txt

COPY core /app/core
COPY core/bin/agy /usr/local/bin/agy
RUN chmod 0755 /usr/local/bin/agy

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "-m", "core.scripts.orchestrator_daemon"]
