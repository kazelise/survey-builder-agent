"""Markdown chunker for the handbook RAG index (`search_handbook`).

Splits each doc by heading hierarchy (H1..H6) into sections, then further
splits any section whose body exceeds ``max_chars`` into overlapping
sub-chunks on paragraph boundaries. Overlap carries the tail of one chunk
into the head of the next so a fact split across a chunk boundary is still
visible to whichever chunk the retriever happens to pick.

Deliberately not a general-purpose Markdown parser: fenced code blocks are
tracked only so a ``#`` inside one isn't mistaken for a heading; nothing
else about Markdown syntax (lists, tables, links) is interpreted specially
-- chunk text is the raw source slice, headings become the citation
("breadcrumb") string returned alongside each search result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
FENCE_RE = re.compile(r"^(```|~~~)")
FRONTMATTER_DELIM = "---"

DEFAULT_MAX_CHARS = 900
DEFAULT_OVERLAP_CHARS = 150
# Generous bound for a real YAML frontmatter block (VitePress hero configs
# are a handful of lines) -- used by strip_frontmatter to avoid scanning
# arbitrarily far into a document that merely *opens* with a horizontal
# rule.
FRONTMATTER_MAX_LINES = 40


@dataclass(frozen=True)
class Chunk:
    id: str
    source_file: str
    heading: str
    text: str
    order: int


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block (```--- ... ---```), used by
    the VitePress docs-site pages (e.g. docs-site/docs/index.md's `layout:
    home` hero block) -- it's site config, not handbook content worth
    indexing.

    A document that merely *opens* with a Markdown horizontal-rule divider
    ('---' as the literal first line -- a common section-break convention)
    is not frontmatter, even though it also starts with '---'. Guard
    against mistaking one for the other -- and silently deleting the
    entire first section up to some later, unrelated '---' divider -- by
    only treating the block as frontmatter when a closing '---' appears
    within FRONTMATTER_MAX_LINES AND before any heading: real frontmatter
    is a short, flat key/value block that never contains a heading."""
    if not text.startswith(FRONTMATTER_DELIM):
        return text
    lines = text.splitlines(keepends=True)
    limit = min(len(lines), FRONTMATTER_MAX_LINES)
    for i in range(1, limit):
        stripped = lines[i].rstrip("\n")
        if HEADING_RE.match(stripped):
            break  # hit a heading before any closing delimiter -- not frontmatter
        if stripped == FRONTMATTER_DELIM:
            return "".join(lines[i + 1 :])
    return text  # no bounded, heading-free closing delimiter found -- leave as-is


@dataclass
class _Section:
    breadcrumb: str
    lines: list[str]


def _iter_sections(text: str) -> list[_Section]:
    """Walk the file top to bottom, tracking a heading-level stack, and
    bucket body lines under the breadcrumb ("H1 > H2 > H3") active at that
    point. A new heading at any level always closes the previous section."""
    stack: list[tuple[int, str]] = []
    sections: list[_Section] = []
    current = _Section(breadcrumb="", lines=[])
    in_fence = False

    def flush() -> None:
        if current.lines and any(line.strip() for line in current.lines):
            sections.append(_Section(current.breadcrumb, current.lines))

    for raw_line in text.splitlines():
        if FENCE_RE.match(raw_line.strip()):
            in_fence = not in_fence
            current.lines.append(raw_line)
            continue
        if not in_fence:
            m = HEADING_RE.match(raw_line)
            if m:
                flush()
                level = len(m.group(1))
                title = m.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                current = _Section(breadcrumb=" > ".join(t for _, t in stack), lines=[])
                continue
        current.lines.append(raw_line)
    flush()
    return sections


def _split_paragraphs(text: str) -> list[str]:
    paras = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in paras if p.strip()]


