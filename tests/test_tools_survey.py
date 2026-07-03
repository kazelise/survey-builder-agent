"""Handler-level unit tests for tools/survey.py -- direct (ctx, args) ->
dict calls, no ToolExecutor/loop/model involved. Complements the
integration-level eval cases with deterministic, fast coverage of the
boundary conditions those don't pin down (see the "core business logic has
no unit tests" audit finding: publish-lock pre-check, language alias
normalization, get_share_link's declared-but-ignored survey_id)."""

from __future__ import annotations

import pytest

from survey_agent.context import HandlerContext, RunContext
from survey_agent.http_client import CS14Client
from survey_agent.tools.schema import ToolError
from survey_agent.tools.survey import create_survey, get_share_link


def _ctx() -> HandlerContext:
    return HandlerContext(client=CS14Client(base_url="http://unused", dry_run=True), run=RunContext())


def test_get_share_link_returns_link_for_the_survey_this_run_built():
    ctx = _ctx()
    survey = create_survey(ctx, {"title": "T", "default_language": "en", "supported_languages": ["en"]})
    result = get_share_link(ctx, {"survey_id": survey["survey_id"]})
    assert result["share_code"] == survey["share_code"]
    assert result["share_link"] == f"/survey/{survey['share_code']}?lang=en"


def test_get_share_link_rejects_a_survey_id_that_does_not_match_the_current_run():
    # Regression: get_share_link's schema declares survey_id as required,
    # but the handler never read args["survey_id"] -- it always returned
    # ctx.run.share_code regardless. Building survey A then asking for
    # survey B's share link used to silently return A's link with
    # is_error=False (RunContext only ever tracks one survey per run).
    ctx = _ctx()
    survey_a = create_survey(ctx, {"title": "A", "default_language": "en", "supported_languages": ["en"]})
    other_survey_id = survey_a["survey_id"] + 999  # a survey_id this run never built/loaded

    with pytest.raises(ToolError):
        get_share_link(ctx, {"survey_id": other_survey_id})


def test_get_share_link_raises_before_any_survey_exists():
    ctx = _ctx()
    with pytest.raises(ToolError):
        get_share_link(ctx, {"survey_id": 1})
