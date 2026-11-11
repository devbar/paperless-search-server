---
name: paperless-search
description: "Semantic search across the user's Paperless-ngx document archive via the paperless MCP server. Use for any question about contents of archived documents — invoices, letters, contracts, statements — when meaning matters more than exact metadata filters."
version: 1.0.0
author: contributors
license: internal
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Paperless, Documents, Semantic-Search, DMS, MCP]
---

# Paperless Semantic Search

Search the Paperless-ngx archive by **document content** using the `paperless`
MCP server, which serves an embedding index (Ollama `embeddinggemma`, 768 dims,
cosine distance). The archive content is mostly German — querying in German is
fully supported and usually preferable; the embedding model is multilingual.

## Tools

- `paperless_search(query, limit=5)` — semantic top-N hits (document chunks)
- `paperless_document(document_id)` — full indexed text of one document
- `paperless_index_stats()` — index overview (chunks, documents, model)

## When to use it

- Natural-language or associative questions over **contents** ("Stromrechnung vom Sommer", "Vertrag mit Kündigungsklausel", "wie hoch war der Abschlag").
- The user remembers the topic, not the exact title/correspondent/date.
- Exploratory lookups where exact keyword search would miss synonyms.

## When NOT to use it

- Exact metadata filters (correspondent = X, date ranges, tag lists) → use Paperless-ngx's own search/UI/API instead.
- Anything that must include the latest arrivals: the index is refreshed by an external indexer and may lag behind Paperless-ngx. If freshness matters, run `paperless_index_stats` and say so.
- Image-only content: the index covers extracted text, not pictures/layout.

## Working with results

1. Call `paperless_search` with a concise natural-language query; hits carry
   `document_id`, metadata and a `distance` (cosine — smaller = more similar;
   ~0.2–0.5 is typically a good hit here, treat >0.6 as weak).
2. Escalate relevant hits with `paperless_document(document_id)` for the full text.
3. Answer in the user's language and cite source documents by title,
   correspondent, date and filename.

## Boundaries

- The index is read-only for this server; never attempt to modify it.
- If a tool reports the DB as unavailable or Hermes lacks the `paperless`
  tools, the deployment/config is at fault — see the repo README/AGENTS.md,
  do not improvise fixes inside the archive.
