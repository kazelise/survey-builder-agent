"""Ollama `bge-m3` embedding client + cosine similarity helper.

Optional semantic-search leg of `search_handbook`'s hybrid retrieval (see
rag/index.py). BM25 (rag/bm25.py) never depends on this module and always
works; this module is only exercised when a caller wants to attempt the
embedding leg, and every method fails soft -- `available()` returns
`False` and `embed()` raises the narrow `EmbeddingUnavailableError` --
so a missing/unreachable Ollama daemon degrades `search_handbook` to
BM25-only instead of crashing the tool call (DESIGN.md's failure-handling
philosophy: semantic errors get reported, never a silent hang or a
half-built tool_result).

Talks to Ollama's `/api/embed` endpoint directly over `httpx` (already a
project dependency) -- no `ollama` Python package, matching the "no
framework" pillar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import httpx

DEFAULT_OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "bge-m3"


class EmbeddingClient(Protocol):
    """Structural type both `OllamaEmbeddingClient` and test fakes satisfy
    -- rag/index.py depends on this, not the concrete Ollama client, so
    tests can inject a deterministic fake with zero network (see
    tests/test_handbook_index.py / tests/test_handbook_tool.py)."""

    def available(self) -> bool: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingUnavailableError(RuntimeError):
    """Raised by `.embed()` when Ollama can't be reached, errors, or
    returns a malformed response. Callers (rag/index.py) catch this and
    degrade to BM25-only rather than letting it bubble into the tool
    result as a hard `is_error`."""


@dataclass
class OllamaEmbeddingClient:
    base_url: str = DEFAULT_OLLAMA_URL
    model: str = EMBED_MODEL
    probe_timeout: float = 1.5
    embed_timeout: float = 30.0

    _available_cache: bool | None = field(default=None, init=False, repr=False)
    _unavailable_reason: str | None = field(default=None, init=False, repr=False)

    def available(self, *, force_recheck: bool = False) -> bool:
        """Probe `/api/tags` once per process (cheap memoization) unless
        `force_recheck` -- a `search_handbook` call may happen many times
        in one agent run, and re-probing Ollama on every call would add
        latency for no benefit within a single run's lifetime."""
        if self._available_cache is not None and not force_recheck:
            return self._available_cache

        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=self.probe_timeout)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception as exc:  # noqa: BLE001 - any failure just means "not available"
            self._unavailable_reason = f"Ollama unreachable at {self.base_url} ({type(exc).__name__})"
            self._available_cache = False
            return False

        # Ollama tag names look like "bge-m3:latest" -- compare the part
        # before the colon so any installed tag of the model counts.
        if not any(name.split(":")[0] == self.model for name in models):
            self._unavailable_reason = (
                f"Ollama is running but model {self.model!r} is not installed (`ollama pull {self.model}`)"
            )
            self._available_cache = False
            return False

        self._unavailable_reason = None
        self._available_cache = True
        return True

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def embed(self, texts: list[str]) -> list[list[float]]:
        """POST /api/embed with `{"model": ..., "input": [...]}`. Raises
        `EmbeddingUnavailableError` on any transport/HTTP/shape problem so
        callers never have to guess what went wrong."""
        try:
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
                timeout=self.embed_timeout,
            )
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
        except Exception as exc:  # noqa: BLE001 - normalize every failure mode
            raise EmbeddingUnavailableError(str(exc)) from exc

        if len(embeddings) != len(texts):
            raise EmbeddingUnavailableError(f"expected {len(texts)} embeddings, got {len(embeddings)}")
        return embeddings


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
