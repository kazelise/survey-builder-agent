"""Tests for the handbook markdown chunker (src/survey_agent/rag/chunker.py)."""

from __future__ import annotations

from survey_agent.rag.chunker import DEFAULT_MAX_CHARS, chunk_markdown_text, strip_frontmatter


def test_pack_never_exceeds_max_chars_even_with_near_max_size_paragraphs():
    # Regression: after closing a chunk (chunks.append(current)), the next
    # chunk used to be seeded as `tail + joiner + unit` with NO re-check
    # against max_chars -- only the *next* iteration's candidate got
    # checked. When the seeding unit is itself close to max_chars, tail (up
    # to overlap_chars) + joiner + unit silently overshoots the budget by
    # ~overlap_chars, and not just once: every chunk after the first
    # overflow does it, because the oversized `current` then always fails
    # the next candidate check and gets flushed as-is. Reproduces the audit
    # finding's repro shape: several ~850-char paragraphs against the
    # default max_chars=900/overlap_chars=150.
    paragraphs = [f"Paragraph {i}. " + ("x" * 835) for i in range(4)]  # ~850 chars each
    text = "# Title\n\n" + "\n\n".join(paragraphs)

    chunks = chunk_markdown_text(text, "docs/big.md")

    assert len(chunks) > 1  # actually got split into multiple chunks
    for c in chunks:
        assert len(c.text) <= DEFAULT_MAX_CHARS, f"chunk {c.order} is {len(c.text)} chars, over max_chars={DEFAULT_MAX_CHARS}"


def test_pack_still_carries_overlap_when_it_fits():
    # Sanity: the fix must not just drop overlap entirely -- when a tail
    # comfortably fits under the budget alongside the next unit, it should
    # still be carried into the next chunk (the whole point of
    # overlap_chars). Each paragraph here is individually well under
    # max_chars, so this exercises the seed-with-overlap path specifically
    # (not the separate individually-oversized-unit/split_oversized path).
    paragraphs = ["A" * 150, "B" * 150, "C" * 50]
    text = "# T\n\n" + "\n\n".join(paragraphs)

    chunks = chunk_markdown_text(text, "docs/small.md", max_chars=200, overlap_chars=30)

    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= 200
    assert chunks[0].text[-10:] in chunks[1].text


def test_strip_frontmatter_removes_a_real_yaml_frontmatter_block():
    text = "---\nlayout: home\nhero:\n  name: cs14\n---\n\n# Welcome\n\nBody text.\n"
    stripped = strip_frontmatter(text)
    assert stripped == "\n# Welcome\n\nBody text.\n"


def test_strip_frontmatter_does_not_eat_content_after_a_leading_horizontal_rule():
    # Regression: a doc that opens with a Markdown horizontal-rule divider
    # ('---' as the literal first line -- a common section-break
    # convention) followed *later* by a second bare '---' divider used to
    # have its ENTIRE first section (heading included) silently deleted,
    # because the old check only looked for the next literal '---' line
    # with no bound and no awareness that a heading had already started.
    text = (
        "---\n"
        "# Real Heading\n\n"
        "Some real content here that must not be deleted.\n\n"
        "More content.\n\n"
        "---\n\n"
        "Section after the second divider.\n"
    )
    stripped = strip_frontmatter(text)
    assert "Real Heading" in stripped
    assert "must not be deleted" in stripped
    assert stripped == text  # not frontmatter at all -- left completely as-is


def test_strip_frontmatter_leaves_unterminated_frontmatter_alone():
    text = "---\nno closing delimiter anywhere in this short file\n"
    assert strip_frontmatter(text) == text


def test_strip_frontmatter_ignores_content_with_no_leading_delimiter():
    text = "# Just a normal doc\n\nNo frontmatter here.\n"
    assert strip_frontmatter(text) == text
