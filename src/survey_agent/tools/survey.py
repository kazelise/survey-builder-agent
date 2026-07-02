"""Survey-level tool handlers: create/update/get/list/publish + list_posts
and the local (no-HTTP) get_share_link.

Each handler is `(HandlerContext, args) -> dict`. They also update
`ctx.run` (RunContext) as a side effect — see context.py for why.
"""

from __future__ import annotations

from .schema import ToolError, ToolSpec

LANGUAGE_CODES = ["en", "zh-CN", "zh-TW", "ja", "ko", "es"]
_LANGUAGE_ALIASES = {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-tw": "zh-TW"}
PLATFORM_STYLES = ["x", "facebook", "instagram", "xiaohongshu"]
PLATFORM_UI_STYLES = [
    "twitter",
    "facebook",
    "instagram",
    "xiaohongshu",
    "truth_social",
    "bluesky",
    "douyin",
]

# Structural fields the backend locks once status=="published" (409). Mirrors
# `_STRUCTURAL_SURVEY_FIELDS` in backend/app/routers/surveys.py — kept in
# sync manually since the agent never imports backend code (DESIGN.md
# constraint). See DESIGN.md §3.
STRUCTURAL_SURVEY_FIELDS = frozenset(
    {
        "platform_style",
        "platform_ui_style",
        "num_groups",
        "group_names",
        "gaze_tracking_enabled",
        "gaze_interval_ms",
        "click_tracking_enabled",
        "calibration_enabled",
        "calibration_points",
        "default_language",
        "supported_languages",
    }
)


def _normalize_language(code: str) -> str:
    """Client-side mirror of the backend's alias map (zh -> zh-CN, zh-tw ->
    zh-TW) so the model's loose "zh"/"zh-cn" guesses become a valid enum
    value before they ever hit the network — one fewer 422 round trip."""
    if code in LANGUAGE_CODES:
        return code
    lowered = code.strip().lower()
    return _LANGUAGE_ALIASES.get(lowered, code)


def _normalize_languages(codes: list[str]) -> list[str]:
    seen: list[str] = []
    for code in codes:
        normalized = _normalize_language(code)
        if normalized not in seen:
            seen.append(normalized)
    return seen


def create_survey(ctx, args: dict) -> dict:
    payload = dict(args)
    if "default_language" in payload:
        payload["default_language"] = _normalize_language(payload["default_language"])
    if "supported_languages" in payload:
        payload["supported_languages"] = _normalize_languages(payload["supported_languages"])

    survey = ctx.client.create_survey(payload)
    ctx.run.apply_survey(survey)
    return {
        "survey_id": survey["id"],
        "share_code": survey["share_code"],
        "status": survey["status"],
        "default_language": survey["default_language"],
        "supported_languages": survey["supported_languages"],
        "num_groups": survey["num_groups"],
        "group_names": survey.get("group_names"),
    }


def update_survey(ctx, args: dict) -> dict:
    survey_id = args["survey_id"]
    updates = {k: v for k, v in args.items() if k != "survey_id"}
    if "default_language" in updates and updates["default_language"] is not None:
        updates["default_language"] = _normalize_language(updates["default_language"])
    if "supported_languages" in updates and updates["supported_languages"] is not None:
        updates["supported_languages"] = _normalize_languages(updates["supported_languages"])

    # Pre-check against locally tracked state so an obviously-doomed patch
    # (renaming groups after publish) never makes a network round trip; the
    # backend is still the source of truth (this is a UX shortcut, not a
    # security boundary) — DESIGN.md §3 "Publish-locked fields".
    if ctx.run.survey_id == survey_id and ctx.run.status == "published":
        blocked = STRUCTURAL_SURVEY_FIELDS & updates.keys()
        if blocked:
            raise ToolError(
                f"Cannot change {sorted(blocked)} on a published survey — these are "
                "locked at create_survey time. Only content fields (title, "
                "description) can still change."
            )

    survey = ctx.client.patch_survey(survey_id, updates)
    ctx.run.apply_survey(survey)
    return {"survey_id": survey["id"], "status": survey["status"], "updated_fields": sorted(updates.keys())}


def get_survey(ctx, args: dict) -> dict:
    survey = ctx.client.get_survey(args["survey_id"])
    if ctx.run.survey_id in (None, survey["id"]):
        ctx.run.apply_survey(survey)
    return survey


def list_surveys(ctx, args: dict) -> dict:
    return ctx.client.list_surveys(
        status=args.get("status"), limit=args.get("limit", 20), offset=args.get("offset", 0)
    )


def list_posts(ctx, args: dict) -> dict:
    posts = ctx.client.list_posts(
        args["survey_id"], limit=args.get("limit", 50), offset=args.get("offset", 0)
    )
    return {"count": len(posts), "posts": posts}


def publish_survey(ctx, args: dict) -> dict:
    survey_id = args["survey_id"]
    # Same idea as update_survey's pre-check: avoid a guaranteed 422 when we
    # already know locally there are no posts yet.
    if ctx.run.survey_id == survey_id and not ctx.run.post_ids:
        raise ToolError("Survey has no posts yet — call add_post at least once before publishing.")

    survey = ctx.client.publish_survey(survey_id)
    ctx.run.apply_survey(survey)
    return {
        "survey_id": survey["id"],
        "status": survey["status"],
        "share_code": survey["share_code"],
        "share_link": ctx.run.share_link(),
    }


def get_share_link(ctx, args: dict) -> dict:
    """Local-only tool: builds the link from RunContext, no HTTP call."""
    if ctx.run.share_code is None:
        raise ToolError("No share_code known yet — call create_survey (and publish_survey) first.")
    language = args.get("language")
    return {"share_link": ctx.run.share_link(language), "share_code": ctx.run.share_code}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="create_survey",
        description=(
            "Create a new draft survey. Language set and A/B group config are LOCKED after "
            "publish, so decide them here up front."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "platform_style": {"type": "string", "enum": PLATFORM_STYLES},
                "platform_ui_style": {"type": "string", "enum": PLATFORM_UI_STYLES},
                "num_groups": {"type": "integer", "minimum": 1, "maximum": 10},
                "group_names": {"type": "object", "description": '{"1": "with_likes", "2": "no_likes"}'},
                "default_language": {"type": "string", "enum": LANGUAGE_CODES},
                "supported_languages": {
                    "type": "array",
                    "items": {"type": "string", "enum": LANGUAGE_CODES},
                    "minItems": 1,
                },
            },
            "required": ["title", "default_language", "supported_languages"],
            "additionalProperties": False,
        },
        handler=create_survey,
    ),
    ToolSpec(
        name="update_survey",
        description=(
            "Patch a draft survey's title/description or other draft-mutable fields. "
            "Structural fields (languages, groups, tracking config) 409 once published."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "platform_style": {"type": "string", "enum": PLATFORM_STYLES},
                "platform_ui_style": {"type": "string", "enum": PLATFORM_UI_STYLES},
                "num_groups": {"type": "integer", "minimum": 1, "maximum": 10},
                "group_names": {"type": "object"},
                "default_language": {"type": "string", "enum": LANGUAGE_CODES},
                "supported_languages": {"type": "array", "items": {"type": "string", "enum": LANGUAGE_CODES}},
            },
            "required": ["survey_id"],
            "additionalProperties": False,
        },
        handler=update_survey,
    ),
    ToolSpec(
        name="get_survey",
        description="Fetch a single owned survey by id.",
        input_schema={
            "type": "object",
            "properties": {"survey_id": {"type": "integer"}},
            "required": ["survey_id"],
            "additionalProperties": False,
        },
        handler=get_survey,
    ),
    ToolSpec(
        name="list_surveys",
        description="List surveys owned by the current researcher, optionally filtered by status.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["draft", "published", "closed"]},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=list_surveys,
    ),
    ToolSpec(
        name="list_posts",
        description="List the posts already added to a survey.",
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["survey_id"],
            "additionalProperties": False,
        },
        handler=list_posts,
    ),
    ToolSpec(
        name="publish_survey",
        description="Publish a draft survey. Must have at least one post. This is the LAST mutating call.",
        input_schema={
            "type": "object",
            "properties": {"survey_id": {"type": "integer"}},
            "required": ["survey_id"],
            "additionalProperties": False,
        },
        handler=publish_survey,
    ),
    ToolSpec(
        name="get_share_link",
        description="Build the participant share link for the current survey (local, no network call).",
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "language": {"type": "string", "enum": LANGUAGE_CODES},
            },
            "required": ["survey_id"],
            "additionalProperties": False,
        },
        handler=get_share_link,
    ),
]
