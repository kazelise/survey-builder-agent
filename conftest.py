"""Repo-root pytest conftest.

`tests/` has no `__init__.py`, so pytest's default "prepend" import mode
only puts `tests/` itself on `sys.path`, not the repo root — which means
`evals` (a sibling top-level package used by `evals/runner.py`,
`evals/graders.py`) isn't importable from test modules by default. Make it
importable so `tests/test_graders.py` can unit-test the eval harness's
pure grading functions directly, the same way `evals/runner.py` already
imports `survey_agent.*` via `src/` being installed editable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
