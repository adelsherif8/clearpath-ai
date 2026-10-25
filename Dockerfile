# ClearPath AI on Cloud Run: dashboard + JSON API + MCP server (/mcp/) in one container.
# Base image pinned by digest (A08); Dependabot keeps it current.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY requirements.txt .
# Every package is verified against the sha256 hashes in the lock file (A03).
RUN pip install --require-hashes --no-deps -r requirements.txt
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY data ./data
USER 10001
ENV CP_ENV=production
# No --proxy-headers / --forwarded-allow-ips="*": the app resolves the client IP itself from the
# right-most X-Forwarded-For hop that Cloud Run's front end appends (see app/security.py).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-server-header --no-proxy-headers
