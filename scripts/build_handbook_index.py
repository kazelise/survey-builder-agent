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
import re
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

# Secret/PII guard: data/handbook_index.json is a deliberately COMMITTED,
# public build artifact (README.md documents it as "入库"), so any
# credential-shaped text in the source docs must never reach it -- this is
# not hypothetical: a real staging password + a real server IP were once
# committed to this repo via this exact ingestion path (docs/DEMO_RUNBOOK.md
# / docs/deployment.md in the cs14 platform repo this index is built from).
# Best-effort, not a full secret scanner: it catches the two shapes that
# actually leaked (credential-labeled markdown table cells, raw IPv4
# literals) as a second line of defense on top of "real docs shouldn't put
# live secrets in markdown in the first place."
_SECRET_LABEL_RE = re.compile(
    r"(\|[^|\n]*(?:password|token|secret|api[_ ]?key|access[_ ]?key|email)[^|\n]*\|\s*)`[^`]+`",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
# Loopback/private/unspecified ranges are never a real server's identity —
# leave them un-redacted so ordinary "run it on 127.0.0.1" doc examples stay
# readable.
_SAFE_IP_PREFIXES = ("127.", "10.", "0.0.0.0", "192.168.")


def _is_safe_ip(ip: str) -> bool:
    if ip.startswith(_SAFE_IP_PREFIXES):
        return True
    if ip.startswith("172."):
        second = int(ip.split(".")[1])
        return 16 <= second <= 31
    return False


def redact_secrets(text: str) -> str:
    """Strip credential-shaped table values and raw public IPv4 literals out
    of chunk text before it's written to the committed index or fed to the
    embedding model."""
    text = _SECRET_LABEL_RE.sub(lambda m: f"{m.group(1)}`<REDACTED>`", text)
    text = _IPV4_RE.sub(lambda m: m.group(0) if _is_safe_ip(m.group(0)) else "<REDACTED-ip>", text)
    return text


def build_chunks() -> list[dict]:
    chunks: list[dict] = []
    for path in iter_doc_files(CS14_ROOT):
        for chunk in chunk_markdown_file(path, CS14_ROOT):
            chunks.append(
                {
                    "id": chunk.id,
                    "source_file": chunk.source_file,
                    "heading": chunk.heading,
                    "text": redact_secrets(chunk.text),
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
