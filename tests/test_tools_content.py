"""Handler-level unit tests for tools/content.py -- direct (ctx, args) ->
dict calls, no ToolExecutor/loop/model. Covers _validate_question_config's
boundary conditions (see the "core business logic has no unit tests" audit
finding): single/multiple_choice need non-empty options; likert/rating
need integer 0<=min<max."""

from __future__ import annotations

import pytest

from survey_agent.context import HandlerContext, RunContext
from survey_agent.http_client import CS14Client
from survey_agent.tools.content import add_post, add_post_question, add_survey_question
from survey_agent.tools.schema import ToolError
from survey_agent.tools.survey import create_survey


def _ctx() -> HandlerContext:
    return HandlerContext(client=CS14Client(base_url="http://unused", dry_run=True), run=RunContext())


def _survey_with_post(ctx) -> tuple[int, int]:
    survey = create_survey(ctx, {"title": "T", "default_language": "en", "supported_languages": ["en"]})
    post = add_post(ctx, {"survey_id": survey["survey_id"], "original_url": "https://x.com/1", "order": 1})
    return survey["survey_id"], post["post_id"]


@pytest.mark.parametrize("question_type", ["single_choice", "multiple_choice"])
def test_choice_questions_reject_empty_or_missing_options(question_type):
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    with pytest.raises(ToolError):
        add_post_question(
            ctx,
            {
                "survey_id": survey_id,
                "post_id": post_id,
                "question_type": question_type,
                "text": "Pick one",
                "order": 1,
                "config": {"options": []},
            },
        )
    with pytest.raises(ToolError):
        add_post_question(
            ctx,
            {
                "survey_id": survey_id,
                "post_id": post_id,
                "question_type": question_type,
                "text": "Pick one",
                "order": 1,
                "config": {},
            },
        )


def test_choice_question_with_options_succeeds():
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    result = add_post_question(
        ctx,
        {
            "survey_id": survey_id,
            "post_id": post_id,
            "question_type": "single_choice",
            "text": "Pick one",
            "order": 1,
            "config": {"options": ["Agree", "Disagree"]},
        },
    )
    assert result["question_type"] == "single_choice"


@pytest.mark.parametrize("question_type", ["likert", "rating"])
@pytest.mark.parametrize("bad_config", [{"min": 3, "max": 3}, {"min": 5, "max": 1}, {"min": -1, "max": 5}])
def test_likert_rating_reject_invalid_min_max(question_type, bad_config):
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    with pytest.raises(ToolError):
        add_post_question(
            ctx,
            {
                "survey_id": survey_id,
                "post_id": post_id,
                "question_type": question_type,
                "text": "Rate it",
                "order": 1,
                "config": bad_config,
            },
        )


@pytest.mark.parametrize("question_type", ["likert", "rating"])
def test_likert_rating_accepts_a_valid_range(question_type):
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    result = add_post_question(
        ctx,
        {
            "survey_id": survey_id,
            "post_id": post_id,
            "question_type": question_type,
            "text": "Rate it",
            "order": 1,
            "config": {"min": 1, "max": 5},
        },
    )
    assert result["question_type"] == question_type


def test_likert_rating_uses_a_valid_default_range_when_config_omitted():
    # _validate_question_config defaults to min=1, max=5 when config is
    # None/omitted -- must not spuriously raise.
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    result = add_post_question(
        ctx,
        {"survey_id": survey_id, "post_id": post_id, "question_type": "likert", "text": "Rate it", "order": 1},
    )
    assert result["question_type"] == "likert"


def test_text_and_free_text_question_types_need_no_config_validation():
    ctx = _ctx()
    survey_id, post_id = _survey_with_post(ctx)
    for question_type in ("text", "free_text"):
        result = add_post_question(
            ctx,
            {
                "survey_id": survey_id,
                "post_id": post_id,
                "question_type": question_type,
                "text": "Anything to add?",
                "order": 1,
            },
        )
        assert result["question_type"] == question_type


def test_add_survey_question_applies_the_same_validation_as_add_post_question():
    ctx = _ctx()
    survey_id, _post_id = _survey_with_post(ctx)
    with pytest.raises(ToolError):
        add_survey_question(
            ctx,
            {
                "survey_id": survey_id,
                "question_type": "multiple_choice",
                "text": "Pick any",
                "order": 1,
                "config": {"options": []},
            },
        )
