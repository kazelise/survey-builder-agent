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
from survey_agent.tools.content import add_post
from survey_agent.tools.schema import ToolError
from survey_agent.tools.survey import _normalize_language, create_survey, get_share_link, publish_survey, update_survey


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


# ── _normalize_language: zh/zh-cn/zh-tw alias mapping (DESIGN.md §3) ──────


def test_normalize_language_maps_known_aliases_to_canonical_codes():
    assert _normalize_language("zh") == "zh-CN"
    assert _normalize_language("zh-cn") == "zh-CN"
    assert _normalize_language("ZH-CN") == "zh-CN"  # case-insensitive
    assert _normalize_language("zh-tw") == "zh-TW"
    assert _normalize_language("  zh  ") == "zh-CN"  # surrounding whitespace stripped


def test_normalize_language_passes_through_already_canonical_and_unknown_codes():
    assert _normalize_language("en") == "en"  # already canonical: no-op
    assert _normalize_language("zh-CN") == "zh-CN"  # already canonical: no-op
    assert _normalize_language("fr") == "fr"  # unknown code passed through untouched (backend 422s it)


def test_update_survey_normalizes_a_language_alias_before_sending():
    ctx = _ctx()
    survey = create_survey(ctx, {"title": "T", "default_language": "en", "supported_languages": ["en"]})
    update_survey(ctx, {"survey_id": survey["survey_id"], "default_language": "zh"})
    assert ctx.run.default_language == "zh-CN"


# ── update_survey: publish-lock pre-check (STRUCTURAL_SURVEY_FIELDS) ─────


def _published_survey(ctx) -> int:
    survey = create_survey(ctx, {"title": "T", "default_language": "en", "supported_languages": ["en"], "num_groups": 1})
    add_post(ctx, {"survey_id": survey["survey_id"], "original_url": "https://x.com/1", "order": 1})
    publish_survey(ctx, {"survey_id": survey["survey_id"]})
    return survey["survey_id"]


def test_update_survey_blocks_structural_fields_after_publish():
    ctx = _ctx()
    survey_id = _published_survey(ctx)
    with pytest.raises(ToolError):
        update_survey(ctx, {"survey_id": survey_id, "num_groups": 2})


def test_update_survey_still_allows_content_only_fields_after_publish():
    # DESIGN.md §3: only content fields (title, description) may still
    # change once published -- everything structural 409s. This must keep
    # working, not just the block-case above.
    ctx = _ctx()
    survey_id = _published_survey(ctx)
    result = update_survey(ctx, {"survey_id": survey_id, "title": "New title"})
    assert result["status"] == "published"
    assert result["updated_fields"] == ["title"]


def test_update_survey_lock_only_applies_to_the_survey_this_run_built():
    # The pre-check is keyed on `ctx.run.survey_id == survey_id and
    # ctx.run.status == "published"` -- it must not misfire for a
    # different survey_id this run never touched (that's a backend-side
    # 409 concern, not something the local shortcut should guess at).
    ctx = _ctx()
    _published_survey(ctx)
    other_id = 999
    # Should not raise ToolError locally (no local record of survey 999
    # being published) -- the dry-run backend will 404, which is a
    # different, expected failure mode, not the publish-lock ToolError.
    with pytest.raises(Exception) as exc_info:
        update_survey(ctx, {"survey_id": other_id, "num_groups": 2})
    from survey_agent.http_client import CS14ApiError

    assert not isinstance(exc_info.value, ToolError)
    assert isinstance(exc_info.value, CS14ApiError)
