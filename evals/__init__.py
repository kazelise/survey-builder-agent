"""Evaluation suite for the Survey Builder Agent (DESIGN.md §12).

Kept as a plain sibling package to ``src/survey_agent`` (not inside ``src/``)
so it can import the installed ``survey_agent`` package like any other
consumer, while still being run directly via ``uv run python -m
evals.runner`` from the ``agent/`` directory.
"""
