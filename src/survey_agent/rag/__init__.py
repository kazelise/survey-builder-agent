"""Retrieval infrastructure for the handbook RAG tool (`search_handbook`).

Kept separate from `tools/` on purpose: this package knows nothing about
ToolSpec/Anthropic/MCP wire formats -- it's a small, dependency-free
retrieval library (chunking + BM25 + optional Ollama embeddings) that
`tools/handbook.py` wires into one tool. Nothing here imports `anthropic`,
`mcp`, or `httpx`'s tool-facing bits; `embeddings.py` is the only module
that touches the network (a local Ollama daemon), and every caller treats
that as optional (DESIGN.md-style graceful degradation).
"""

from __future__ import annotations
