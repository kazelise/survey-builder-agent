#!/usr/bin/env python3
"""Builds `agent/data/handbook_index.json` (chunked `docs/*.md` +
`docs-site/docs/**/*.md`, see `rag/chunker.py`) and, optionally,
`agent/data/handbook_embeddings.json` (Ollama `bge-m3` vectors for the
same chunks, used by `search_handbook`'s hybrid leg -- see
`rag/embeddings.py`).

Usage (from `agent/`):
    uv run python scripts/build_handbook_index.py              # BM25 index only
    uv run python scripts/build_handbook_index.py --embed       # + embeddings, if Ollama/bge-m3 is reachable

The BM25 index is small (a few hundred KB of chunk text) and is committed
to the repo -- it's the "always works" retrieval path (no third-party
deps, no external service required to answer a query). The embeddings
file is a derived, environment-dependent artifact (only buildable where
Ollama + bge-m3 is installed) and is gitignored; regenerate it locally
with `--embed`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
# Standalone repo: point CS14_REPO_ROOT at a local checkout of
# kazelise/usyd-cs14-1 to (re)build the index from its docs/ + docs-site/.
# Falls back to the parent dir, which is correct when this project is
# nested inside the cs14 repo as agent/.
CS14_ROOT = Path(os.environ.get("CS14_REPO_ROOT", str(AGENT_ROOT.parent)))
sys.path.insert(0, str(AGENT_ROOT / "src"))

from survey_agent.rag.chunker import chunk_markdown_file, iter_doc_files  # noqa: E402
from survey_agent.rag.embeddings import EmbeddingUnavailableError, OllamaEmbeddingClient  # noqa: E402
from survey_agent.rag.index import DEFAULT_EMBEDDINGS_PATH, DEFAULT_INDEX_PATH  # noqa: E402

EMBED_BATCH_SIZE = 16


def build_chunks() -> list[dict]:
    chunks: list[dict] = []
    for path in iter_doc_files(CS14_ROOT):
        for chunk in chunk_markdown_file(path, CS14_ROOT):
            chunks.append(
                {
                    "id": chunk.id,
                    "source_file": chunk.source_file,
                    "heading": chunk.heading,
                    "text": chunk.text,
                    "order": chunk.order,
                }
            )
    return chunks


def write_index(chunks: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_root": "docs/*.md + docs-site/docs/**/*.md",
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_embeddings(chunks: list[dict], out_path: Path, client: OllamaEmbeddingClient) -> int:
    vectors: dict[str, list[float]] = {}
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        texts = [f"{c['heading']}\n{c['text']}" for c in batch]
        embedded = client.embed(texts)
        for c, vec in zip(batch, embedded):
            vectors[c["id"]] = [round(v, 6) for v in vec]
        print(f"  embedded {min(i + EMBED_BATCH_SIZE, len(chunks))}/{len(chunks)} chunks", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "model": client.model,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dim": len(next(iter(vectors.values()))) if vectors else 0,
        "vectors": vectors,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return len(vectors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embed", action="store_true", help="Also build the Ollama bge-m3 embedding index")
    parser.add_argument("--index-out", default=str(DEFAULT_INDEX_PATH))
    parser.add_argument("--embeddings-out", default=str(DEFAULT_EMBEDDINGS_PATH))
    args = parser.parse_args(argv)

    chunks = build_chunks()
    if not chunks:
        print(f"No markdown files found under {CS14_ROOT}/docs or {CS14_ROOT}/docs-site", file=sys.stderr)
        return 1

    index_out = Path(args.index_out)
    write_index(chunks, index_out)
    n_files = len({c["source_file"] for c in chunks})
    print(f"Wrote {len(chunks)} chunks from {n_files} files to {index_out}")

    if args.embed:
        client = OllamaEmbeddingClient()
        if not client.available():
            print(
                f"--embed requested but {client.unavailable_reason or 'Ollama/bge-m3 unavailable'}; "
                "skipping embeddings (search_handbook will run BM25-only).",
                file=sys.stderr,
            )
            return 0
        embeddings_out = Path(args.embeddings_out)
        try:
            n = build_embeddings(chunks, embeddings_out, client)
        except EmbeddingUnavailableError as exc:
            print(f"Embedding build failed: {exc}", file=sys.stderr)
            return 1
        size_mb = embeddings_out.stat().st_size / (1024 * 1024)
        print(f"Wrote {n} embedding vectors ({size_mb:.2f} MB) to {embeddings_out}")
        if size_mb > 2:
            print(f"{embeddings_out.name} is over 2MB -- confirm it's covered by .gitignore (it is, by default).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
