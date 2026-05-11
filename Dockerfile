FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ENV ACNT_PORT=80 \
    ACNT_MCP_PATH=/mcp \
    ACNT_MCP_SESSION_IDLE_TIMEOUT_SECONDS=1800 \
    ACNT_MCP_RETRY_INTERVAL_MS=2500

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 80

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${ACNT_PORT:-80} --proxy-headers --forwarded-allow-ips='*'"]
