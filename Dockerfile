FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Motivation vs Logic: run the container on port 80 so HTTP ingress and docs/default envs are aligned.
ENV HTH_PORT=80 \
    HTH_MCP_PATH=/mcp \
    HTH_MCP_SESSION_IDLE_TIMEOUT_SECONDS=1800 \
    HTH_MCP_RETRY_INTERVAL_MS=2500

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Motivation vs Logic: rely on `.dockerignore` and copy just the runtime packages so docs/mock/test helpers stay out of the image.
COPY app ./app
COPY ui ./ui

EXPOSE 80

CMD ["sh", "-c", "uvicorn ${APP_TARGET:-app.main:app} --host 0.0.0.0 --port ${HTH_PORT:-80}"]
