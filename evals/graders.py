"""Two-layer grader (DESIGN.md §12):

1. ``sequence_match`` — order-aware similarity between the tool names the
   run actually called and the case's ``expected_sequence``. Uses a
   longest-common-subsequence ratio so extra read-only calls
   (``get_survey``/``list_surveys``/``list_posts``) never hurt the score,
   but a missing or out-of-order mutation does.
2. ``terminal_state_assert`` — checks the run's final state (survey status/
   languages/groups/post & question counts, final text, error recovery)
   against the case's ``terminal_assert`` block.

Both graders are pure functions over plain data (no network, no model) so
they're trivially unit-testable and identical whether the run underneath was
mock or real.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SequenceScore:
    score: float  # 0..1
    lcs_len: int
    expected_len: int
    missing: list[str] = field(default_factory=list)
    in_order: bool = True


def sequence_match(actual: list[str], expected: list[str]) -> SequenceScore:
    if not expected:
        # Nothing mandated (e.g. a should-refuse case): pass iff the run
        # also called nothing mutating-shaped — callers layer that check via
        # terminal_assert's no_mutating_calls; here an empty expectation is
        # trivially satisfied so it doesn't drag the sequence score down.
        return SequenceScore(score=1.0, lcs_len=0, expected_len=0)

    lcs_len = _lcs_length(actual, expected)
    missing = list((Counter(expected) - Counter(actual)).elements())
    score = lcs_len / len(expected)
    return SequenceScore(score=score, lcs_len=lcs_len, expected_len=len(expected), missing=missing, in_order=score == 1.0)


def _lcs_length(a: list[str], b: list[str]) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[len(a)][len(b)]


MUTATING_TOOLS = frozenset(
    {
        "create_survey",
        "update_survey",
        "add_post",
        "update_post_display",
        "add_comment",
        "add_post_question",
        "add_survey_question",
        "publish_survey",
    }
)


@dataclass
class TerminalResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def terminal_state_assert(
    assertions: dict,
    *,
    state: dict,
    final_text: str | None,
    reason: str,
    tool_calls: list[str],
    error_tool_calls: list[str],
    question_types: list[str],
) -> TerminalResult:
    """``state`` is ``RunContext.as_state()``; ``tool_calls``/``error_tool_calls``
    are tool names from every executed call (all + error-only respectively);
    ``question_types`` accumulates every question_type successfully created.
    """
    failures: list[str] = []
    text = final_text or ""
    lowered = text.lower()

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    if "reason" in assertions:
        check(reason == assertions["reason"], f"reason={reason!r}, expected {assertions['reason']!r}")

    if "status" in assertions:
        check(state.get("status") == assertions["status"], f"status={state.get('status')!r}, expected {assertions['status']!r}")
    if "status_in" in assertions:
        check(state.get("status") in assertions["status_in"], f"status={state.get('status')!r} not in {assertions['status_in']}")

    if assertions.get("share_code") == "*":
        check(bool(state.get("share_code")), "share_code expected to be set, got None")
    elif "share_code" in assertions:
        check(state.get("share_code") == assertions["share_code"], f"share_code={state.get('share_code')!r}")

    if "default_language" in assertions:
        check(
            state.get("default_language") == assertions["default_language"],
            f"default_language={state.get('default_language')!r}, expected {assertions['default_language']!r}",
        )

    if "supported_languages_superset" in assertions:
        want = set(assertions["supported_languages_superset"])
        have = set(state.get("supported_languages") or [])
        check(want <= have, f"supported_languages={sorted(have)} missing {sorted(want - have)}")

    if "supported_languages_exact" in assertions:
        want = list(assertions["supported_languages_exact"])
        have = state.get("supported_languages") or []
        check(sorted(want) == sorted(have), f"supported_languages={have}, expected {want}")

    if "num_groups" in assertions:
        check(state.get("num_groups") == assertions["num_groups"], f"num_groups={state.get('num_groups')!r}")
    if "num_groups_min" in assertions:
        check((state.get("num_groups") or 0) >= assertions["num_groups_min"], f"num_groups={state.get('num_groups')!r} < min")

    post_count = len(state.get("post_ids") or [])
    if "post_count" in assertions:
        check(post_count == assertions["post_count"], f"post_count={post_count}, expected {assertions['post_count']}")
    if "post_count_min" in assertions:
        check(post_count >= assertions["post_count_min"], f"post_count={post_count} < min {assertions['post_count_min']}")

    question_count = state.get("question_count") or 0
    if "question_count_min" in assertions:
        check(question_count >= assertions["question_count_min"], f"question_count={question_count} < min")
    if "question_count" in assertions:
        check(question_count == assertions["question_count"], f"question_count={question_count}, expected {assertions['question_count']}")

    if "has_question_type" in assertions:
        wanted = assertions["has_question_type"]
        wanted_list = [wanted] if isinstance(wanted, str) else list(wanted)
        missing_types = [t for t in wanted_list if t not in question_types]
        check(not missing_types, f"question_types={question_types} missing {missing_types}")

    if "final_text_contains_any" in assertions:
        needles = assertions["final_text_contains_any"]
        check(
            any(n.lower() in lowered for n in needles),
            f"final_text did not contain any of {needles!r} (got {text[:120]!r})",
        )
    if "final_text_contains_all" in assertions:
        needles = assertions["final_text_contains_all"]
        missing_needles = [n for n in needles if n.lower() not in lowered]
        check(not missing_needles, f"final_text missing {missing_needles!r} (got {text[:120]!r})")
    if "final_text_not_contains" in assertions:
        present = [n for n in assertions["final_text_not_contains"] if n.lower() in lowered]
        check(not present, f"final_text unexpectedly contains {present!r} (got {text[:120]!r})")

    if assertions.get("no_mutating_calls"):
        mutating = [n for n in tool_calls if n in MUTATING_TOOLS]
        check(not mutating, f"expected no mutating tool calls, got {mutating}")
    if "max_mutating_calls" in assertions:
        mutating = [n for n in tool_calls if n in MUTATING_TOOLS]
        check(
            len(mutating) <= assertions["max_mutating_calls"],
            f"{len(mutating)} mutating calls > max {assertions['max_mutating_calls']} ({mutating})",
        )

    if "expect_error_from" in assertions:
        wanted_tools = assertions["expect_error_from"]
        missing_err = [t for t in wanted_tools if t not in error_tool_calls]
        check(not missing_err, f"expected an is_error result from {missing_err}, error_tool_calls={error_tool_calls}")

    if assertions.get("share_link_present"):
        check(bool(state.get("share_link")), "share_link expected to be set, got None")

    return TerminalResult(passed=not failures, failures=failures)