def _hard_split(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Character-level split with overlap -- the last-resort fallback when
    a single line is bigger than max_chars on its own (e.g. one very long
    table row or an unbroken code line) and there's no smaller natural
    boundary left to defer to."""
    step = max(max_chars - overlap_chars, 1)
    out = []
    for start in range(0, len(text), step):
        out.append(text[start : start + max_chars])
        if start + max_chars >= len(text):
            break
    return out


def _seed_next_chunk(prev_chunk: str, unit: str, overlap_chars: int, joiner: str, max_chars: int) -> str:
    """Seed the next chunk with up to `overlap_chars` of `prev_chunk`'s
    tail plus `unit`, shrinking the tail (never `unit` itself -- the
    caller guarantees `len(unit) <= max_chars`) until the seed actually
    fits the budget. A fixed-length tail is not safe to prepend
    unconditionally: when `unit` is itself close to max_chars, tail +
    joiner + unit silently overshoots by ~overlap_chars (see
    tests/test_chunker.py)."""
    tail_len = overlap_chars
    while tail_len > 0:
        tail = prev_chunk[-tail_len:]
        candidate = f"{tail}{joiner}{unit}"
        if len(candidate) <= max_chars:
            return candidate
        tail_len -= 1
    return unit  # shrunk to no tail at all; `unit` alone is always <= max_chars


def _pack(units: list[str], max_chars: int, overlap_chars: int, joiner: str, split_oversized) -> list[str]:
    """Generic greedy packer shared by both granularities below: accumulate
    `units` (paragraphs, or -- one level down -- lines) joined by `joiner`
    while under max_chars; when the next unit would overflow the current
    chunk, close it and seed the next one with the trailing
    `overlap_chars` of the just-closed chunk (shrunk if needed to still
    fit -- see _seed_next_chunk). A unit that's too big on its own is
    flushed and handed to `split_oversized` for a finer-grained split,
    independent of packing position -- checking this first avoids ever
    building an unbounded chunk."""
    if not units:
        return []

    chunks: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(split_oversized(unit, max_chars, overlap_chars))
            continue

        candidate = f"{current}{joiner}{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue

        chunks.append(current)
        current = _seed_next_chunk(current, unit, overlap_chars, joiner, max_chars)

    if current:
        chunks.append(current)
    return chunks


def _split_oversized_paragraph(paragraph: str, max_chars: int, overlap_chars: int) -> list[str]:
    """A paragraph bigger than max_chars is usually a table or a list
    (many single-newline-separated lines, no blank line between them) --
    pack at line granularity before giving up and hard-splitting mid-line,
    so a table split across chunks breaks between rows, not through one."""
    lines = paragraph.split("\n")
    if len(lines) > 1:
        return _pack(lines, max_chars, overlap_chars, "\n", lambda line, mc, oc: _hard_split(line, mc, oc))
    return _hard_split(paragraph, max_chars, overlap_chars)


def _pack_paragraphs(paragraphs: list[str], max_chars: int, overlap_chars: int) -> list[str]:
    return _pack(paragraphs, max_chars, overlap_chars, "\n\n", _split_oversized_paragraph)


def chunk_markdown_text(
    text: str,
    source_file: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Chunk one markdown document's raw text. `source_file` is the
    citation path stored on every chunk (e.g. "docs/architecture.md")."""
    text = strip_frontmatter(text)
    order = 0
    chunks: list[Chunk] = []
    for section in _iter_sections(text):
        body = "\n".join(section.lines).strip()
        if not body:
            continue
        paragraphs = _split_paragraphs(body)
        for piece in _pack_paragraphs(paragraphs, max_chars, overlap_chars):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(
                Chunk(
                    id=f"{source_file}::{order}",
                    source_file=source_file,
                    heading=section.breadcrumb or Path(source_file).stem,
                    text=piece,
                    order=order,
                )
            )
            order += 1
    return chunks


def chunk_markdown_file(
    path: Path,
    root: Path,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Like `chunk_markdown_text`, but reads `path` and derives
    `source_file` as its POSIX-style path relative to `root` (so the index
    is portable across machines instead of embedding an absolute path)."""
    text = path.read_text(encoding="utf-8")
    source_file = path.resolve().relative_to(root.resolve()).as_posix()
    return chunk_markdown_text(text, source_file, max_chars=max_chars, overlap_chars=overlap_chars)


def iter_doc_files(cs14_root: Path) -> list[Path]:
    """The handbook corpus: docs/*.md (flat) + docs-site/**/*.md (nested),
    ~21 pages at the time this was written. Sorted for a deterministic
    chunk order/id assignment across rebuilds (stable diffs when only one
    doc changes)."""
    docs = sorted((cs14_root / "docs").glob("*.md"))
    docs_site = sorted((cs14_root / "docs-site").rglob("*.md"))
    return [*docs, *docs_site]
