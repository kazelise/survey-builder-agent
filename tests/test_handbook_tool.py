"""Offline tests for the search_handbook tool (the seam promised in
tools/handbook.py's docstring): a 3-chunk fixture index plus fake embedding
clients exercise BM25-only vs hybrid modes, the degradation notes, top_k
clamping, and the configure()/reset() singleton seam — no data/ files, no
Ollama, no network."""

from __future__ import annotations

import pytest

from survey_agent.rag.chunker import Chunk
from survey_agent.rag.index import HandbookIndex
from survey_agent.tools import handbook
from survey_agent.tools.schema import ToolError


def _fixture_chunks() -> list[Chunk]:
    return [
        Chunk(
            id="c-export",
            source_file="docs/EXPORT.md",
            heading="Exporting participant data",
            text=(
                "Researchers can export participant responses as CSV or JSON from the "
                "analytics page, filtered by group, language and completion status."
            ),
            order=0,
        ),
        Chunk(
            id="c-calibration",
            source_file="docs/CALIBRATION.md",
            heading="Webcam calibration",
            text=(
                "Optional webcam calibration produces numeric attention quality signals. "
                "Raw webcam video is never persisted or uploaded."
            ),
            order=1,
        ),
        Chunk(
            id="c-groups",
            source_file="docs/GROUPS.md",
            heading="A/B experiment groups",
            text=(
                "Participants are assigned to experiment groups on entry and keep the "
                "same group across tab closes and resumes."
            ),
            order=2,
        ),
    ]


class FakeEmbedder:
    """Duck-typed EmbeddingClient: maps exact texts to fixed vectors."""

    model = "fake-embed"
    unavailable_reason: str | None = None

    def __init__(self, mapping: dict[str, list[float]] | None = None, is_available: bool = True):
        self.mapping = mapping or {}
        self._available = is_available
        if not is_available:
            self.unavailable_reason = "fake daemon not running"

    def available(self) -> bool:
        return self._available

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.mapping.get(t, [1.0, 0.0]) for t in texts]


@pytest.fixture(autouse=True)
def _reset_singletons():
    yield
    handbook.reset()


def test_bm25_only_when_no_embedding_index_built():
    handbook.configure(index=HandbookIndex(_fixture_chunks()), embed_client=FakeEmbedder())
    payload = handbook.search_handbook(None, {"query": "export participant data as CSV"})
    assert payload["mode"] == "bm25_only"
    assert "No embedding index built" in payload["note"]
    top = payload["results"][0]
    assert top["source_file"] == "docs/EXPORT.md"
    assert top["heading"] == "Exporting participant data"


def test_hybrid_mode_fuses_embedding_leg():
    chunks = _fixture_chunks()
    embeddings = {
        "c-export": [1.0, 0.0],
        "c-calibration": [0.0, 1.0],
        "c-groups": [0.7, 0.7],
    }
    query = "does the platform store raw webcam video?"
    client = FakeEmbedder(mapping={query: [0.0, 1.0]})
    handbook.configure(index=HandbookIndex(chunks, embeddings=embeddings), embed_client=client)
    payload = handbook.search_handbook(None, {"query": query, "top_k": 2})
    assert payload["mode"] == "hybrid"
    assert "note" not in payload  # healthy hybrid runs carry no dead weight
    top_sources = [r["source_file"] for r in payload["results"]]
    assert "docs/CALIBRATION.md" in top_sources


def test_degrades_to_bm25_when_client_unavailable():
    handbook.configure(
        index=HandbookIndex(_fixture_chunks(), embeddings={"c-export": [1.0, 0.0]}),
        embed_client=FakeEmbedder(is_available=False),
    )
    payload = handbook.search_handbook(None, {"query": "experiment groups"})
    assert payload["mode"] == "bm25_only"
    assert "degraded" in payload["note"]


def test_empty_query_raises_tool_error():
    handbook.configure(index=HandbookIndex(_fixture_chunks()), embed_client=FakeEmbedder())
    with pytest.raises(ToolError):
        handbook.search_handbook(None, {"query": "   "})


def test_top_k_clamped_and_defaulted():
    handbook.configure(index=HandbookIndex(_fixture_chunks()), embed_client=FakeEmbedder())
    huge = handbook.search_handbook(None, {"query": "groups", "top_k": 999})
    assert len(huge["results"]) <= len(_fixture_chunks())
    bad = handbook.search_handbook(None, {"query": "groups", "top_k": "not-a-number"})
    assert 1 <= len(bad["results"]) <= handbook.DEFAULT_TOP_K
