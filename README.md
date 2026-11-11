# paperless-search-mcp

Distributable MCP server for **semantic search over Paperless-ngx documents**. The server reads an externally built vector index (SQLite + [sqlite-vec]) and answers queries via the Model Context Protocol. Queries are embedded with the same Ollama embedding model the index was built with.

The index file (`llmindex.db`) is **written** only by a separate indexer; this server opens it **read-only** per request and therefore always sees the current state — reader and writer can use the file concurrently.

## Architecture

```
Paperless documents ──> indexer (separate) ──> llmindex.db (SQLite + sqlite-vec)
                                                       │  mounted read-only
                                                       v
                       Ollama (/api/embed) <── paperless-search-mcp (this container)
                                                       │  MCP over HTTP (Streamable HTTP, port 8000)
                                                       v
                                              MCP client (Hermes, Claude, ...)
```

## Quick start

```bash
# 1) Create configuration
cp .env.example .env          # adjust paths/URLs

# 2) Build and start
docker compose up --build -d

# 3) Check
docker logs paperless-search-mcp     # "listening on http://0.0.0.0:8000/mcp"
curl -H "Authorization: Bearer $MCP_API_KEY" \
     http://localhost:8000/mcp       # MCP endpoint (expects POST with JSON-RPC)
```

## Authentication (API key)

The HTTP endpoint is protected by a mandatory API key — set `MCP_API_KEY`
(put it in `.env`; generate e.g. with `openssl rand -hex 32`). The server
**refuses to start** on HTTP transports without it. Clients authenticate per
request with either header:

```
Authorization: Bearer <key>
X-API-Key: <key>
```

Both are compared in constant time; failures get `401` with
`WWW-Authenticate: Bearer`. On `stdio` the key is ignored (no network
surface). Note: no auth implies no TLS either — terminate HTTPS at a reverse
proxy if the endpoint leaves the trusted LAN.

Or without Compose:

```bash
docker build -t paperless-search-mcp .
docker run -d --name paperless-search-mcp \
  -p 8000:8000 \
  -v /path/to/index:/data:ro \
  -e OLLAMA_URL=http://host.docker.internal:11434 \
  paperless-search-mcp
```

> **Important:** mount the **directory**, not the single file. If the indexer
> replaces the file atomically, the mapping stays valid.

## Configuration (environment variables)

| Variable        | Default                       | Description |
|-----------------|-------------------------------|-------------|
| `PAPERLESS_DB`  | `/data/llmindex.db`           | Path to the SQLite index file inside the container |
| `OLLAMA_URL`    | `http://localhost:11434`      | Ollama API; **inside a container `localhost` is the container itself** (see below) |
| `EMBED_MODEL`   | `embeddinggemma:latest`       | Must match the model the index was built with |
| `QUERY_PREFIX`  | *(empty)*                     | Optional prefix for query embeddings |
| `DB_TIMEOUT`    | `5.0`                         | Wait time (s) when the DB is write-locked |
| `MCP_API_KEY`   | *(none — required)*           | API key for HTTP transports; server refuses to start without it |
| `MCP_TRANSPORT` | `streamable-http`             | `streamable-http`, `sse` or `stdio` |
| `MCP_HOST`      | `0.0.0.0`                     | Bind address (HTTP transports only) |
| `MCP_PORT`      | `8000`                        | Bind port (HTTP transports only) |

### Reaching Ollama from the container

- **Docker Desktop (Windows/macOS):** `OLLAMA_URL=http://host.docker.internal:11434`
- **Linux:** use the Docker host IP (e.g. `http://172.17.0.1:11434`) and start Ollama with `OLLAMA_HOST=0.0.0.0`; alternatively use `network_mode: host` or `extra_hosts: ["host.docker.internal:host-gateway"]`.

### File permissions (Linux hosts)

The server runs as an unprivileged user inside the container (default UID
`10001`) and must be able to **read** the mounted index file. Bind mounts keep
the host's numeric UID/GID — a file owned by UID 1000 with `0600` permissions
is therefore not readable and SQLite reports `unable to open database file`.

Two fixes:

```bash
# a) no rebuild: run the container with the host UID (compose: 'user: "1000:1000"')
docker run -d --user 1000:1000 ... paperless-search-mcp

# b) bake the UID into the image (compose: 'build.args.APP_UID: "1000"')
docker build --build-arg APP_UID=1000 --build-arg APP_GID=1000 -t paperless-search-mcp .
```

Alternatively loosen up the host file (`chmod o+r llmindex.db` plus `o+rx` on
the directory).

## Read-only behaviour

Every request opens a fresh connection with `mode=ro` and
`PRAGMA query_only = ON` and closes it afterwards. As a result:

- the server immediately sees new entries written by the indexer (no cache),
- accidental writes are impossible,
- a genuinely read-only mounted volume (`:ro`) works.

**Note on WAL:** if the DB is in WAL mode and being written concurrently,
SQLite must be able to read the `-wal`/`-shm` files — the directory must then
be part of the volume too (which it is with a directory mount).

## Server tools

- `paperless_search(query, limit=5)` — semantic search (top-N chunks with distance)
- `paperless_document(document_id)` — full indexed text of a document
- `paperless_index_stats()` — chunks/documents, model, dimensions, DB path

## Integration in Hermes

Hermes supports static headers on URL-based MCP servers. A ready-to-copy
connection schema lives in [`hermes/config.example.yaml`](hermes/config.example.yaml):

```yaml
# mcp_servers:
  paperless:
    url: http://localhost:8000/mcp
    timeout: 120
    headers:
      Authorization: Bearer <same key as MCP_API_KEY>
```

The same directory documents the equivalent `hermes config set` CLI commands.
The existing local server can keep running untouched or be replaced by the
container. An optional agent skill (when/how to use the tools) is provided in
[`skills/paperless-search/`](skills/paperless-search/) — copy that folder into
the Hermes skills directory to activate it.

## Local usage without Docker (stdio)

```bash
uv run paperless_search_server.py    # MCP_TRANSPORT=stdio, e.g. local in Hermes
```

The script header contains PEP-723 inline metadata, so `uv` resolves the
dependencies automatically — inside the container the packages installed via
`requirements.txt` are used instead.

## License

Internal project.
