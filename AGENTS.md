# AGENTS.md

Guidance for LLM agents (and humans) working on this repository.

## What this project is

**paperless-search-mcp** — a distributable [MCP](https://modelcontextprotocol.io) server for **semantic search over Paperless-ngx documents**. It exposes an externally built embedding index (SQLite + [sqlite-vec]) to MCP clients and embeds incoming queries with the same Ollama model the index was built with.

**Important context:** Paperless-ngx itself has no embeddings or semantic search; it uses a Whoosh full-text index. The vec0 index (`llmindex.db`) is produced by a **separate indexer that is NOT part of this repository** (LlamaIndex-based, embeddings via Ollama `embeddinggemma:latest`, 768 dims, cosine distance). This server is deliberately **read-only** against that file (fresh `mode=ro` connection + `PRAGMA query_only = ON` per request), so the indexer and this server can use the DB concurrently and readers always see current data.

## Repository layout

| File | Role |
|------|------|
| `paperless_search_server.py` | the entire server (FastMCP, 3 tools) — keep it a single file |
| `requirements.txt` | container dependencies (`mcp`, `sqlite-vec`) |
| `Dockerfile` | image build; `APP_UID`/`APP_GID` build args, non-root, VOLUME `/data`, TCP healthcheck |
| `docker-compose.yml` | deployment example (env_file, `:ro` volume, optional `user:`) |
| `.env.example` | all supported environment variables, documented |
| `README.md` | user-facing setup docs |
| `hermes/config.example.yaml` | versioned Hermes connection schema (`mcp_servers` block + CLI commands) |
| `skills/paperless-search/SKILL.md` | optional Hermes skill: agent guidance for using the tools |

## Tools exposed

- `paperless_search(query, limit=5)` — vec0 `MATCH` with float32-packed embedding from `${OLLAMA_URL}/api/embed`
- `paperless_document(document_id)` — concatenated text of all chunks of one document
- `paperless_index_stats()` — counts + metadata from `index_meta`/`document_meta` tables

Chunk payloads carry LlamaIndex serialization (`node_content` → `_node_content` JSON); `_parse_node` extracts metadata (`title`, `correspondent`, `document_type`, `created`, `tags`, `filename`, `text`).

## Configuration

All config is via environment variables — see `.env.example` (`PAPERLESS_DB`, `OLLAMA_URL`, `EMBED_MODEL`, `QUERY_PREFIX`, `DB_TIMEOUT`, `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`). No config files, no CLI flags. Defaults assume container deployment (`/data/llmindex.db`, transport `streamable-http`, port 8000).

## Build, run, test

```bash
# local, stdio (uv resolves deps from the PEP-723 script header)
uv run paperless_search_server.py

# container
docker build -t paperless-search-mcp .
docker run -d -p 8000:8000 -v /dir/with/llmindex.db:/data:ro \
  -e OLLAMA_URL=http://host.docker.internal:11434 paperless-search-mcp
```

**Smoke test** (MCP Streamable HTTP handshake — note the session dance; `Accept: application/json, text/event-stream` is mandatory):

```bash
S=$(curl -si -X POST http://localhost:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
  -o /dev/null -D - | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')
curl -s -X POST http://localhost:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $S" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -s -X POST http://localhost:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $S" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"paperless_index_stats","arguments":{}}}'
```

Responses are SSE-framed (`event: message` / `data: <json>`). A working stats call returns `isError: false` with chunk/document counts; a search end-to-end verifies Ollama reachability.

## Operational pitfalls (learned from production debugging — do not rediscover)

- **API key is mandatory on HTTP transports**: no `MCP_API_KEY` → the process exits at startup (deliberately loud). Auth is a hand-rolled ASGI middleware (`ApiKeyAuthMiddleware`) because FastMCP 1.x has no static-key auth; it wraps `mcp.streamable_http_app()`/`sse_app()` and we run uvicorn ourselves. Test it: no header → 401, wrong key → 401, `Authorization: Bearer <key>` → normal MCP flow. Hermes clients use config `headers: {Authorization: Bearer ...}`.

- **FastMCP swallows tool exceptions**: they surface only in the MCP result (`isError: true`), NOT in container logs. Debug via the response body; the startup log line (`WARNING PAPERLESS_DB ... not found at startup` from `if __name__ ...` guard) is the only server-side signal for a missing DB path.
- **Anonymous volume trap**: the Dockerfile declares `VOLUME ["/data"]`. If the deployer forgets `-v`, Docker silently creates an empty anonymous volume and the DB "cannot be opened". Check `docker inspect --format '{{range .Mounts}}...'`.
- **Linux file permissions**: bind mounts keep host UID/GID. Container default UID is 10001; a host file owned by e.g. UID 1000 with `0600` yields sqlite `unable to open database file` even though `os.path.exists` passes (it only needs `x` on the directory). Fixes: `docker run --user 1000:1000`, compose `user:` or rebuild `--build-arg APP_UID=1000`. Verified working setup: file `0644`, dir `0755`, owner 1000, runtime UID 1000.
- **WAL is fine and expected**: the indexer runs SQLite in WAL mode. Readers then need write access to `-shm`/`-wal` in the same directory — mount the **directory** read-write (not the file read-only) on production hosts. This server still cannot write (enforced at connection level, not by the mount).
- **Directory > single-file mount**: an atomic file replacement by the indexer breaks single-file bind mounts (inode change).
- **`localhost` inside a container is not the host**: Ollama via `host.docker.internal` (Docker Desktop) or host IP / `network_mode: host` on Linux.
- **Reverse proxies**: when deployed behind a domain, make sure the route targets the *new* container; an orphaned old instance serves `unable to open database file` while the fixed one looks healthy under `docker exec`.
- **curl on Windows/MSYS**: writing streamed SSE bodies with `-o` to `/tmp` can intermittently fail (exit 23); write to a native Windows path instead.

## Conventions

- All code comments and documentation in **English**.
- Keep tool docstrings precise — they are the agent-facing API description.
- `mode=ro` + `PRAGMA query_only = ON` is a hard invariant: never give this server write access paths. If a future feature needs writes, that belongs to the indexer project, not here.
- SQLite is the only backend by design (see README "Read-only behaviour"); a different vector store means a new project, please do not bolt it on.
- Commit messages: short imperative summary.

## Current reference deployment (2026-11)

Hosted internally on a homelab behind `http://paperless-search-mcp.example.internal/mcp` (container runs with a host-matching UID, volume `/data/llm_index` rw for WAL, Ollama at `http://ollama.example.internal:11434`). Hermes consumes it via an URL-based `mcp_servers.paperless` entry — verify with `hermes config get mcp_servers.paperless`.

## Known limitations / extension ideas

- No authentication on the MCP endpoint — deploy only in trusted networks.
- Healthcheck is a TCP connect (HTTP transports); nothing checked for stdio.
- No re-index functionality here — by design (indexer lives elsewhere).
