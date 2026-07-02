"""Loads the persisted handbook chunk index and answers `search_handbook`
queries via BM25 (always) plus an optional embedding leg, fused with
reciprocal rank fusion (RRF).

Two files, two lifecycles (see scripts/build_handbook_index.py):
- `data/handbook_index.json`      -- chunks + metadata, committed to the
  repo. Cheap to rebuild from docs/*.md + docs-site/**/*.md; BM25 stats
  are derived from it in memory at load time (tokenizing a few hundred
  short chunks is microseconds -- not worth persisting inverted-index
  state).
- `data/handbook_embeddings.json` -- `{chunk_id: [floats]}`, built
  separately (only when a local Ollama + bge-m3 is reachable) and
  gitignored: it's a derived, environment-dependent artifact, not a
  source of truth, and can be multiple MB for a few hundred 1024-dim
  vectors.

RRF (not min-max score normalization) is the fusion strategy: BM25 scores
and cosine similarities live on incomparable scales, and RRF only needs
each method's *rank order* to combine them --
``score(d) = sum_over_methods(1 / (k + rank_in_method(d)))`` -- which is
simple, has no scale-matching knobs to get wrong, and is the standard
choice for 2-way lexical+semantic fusion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .bm25 import BM25Index
from .chunker import Chunk
from .embeddings import EmbeddingClient, cosine_similarity

# rag/index.py -> rag/ -> survey_agent/ -> src/ -> agent/
PACKAGE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INDEX_PATH = PACKAGE_ROOT / "data" / "handbook_index.json"
DEFAULT_EMBEDDINGS_PATH = PACKAGE_ROOT / "data" / "handbook_embeddings.json"

RRF_K = 60
CANDIDATE_MULTIPLIER = 4  # widen each leg's candidate pool before fusing/truncating to top_k
SNIPPET_MAX_CHARS = 500


@dataclass
class SearchResult:
    source_file: str
    heading: str
    snippet: str
    score: float

    def as_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "heading": self.heading,
            "snippet": self.snippet,
            "score": round(self.score, 4),
        }


class HandbookIndex:
    def __init__(self, chunks: list[Chunk], embeddings: dict[str, list[float]] | None = None):
        self.chunks = chunks
        # Index heading + body together so a query matching only heading
        # words (e.g. a doc title) still scores -- the heading itself
        # isn't stored as a separate chunk.
        self._bm25 = BM25Index([f"{c.heading}\n{c.text}" for c in chunks])
        self.embeddings = embeddings or {}

    @classmethod
    def load(
        cls,
        index_path: Path | str = DEFAULT_INDEX_PATH,
        embeddings_path: Path | str | None = DEFAULT_EMBEDDINGS_PATH,
    ) -> "HandbookIndex":
        index_path = Path(index_path)
        if not index_path.exists():
            raise FileNotFoundError(
                f"Handbook index not found at {index_path}. Build it with "
                "`uv run python scripts/build_handbook_index.py` from the agent/ directory."
            )
        data = json.loads(index_path.read_text(encoding="utf-8"))
        chunks = [
            Chunk(id=c["id"], source_file=c["source_file"], heading=c["heading"], text=c["text"], order=c["order"])
            for c in data["chunks"]
        ]

        embeddings: dict[str, list[float]] = {}
        if embeddings_path is not None:
            embeddings_path = Path(embeddings_path)
            if embeddings_path.exists():
                edata = json.loads(embeddings_path.read_text(encoding="utf-8"))
                embeddings = edata.get("vectors", {})
        return cls(chunks, embeddings)

    def _snippet(self, text: str) -> str:
        text = text.strip()
        if len(text) <= SNIPPET_MAX_CHARS:
            return text
        return text[:SNIPPET_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    def _result(self, idx: int, score: float) -> SearchResult:
        c = self.chunks[idx]
        return SearchResult(source_file=c.source_file, heading=c.heading, snippet=self._snippet(c.text), score=score)

    def search_bm25(self, query: str, top_k: int) -> list[SearchResult]:
        """BM25-only search, exposed directly for tests/tools that want to
        bypass the embedding leg entirely."""
        hits = self._bm25.search(query, top_k=top_k)
        return [self._result(idx, score) for idx, score in hits]

    def _search_embedding(self, query_vec: list[float], top_k: int) -> list[tuple[int, float]]:
        scored = []
        for idx, chunk in enumerate(self.chunks):
            vec = self.embeddings.get(chunk.id)
            if vec is None:
                continue
            scored.append((idx, cosine_similarity(query_vec, vec)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

    def search(self, query: str, top_k: int = 5, embed_client: EmbeddingClient | None = None) -> dict:
        """Runs BM25 always; adds the embedding leg (fused via RRF) when
        `embed_client` is given, a chunk-embeddings file was loaded, and
        the client reports itself available right now. Returns
        ``{"results": [...], "mode": "hybrid"|"bm25_only", "note"?: str}``
        -- `note` is only present when the run is degraded or otherwise
        worth flagging, so a healthy hybrid run doesn't carry dead weight.
        """
        candidate_k = max(top_k * CANDIDATE_MULTIPLIER, 20)
        bm25_hits = self._bm25.search(query, top_k=candidate_k)
        fused: dict[int, float] = {}
        for rank, (idx, _score) in enumerate(bm25_hits):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)

        mode = "bm25_only"
        note: str | None = None

        if not self.embeddings:
            note = (
                "No embedding index built yet (run `scripts/build_handbook_index.py --embed` "
                "with Ollama + bge-m3 running) -- BM25-only results."
            )
        elif embed_client is None:
            note = "No embedding client configured -- BM25-only results."
        elif not embed_client.available():
            reason = getattr(embed_client, "unavailable_reason", None) or "Ollama bge-m3 not reachable"
            note = f"{reason} -- degraded to BM25-only results."
        else:
            try:
                query_vec = embed_client.embed([query])[0]
                embed_hits = self._search_embedding(query_vec, top_k=candidate_k)
                for rank, (idx, _score) in enumerate(embed_hits):
                    fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
                mode = "hybrid"
            except Exception as exc:  # noqa: BLE001 - any embedding failure degrades, never crashes the tool
                note = f"Embedding search failed ({type(exc).__name__}: {exc}) -- degraded to BM25-only results."

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        results = [self._result(idx, score) for idx, score in ranked]

        payload: dict = {"results": [r.as_dict() for r in results], "mode": mode}
        if note:
            payload["note"] = note
        return payload
