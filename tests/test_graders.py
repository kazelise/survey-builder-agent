"""Tests for the eval harness's grading logic (evals/graders.py and
evals/runner.py's CaseResult.passed) -- pure functions over plain data, no
network/model, so they're directly unit-testable (see graders.py's module
docstring)."""

from __future__ import annotations

from evals.graders import sequence_match, terminal_state_assert
from evals.runner import SEQUENCE_PASS_THRESHOLD, CaseResult


def _terminal_ok() -> object:
    return terminal_state_assert(
        {}, state={}, final_text=None, reason="done", tool_calls=[], error_tool_calls=[], question_types=[]
    )


def test_sequence_match_scores_lcs_ratio_and_reports_missing():
    seq = sequence_match(["create_survey", "add_post"], ["create_survey", "add_post", "publish_survey"])
    assert seq.score == 2 / 3
    assert seq.missing == ["publish_survey"]


def test_sequence_match_empty_expected_is_trivially_satisfied():
    seq = sequence_match(["get_survey"], [])
    assert seq.score == 1.0
    assert seq.missing == []


def test_case_result_fails_when_a_mandatory_mutation_is_entirely_missing_even_above_ratio_threshold():
    # Regression (audit finding, high severity): dropping exactly one call
    # from a >=5-item expected_sequence yields a (n-1)/n LCS ratio that is
    # >= SEQUENCE_PASS_THRESHOLD (0.8) for any n>=5, so the ratio check
    # alone rated this a PASS even though a mandatory mutating tool was
    # never called at all -- directly contradicting runner.py's own
    # documented invariant ("any missing mandatory mutation still fails it
    # outright"). This mirrors the concrete repro from the audit: dropping
    # update_post_display from a group_overrides-style case.
    expected = ["create_survey", "add_post", "update_post_display", "add_survey_question", "publish_survey"]
    actual_missing_one = ["create_survey", "add_post", "add_survey_question", "publish_survey"]  # no update_post_display

    seq = sequence_match(actual_missing_one, expected)
    assert seq.score == 0.8  # == SEQUENCE_PASS_THRESHOLD: the old bug's exact boundary
    assert seq.missing == ["update_post_display"]

    result = CaseResult(
        name="repro_missing_mutation",
        category="multi_step",
        lang="zh",
        seq=seq,
        terminal=_terminal_ok(),
        reason="done",
        turns=4,
        tool_calls=actual_missing_one,
    )
    assert result.passed is False  # must fail: a mandated mutation never ran


def test_case_result_fails_when_a_mutation_runs_fewer_times_than_required():
    # Same class of bug: add_comment expected twice, only called once.
    # Counter-based `missing` catches under-counts, not just full absence
    # (mirrors the audit's second repro: one add_comment instead of two).
    expected = ["create_survey", "add_post", "add_comment", "add_comment", "add_post_question", "publish_survey"]
    actual_one_comment = ["create_survey", "add_post", "add_comment", "add_post_question", "publish_survey"]

    seq = sequence_match(actual_one_comment, expected)
    assert seq.score >= SEQUENCE_PASS_THRESHOLD
    assert seq.missing == ["add_comment"]

    result = CaseResult(
        name="repro_undercount",
        category="multi_step",
        lang="zh",
        seq=seq,
        terminal=_terminal_ok(),
        reason="done",
        turns=4,
        tool_calls=actual_one_comment,
    )
    assert result.passed is False


def test_case_result_still_passes_on_a_harmless_extra_read_only_detour():
    # Sanity check the fix doesn't over-correct: an extra read-only call
    # (no missing mutation) must still pass, per sequence_match's own
    # design goal ("extra read-only calls never hurt the score").
    expected = ["create_survey", "add_post", "publish_survey"]
    actual_with_extra_read = ["create_survey", "get_survey", "add_post", "publish_survey"]

    seq = sequence_match(actual_with_extra_read, expected)
    assert seq.score == 1.0
    assert seq.missing == []

    result = CaseResult(
        name="repro_extra_read",
        category="multi_step",
        lang="en",
        seq=seq,
        terminal=_terminal_ok(),
        reason="done",
        turns=4,
        tool_calls=actual_with_extra_read,
    )
    assert result.passed is True
