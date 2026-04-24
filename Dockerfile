FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ENV HTH_PORT=3000 \
    HTH_MCP_PATH=/mcp \
    HTH_MCP_SESSION_IDLE_TIMEOUT_SECONDS=1800 \
    HTH_MCP_RETRY_INTERVAL_MS=2500

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000

CMD ["sh", "-c", "uvicorn ${APP_TARGET:-app.main:app} --host 0.0.0.0 --port ${HTH_PORT:-3000}"]
