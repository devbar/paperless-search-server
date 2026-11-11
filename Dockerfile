# paperless-search-mcp
# Semantic search over an external Paperless-ngx embedding index.

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY paperless_search_server.py .

# UID/GID of the in-container user. The mounted index file must be readable by
# this user — on Linux hosts either match the file owner's UID
# (docker build --build-arg APP_UID=1000 .) or override at runtime
# (docker run --user 1000 ...).
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid ${APP_GID} appuser && useradd --uid ${APP_UID} --gid appuser --no-create-home appuser
USER appuser

# Defaults; the container expects the DB under /data, mounted read-only.
ENV PAPERLESS_DB=/data/llmindex.db \
    OLLAMA_URL=http://localhost:11434 \
    EMBED_MODEL=embeddinggemma:latest \
    QUERY_PREFIX="" \
    DB_TIMEOUT=5.0 \
    MCP_API_KEY="" \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000
VOLUME ["/data"]

# Only meaningful for HTTP transports; MCP_TRANSPORT=stdio has no listener.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; s = socket.create_connection(('127.0.0.1', int(os.environ.get('MCP_PORT', '8000'))), 3); s.close()"

CMD ["python", "paperless_search_server.py"]
