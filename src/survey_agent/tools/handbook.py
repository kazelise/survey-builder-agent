"""search_handbook: retrieval-augmented Q&A over the cs14 platform docs
(`docs/*.md` + `docs-site/docs/**/*.md`, ~21 pages -- see
`scripts/build_handbook_index.py`).

Two-path retrieval, both implemented in `rag/`:
1. BM25 over `data/handbook_index.json`'s chunks -- pure Python, zero
   third-party deps, always available (DESIGN.md's "no framework" pillar
   extends here too: no `rank_bm25`/`faiss`/`numpy`).
2. Ollama `bge-m3` embeddings, fused with BM25 via reciprocal rank fusion
   when a local Ollama daemon with `bge-m3` installed is reachable at
   query time; degrades to BM25-only (with a `note` in the tool result
   explaining why) otherwise. See `rag/index.py::HandbookIndex.search`.

Like `get_share_link`, this is a *local* tool: no `ctx.client` HTTP call
against the cs14 backend, no `ctx.run` mutation -- just an in-process
index lookup (plus, optionally, one loopback call to Ollama), so it's
cheap enough to call speculatively and never touches survey data.

The index/embedding client are lazy-loaded module-level singletons (built
once per process, not per call -- loading+tokenizing ~a few hundred
chunks is cheap but not free, and `OllamaEmbeddingClient.available()`
memoizes its own probe). `configure()` is the test seam: it overrides the
singletons directly instead of going through `HandbookIndex.load()`/the
real Ollama client, which is what lets tests run with a small fixture
index and a fake embedder, fully offline (see
tests/test_handbook_tool.py).
"""

from __future__ import annotations

from ..rag.embeddings import EmbeddingClient, OllamaEmbeddingClient
from ..rag.index import HandbookIndex
from .schema import ToolError, ToolSpec

DEFAULT_TOP_K = 5
MIN_TOP_K = 1
MAX_TOP_K = 20

_index: HandbookIndex | None = None
_embed_client: EmbeddingClient | None = None


def configure(index: HandbookIndex | None = None, embed_client: EmbeddingClient | None = None) -> None:
    """Test/advanced-use seam: inject a prebuilt index and/or embedding
    client instead of the lazy-loaded singletons. Passing `None` for a
    parameter leaves that singleton untouched; call `reset()` to clear
    both back to "lazy-load on next use"."""
    global _index, _embed_client
    if index is not None:
        _index = index
    if embed_client is not None:
        _embed_client = embed_client


def reset() -> None:
    """Clears both singletons back to unset, so the next call re-runs the
    normal lazy-load path. Used by tests to avoid state leaking between
    cases (see tests/test_handbook_tool.py)."""
    global _index, _embed_client
    _index = None
    _embed_client = None


def _get_index() -> HandbookIndex:
    global _index
    if _index is None:
        _index = HandbookIndex.load()
    return _index


def _get_embed_client() -> EmbeddingClient:
    global _embed_client
    if _embed_client is None:
        _embed_client = OllamaEmbeddingClient()
    return _embed_client


def search_handbook(ctx, args: dict) -> dict:
    query = args["query"].strip()
    if not query:
        raise ToolError("query must not be empty.")

    top_k = args.get("top_k", DEFAULT_TOP_K)
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = DEFAULT_TOP_K
    top_k = max(MIN_TOP_K, min(top_k, MAX_TOP_K))

    try:
        index = _get_index()
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc

    return index.search(query, top_k=top_k, embed_client=_get_embed_client())


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="search_handbook",
        description=(
            "Search the cs14 platform handbook/docs (researcher workflow, platform styles, "
            "calibration/gaze tracking, data export, deployment, API reference) for how-to or "
            "policy answers. Returns top-k passages with source_file/heading/snippet/score, "
            "citing where each answer came from. Use this for questions ABOUT the platform "
            "itself, not for reading or mutating a specific survey's data (use get_survey / "
            "list_surveys for that)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language question or keywords, en or zh."},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Default 5."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_handbook,
    ),
]
