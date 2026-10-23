# ClearPath AI on Cloud Run: dashboard + JSON API + MCP server (/mcp/) in one container.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY templates ./templates
COPY data ./data
# Cloud Run sets $PORT; one worker keeps memory small (free tier)
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips="*"
