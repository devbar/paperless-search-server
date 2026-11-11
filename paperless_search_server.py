# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp>=1.2.0,<2",
#     "sqlite-vec>=0.1.6",
# ]
# ///
"""Paperless semantic search MCP server (distributable container build).

Searches a SQLite/sqlite-vec embedding index (llmindex.db) built from
Paperless-ngx documents with an Ollama embedding model (default:
embeddinggemma:latest, 768 dims, cosine distance). Queries are embedded with
the SAME model via the Ollama API.

The database is opened read-only for every request, so the server always sees
the current state of a file that may be written by a separate indexer.

Config via environment variables (defaults shown):
  PAPERLESS_DB    /data/llmindex.db     path to the SQLite embedding index
  OLLAMA_URL      http://localhost:11434
  EMBED_MODEL     embeddinggemma:latest
  QUERY_PREFIX    ""   (the index holds raw chunk text, so queries are
                        embedded raw too for consistency. Try
                        "task: search result | query: " as an alternative.)
  DB_TIMEOUT      5.0                   seconds to wait if the writer holds a lock
  MCP_TRANSPORT   streamable-http       one of: streamable-http, sse, stdio
  MCP_HOST        0.0.0.0               bind address (HTTP transports only)
  MCP_PORT        8000                  bind port (HTTP transports only)
  MCP_API_KEY     ""                    API key protecting the HTTP endpoint.
                        REQUIRED for HTTP transports (server refuses to start
                        without it); clients send it as
                        "Authorization: Bearer <key>" or "X-API-Key: <key>".
                        Ignored for stdio (no network surface).
"""

import hmac
import json
import logging
import os
import sqlite3
import struct
import sys
import urllib.request

import sqlite_vec
from mcp.server.fastmcp import FastMCP

DB_PATH = os.environ.get("PAPERLESS_DB", "/data/llmindex.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embeddinggemma:latest")
QUERY_PREFIX = os.environ.get("QUERY_PREFIX", "")
DB_TIMEOUT = float(os.environ.get("DB_TIMEOUT", "5.0"))

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http").lower()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

_VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")
if MCP_TRANSPORT not in _VALID_TRANSPORTS:
    sys.exit(f"Unsupported MCP_TRANSPORT={MCP_TRANSPORT!r} (expected one of {_VALID_TRANSPORTS})")

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paperless-search")

mcp = FastMCP("paperless-search", host=MCP_HOST, port=MCP_PORT)


class ApiKeyAuthMiddleware:
    """Pure ASGI middleware: every HTTP request requires the API key.

    Accepted credentials (constant-time compared):
      Authorization: Bearer <key>   or   X-API-Key: <key>
    Non-HTTP scopes (lifespan) pass through untouched.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        provided = None
        for name, value in scope["headers"]:
            key = name.lower()
            if key == b"authorization" and value.lower().startswith(b"bearer "):
                provided = value[7:]
            elif key == b"x-api-key":
                provided = value
        if provided is None or not hmac.compare_digest(provided, self.api_key):
            body = (b'{"error":"unauthorized","message":"Valid API key required '
                    b'(Authorization: Bearer <key> or X-API-Key: <key>)"}')
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="paperless-search"'),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _db() -> sqlite3.Connection:
    """Open a fresh read-only connection so each request sees current data."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    con.execute("PRAGMA query_only = ON")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    return con


def _embed(text: str) -> bytes:
    """Embed text via Ollama and return packed float32 bytes for vec0 MATCH."""
    payload = json.dumps({"model": EMBED_MODEL, "input": QUERY_PREFIX + text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    vec = data["embeddings"][0]
    return struct.pack(f"<{len(vec)}f", *vec)


def _parse_node(node_json: str) -> dict:
    d = json.loads(node_json)
    inner = json.loads(d.get("_node_content") or "{}")
    return {
        "title": d.get("title"),
        "correspondent": d.get("correspondent"),
        "document_type": d.get("document_type"),
        "created": d.get("created"),
        "tags": d.get("tags"),
        "filename": d.get("filename"),
        "text": inner.get("text", ""),
    }


@mcp.tool()
def paperless_search(query: str, limit: int = 5) -> list[dict]:
    """Semantic search across the Paperless documents (embedding index).

    Args:
        query: Search term or question in natural language.
        limit: Maximum number of hits (chunks), default 5.

    Returns:
        List of hits with document_id, title, correspondent, document type,
        date, distance (smaller = more similar) and the chunk text.
    """
    limit = max(1, min(int(limit), 25))
    qvec = _embed(query)
    con = _db()
    try:
        rows = con.execute(
            "SELECT id, document_id, node_content, distance "
            "FROM documents WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (qvec, limit),
        ).fetchall()
    finally:
        con.close()
    results = []
    for _id, doc_id, node_json, dist in rows:
        hit = _parse_node(node_json)
        hit["document_id"] = doc_id
        hit["chunk_id"] = _id
        hit["distance"] = round(float(dist), 4)
        results.append(hit)
    return results


@mcp.tool()
def paperless_document(document_id: int) -> dict:
    """Load the complete indexed text of a Paperless document.

    Args:
        document_id: The Paperless document ID (e.g. from paperless_search).

    Returns:
        Document metadata and the full text of all chunks.
    """
    con = _db()
    try:
        rows = con.execute(
            "SELECT node_content FROM documents WHERE document_id = ?",
            (int(document_id),),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return {"document_id": document_id, "error": "Document not found in the index"}
    parts = [_parse_node(r[0]) for r in rows]
    meta = {k: parts[0][k] for k in ("title", "correspondent", "document_type", "created", "tags", "filename")}
    meta["document_id"] = document_id
    meta["chunks"] = len(parts)
    meta["text"] = "\n\n---\n\n".join(p["text"] for p in parts)
    return meta


@mcp.tool()
def paperless_index_stats() -> dict:
    """Statistics on the embedding index (chunk/document counts, model, dimension)."""
    con = _db()
    try:
        meta = dict(con.execute("SELECT key, value FROM index_meta").fetchall())
        chunks = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        docs = con.execute("SELECT COUNT(*) FROM document_meta").fetchone()[0]
    finally:
        con.close()
    return {
        "chunks": chunks,
        "documents": docs,
        "embed_model": meta.get("embed_model"),
        "dimensions": meta.get("dim"),
        "total_inserts": meta.get("total_inserts"),
        "db_path": DB_PATH,
    }


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        log.warning("PAPERLESS_DB %s not found at startup (will retry per request)", DB_PATH)
    log.info("transport=%s db=%s ollama=%s model=%s",
             MCP_TRANSPORT, DB_PATH, OLLAMA_URL, EMBED_MODEL)
    if MCP_TRANSPORT == "stdio":
        # Local stdio: no network surface, API key not required.
        mcp.run(transport="stdio")
    else:
        if not MCP_API_KEY:
            sys.exit("MCP_API_KEY is required for HTTP transports (set it in the "
                     "environment; without it every request would be unauthenticated).")
        log.info("API key auth enabled; listening on http://%s:%d/mcp", MCP_HOST, MCP_PORT)
        import uvicorn
        app = mcp.streamable_http_app() if MCP_TRANSPORT == "streamable-http" else mcp.sse_app()
        uvicorn.run(ApiKeyAuthMiddleware(app, MCP_API_KEY),
                    host=MCP_HOST, port=MCP_PORT, log_level="info")
