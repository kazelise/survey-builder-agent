"""Eval runner (DESIGN.md §12): loads every case in ``evals/cases/``, drives
``survey_agent.loop.run`` under either ``MockModel`` (default, deterministic,
zero API key / zero network — CI-safe) or ``RealModel`` (``--real``, needs
``ANTHROPIC_API_KEY``), grades each run with ``graders.py``, and prints/writes
a scorecard.

Usage (from ``agent/``):
    uv run python -m evals.runner                 # mock, all cases
    uv run python -m evals.runner --real           # live model, all cases
    uv run python -m evals.runner --case 07_*      # glob filter on filename
    uv run python -m evals.runner --report PATH.md # markdown report path
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from survey_agent.config import Settings
from survey_agent.context import HandlerContext, RunContext
from survey_agent.executor import ToolExecutor
from survey_agent.http_client import CS14Client
from survey_agent.loop import run as run_loop
from survey_agent.model import MockModel, ModelUnavailableError, RealModel
from survey_agent.prompts import full_system_prompt
from survey_agent.tools import TOOLS
from survey_agent.tools.schema import anthropic_tools
from survey_agent.trace import Tracer

from .graders import SequenceScore, TerminalResult, sequence_match, terminal_state_assert
from .script_builder import build_mock_script

CASES_DIR = Path(__file__).parent / "cases"

# A case "passes" if both layers pass. The sequence layer uses a threshold
# (not exact ==1.0) so a case is allowed to tolerate the model taking one
# extra recovery detour without being graded a hard fail — but any missing
# mandatory mutation still fails it outright (see graders.sequence_match).
SEQUENCE_PASS_THRESHOLD = 0.8


@dataclass
class CaseResult:
    name: str
    category: str
    lang: str
    seq: SequenceScore
    terminal: TerminalResult
    reason: str
    turns: int
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None  # set if the run itself blew up (not a grading failure)

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        return self.seq.score >= SEQUENCE_PASS_THRESHOLD and self.terminal.passed


def load_cases(pattern: str | None = None) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        if pattern and not fnmatch(path.stem, pattern) and not fnmatch(path.name, pattern):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        cases.append(data)
    return cases


def run_case(case: dict, *, real: bool, settings: Settings) -> CaseResult:
    client = CS14Client(base_url="http://unused.invalid", dry_run=True)
    run_ctx = RunContext()
    ctx = HandlerContext(client=client, run=run_ctx)
    executor = ToolExecutor(TOOLS, ctx, result_max_chars=settings.tool_result_max_chars)
    tracer = Tracer(None)  # exercise the trace pipeline without writing a file per case

    tool_calls: list[str] = []
    error_tool_calls: list[str] = []
    question_types: list[str] = []

    def on_event(kind: str, payload: dict) -> None:
        if kind != "tool_call":
            return
        name = payload["name"]
        tool_calls.append(name)
        if payload["is_error"]:
            error_tool_calls.append(name)
            return
        if name in ("add_post_question", "add_survey_question"):
            try:
                result = json.loads(payload["result"])
            except (json.JSONDecodeError, TypeError):
                result = {}
            qtype = result.get("question_type")
            if qtype:
                question_types.append(qtype)

    if real:
        model: object = RealModel(settings.model, settings.anthropic_api_key, settings.anthropic_base_url, settings.max_tokens)
        case_settings = Settings(max_turns=case.get("max_turns", settings.max_turns))
    else:
        model = MockModel(build_mock_script(case["mock_script"]))
        case_settings = Settings(max_turns=case.get("max_turns", 20))

    try:
        result = run_loop(
            case["instruction"],
            full_system_prompt(),
            model,
            anthropic_tools(TOOLS),
            executor,
            ctx,
            case_settings,
            tracer,
            on_event=on_event,
        )
    except ModelUnavailableError as exc:
        return CaseResult(
            name=case["name"],
            category=case.get("category", "unknown"),
            lang=case.get("lang", "?"),
            seq=SequenceScore(score=0.0, lcs_len=0, expected_len=len(case.get("expected_sequence", []))),
            terminal=TerminalResult(passed=False, failures=[f"model unavailable: {exc}"]),
            reason="model_unavailable",
            turns=0,
            error=str(exc),
        )
    finally:
        client.close()

    expected_sequence = case.get("expected_sequence", [])
    terminal_assertions = case.get("terminal_assert", {})
    if real and "expect_error_from" in terminal_assertions:
        # Recovery cases script a deliberately-bad first call in mock mode.
        # A real model that passes valid arguments on the first try has met
        # the user goal — the recovery path itself is proven by the mock
        # replay — so drop the forced-error expectation and collapse the
        # scripted fail-then-retry duplicate in the expected sequence.
        terminal_assertions = {k: v for k, v in terminal_assertions.items() if k != "expect_error_from"}
        expected_sequence = [t for i, t in enumerate(expected_sequence) if i == 0 or t != expected_sequence[i - 1]]

    seq = sequence_match(tool_calls, expected_sequence)
    terminal = terminal_state_assert(
        terminal_assertions,
        state=ctx.run.as_state(),
        final_text=result.final_text,
        reason=result.reason,
        tool_calls=tool_calls,
        error_tool_calls=error_tool_calls,
        question_types=question_types,
    )
    return CaseResult(
        name=case["name"],
        category=case.get("category", "unknown"),
        lang=case.get("lang", "?"),
        seq=seq,
        terminal=terminal,
        reason=result.reason,
        turns=result.turns,
        tool_calls=tool_calls,
    )


def run_all(cases: list[dict], *, real: bool) -> list[CaseResult]:
    settings = Settings.from_env()
    results = []
    for case in cases:
        results.append(run_case(case, real=real, settings=settings))
    return results


# ── scorecard / report ─────────────────────────────────────────────────
def print_scorecard(results: list[CaseResult]) -> None:
    header = f"{'case':<32} {'cat':<16} {'lang':<6} {'seq':>5} {'term':>5} {'turns':>5} {'pass':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.name:<32} {r.category:<16} {r.lang:<6} {r.seq.score:>5.2f} "
            f"{'ok' if r.terminal.passed else 'FAIL':>5} {r.turns:>5} {'PASS' if r.passed else 'FAIL':>5}"
        )
        if not r.passed:
            for f in r.terminal.failures:
                print(f"      terminal: {f}")
            if r.seq.missing:
                print(f"      seq: missing {r.seq.missing} (score {r.seq.score:.2f})")
            if r.error:
                print(f"      error: {r.error}")

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print("-" * len(header))
    print(f"TOTAL: {passed}/{total} passed ({passed / total * 100:.0f}%)" if total else "TOTAL: no cases")


def _breakdown(results: list[CaseResult], key) -> dict[str, tuple[int, int]]:
    buckets: dict[str, list[CaseResult]] = {}
    for r in results:
        buckets.setdefault(key(r), []).append(r)
    return {k: (sum(1 for r in v if r.passed), len(v)) for k, v in buckets.items()}


def write_markdown_report(results: list[CaseResult], path: Path, *, mode: str) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    avg_seq = sum(r.seq.score for r in results) / total if total else 0.0
    avg_turns = sum(r.turns for r in results) / total if total else 0.0

    by_category = _breakdown(results, lambda r: r.category)
    by_lang = _breakdown(results, lambda r: r.lang)

    lines = [
        "# Survey Builder Agent — Eval Report",
        "",
        f"- Mode: `{mode}`",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Cases: {total}",
        f"- Pass rate: **{passed}/{total} ({passed / total * 100:.0f}%)**" if total else "- Pass rate: n/a (no cases)",
        f"- Avg sequence score: {avg_seq:.2f}",
        f"- Avg turns: {avg_turns:.1f}",
        "",
        "## By category",
        "",
        "| category | pass | total | rate |",
        "|---|---:|---:|---:|",
    ]
    for cat, (p, t) in sorted(by_category.items()):
        lines.append(f"| {cat} | {p} | {t} | {p / t * 100:.0f}% |")

    lines += ["", "## By language mix", "", "| lang | pass | total | rate |", "|---|---:|---:|---:|"]
    for lang, (p, t) in sorted(by_lang.items()):
        lines.append(f"| {lang} | {p} | {t} | {p / t * 100:.0f}% |")

    lines += [
        "",
        "## Cases",
        "",
        "| case | category | lang | seq score | terminal | turns | result |",
        "|---|---|---|---:|---|---:|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.category} | {r.lang} | {r.seq.score:.2f} | "
            f"{'ok' if r.terminal.passed else 'FAIL'} | {r.turns} | {'PASS' if r.passed else 'FAIL'} |"
        )

    failing = [r for r in results if not r.passed]
    if failing:
        lines += ["", "## Failure detail", ""]
        for r in failing:
            lines.append(f"### {r.name}")
            if r.error:
                lines.append(f"- run error: {r.error}")
            if r.seq.missing:
                lines.append(f"- sequence missing: {r.seq.missing} (score {r.seq.score:.2f})")
            for f in r.terminal.failures:
                lines.append(f"- terminal: {f}")
            lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Survey Builder Agent eval suite.")
    parser.add_argument("--real", action="store_true", help="Use the live model instead of MockModel (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--case", default=None, help="Glob filter on case filename/stem, e.g. '01_*'")
    parser.add_argument("--report", default=str(Path(__file__).parent / "REPORT.md"), help="Markdown report output path")
    args = parser.parse_args(argv)

    cases = load_cases(args.case)
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    if args.real:
        settings = Settings.from_env()
        if not settings.anthropic_api_key:
            print("--real requires ANTHROPIC_API_KEY (or `ant auth login`); falling back is not automatic here.", file=sys.stderr)
            return 2

    results = run_all(cases, real=args.real)
    print_scorecard(results)
    report_path = Path(args.report)
    write_markdown_report(results, report_path, mode="real" if args.real else "mock")
    print(f"\nMarkdown report written to {report_path}")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
