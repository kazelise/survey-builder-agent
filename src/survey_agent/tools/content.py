"""Post/comment/question tool handlers.

No "manual post" endpoint exists on the backend — `add_post` only takes a
URL, and separate `update_post_display` calls set the shown content /
fake engagement numbers / per-group visibility (DESIGN.md §3).
"""

from __future__ import annotations

from .schema import ToolError, ToolSpec

QUESTION_TYPES = ["text", "free_text", "single_choice", "multiple_choice", "likert", "rating"]


def _validate_question_config(question_type: str, config: dict | None) -> None:
    """Client-side mirror of the backend's `validate_scale_config` /
    answer-time option checks, run early so a broken question never gets
    created (DESIGN.md §3 "single/multiple_choice options validated only at
    answer time... add_*_question handler client-side-validates")."""
    config = config or {}
    if question_type in ("single_choice", "multiple_choice"):
        options = config.get("options")
        if not isinstance(options, list) or not options:
            raise ToolError(
                f"{question_type} requires a non-empty config.options list, e.g. "
                '{"options": ["Agree", "Disagree"]}.'
            )
    elif question_type in ("likert", "rating"):
        minimum = config.get("min", 1)
        maximum = config.get("max", 5)
        if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 0 or minimum >= maximum:
            raise ToolError(
                f"{question_type} requires integer config.min < config.max with min >= 0 "
                f"(got min={minimum!r}, max={maximum!r})."
            )


def add_post(ctx, args: dict) -> dict:
    payload = {"original_url": args["original_url"], "order": args["order"]}
    post = ctx.client.create_post(args["survey_id"], payload)
    ctx.run.post_ids.append(post["id"])

    fetched = any(post.get(k) for k in ("fetched_title", "fetched_description", "fetched_image_url"))
    result = {
        "post_id": post["id"],
        "order": post["order"],
        "fetched_title": post.get("fetched_title"),
        "og_metadata_fetched": fetched,
    }
    if not fetched:
        # OG fetch silently no-ops on localhost/private-IP URLs (SSRF guard) —
        # surface that here so the model knows to follow up instead of
        # shipping a post with no visible content (DESIGN.md §3).
        result["hint"] = (
            "OG metadata came back empty (placeholder/localhost URL, or the fetch failed). "
            "Call update_post_display to set display_title/display_image_url/display_description."
        )
    return result


def update_post_display(ctx, args: dict) -> dict:
    survey_id = args["survey_id"]
    post_id = args["post_id"]
    updates = {k: v for k, v in args.items() if k not in ("survey_id", "post_id")}
    post = ctx.client.patch_post(survey_id, post_id, updates)
    return {"post_id": post["id"], "updated_fields": sorted(updates.keys())}


def add_comment(ctx, args: dict) -> dict:
    payload = {
        "author_name": args["author_name"],
        "text": args["text"],
        "author_avatar_url": args.get("author_avatar_url"),
    }
    comment = ctx.client.add_comment(args["survey_id"], args["post_id"], payload)
    return {"comment_id": comment["id"], "post_id": args["post_id"]}


def add_post_question(ctx, args: dict) -> dict:
    _validate_question_config(args["question_type"], args.get("config"))
    payload = {
        "question_type": args["question_type"],
        "text": args["text"],
        "order": args["order"],
        "config": args.get("config"),
    }
    question = ctx.client.create_post_question(args["survey_id"], args["post_id"], payload)
    ctx.run.question_count += 1
    return {"question_id": question["id"], "post_id": args["post_id"], "question_type": question["question_type"]}


def add_survey_question(ctx, args: dict) -> dict:
    _validate_question_config(args["question_type"], args.get("config"))
    payload = {
        "question_type": args["question_type"],
        "text": args["text"],
        "order": args["order"],
        "config": args.get("config"),
    }
    question = ctx.client.create_survey_question(args["survey_id"], payload)
    ctx.run.question_count += 1
    return {"question_id": question["id"], "question_type": question["question_type"]}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="add_post",
        description=(
            "Add a stimulus post by URL. The backend auto-fetches OG metadata; if it comes "
            "back empty, follow with update_post_display."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "original_url": {"type": "string"},
                "order": {"type": "integer"},
            },
            "required": ["survey_id", "original_url", "order"],
            "additionalProperties": False,
        },
        handler=add_post,
    ),
    ToolSpec(
        name="update_post_display",
        description=(
            "Override a post's shown title/image/description, fake engagement numbers, and "
            "per-group visibility (visible_to_groups / group_overrides)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "post_id": {"type": "integer"},
                "display_title": {"type": "string"},
                "display_image_url": {"type": "string"},
                "display_description": {"type": "string"},
                "source_label": {"type": "string"},
                "more_info_label": {"type": "string"},
                "display_likes": {"type": "integer"},
                "display_comments_count": {"type": "integer"},
                "display_shares": {"type": "integer"},
                "show_likes": {"type": "boolean"},
                "show_comments": {"type": "boolean"},
                "show_shares": {"type": "boolean"},
                "visible_to_groups": {"type": "array", "items": {"type": "integer"}},
                "group_overrides": {
                    "type": "object",
                    "description": 'Per-group field overrides, e.g. {"1": {"show_likes": false}}',
                },
            },
            "required": ["survey_id", "post_id"],
            "additionalProperties": False,
        },
        handler=update_post_display,
    ),
    ToolSpec(
        name="add_comment",
        description="Add a fake (researcher-authored) comment to a post.",
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "post_id": {"type": "integer"},
                "author_name": {"type": "string"},
                "text": {"type": "string"},
                "author_avatar_url": {"type": "string"},
            },
            "required": ["survey_id", "post_id", "author_name", "text"],
            "additionalProperties": False,
        },
        handler=add_comment,
    ),
    ToolSpec(
        name="add_post_question",
        description=(
            "Add a question attached to ONE specific post — participants answer it right "
            "under that post. Use when the researcher pairs questions with posts ('each "
            "post has a … question', '每条帖子下提问', or one post immediately followed by "
            "its questions). For overall questionnaire items, use add_survey_question."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "post_id": {"type": "integer"},
                "question_type": {"type": "string", "enum": QUESTION_TYPES},
                "text": {"type": "string"},
                "order": {"type": "integer"},
                "config": {
                    "type": "object",
                    "description": '{"options": [...]} for choice types, {"min":,"max":} for likert/rating',
                },
            },
            "required": ["survey_id", "post_id", "question_type", "text", "order"],
            "additionalProperties": False,
        },
        handler=add_post_question,
    ),
    ToolSpec(
        name="add_survey_question",
        description=(
            "Add a survey-level questionnaire item shown after the feed (trust, attitude, "
            "demographics, overall reactions) — for questions not tied to a specific post "
            "('some posts and some questions'). When questions are paired with posts, use "
            "add_post_question instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "survey_id": {"type": "integer"},
                "question_type": {"type": "string", "enum": QUESTION_TYPES},
                "text": {"type": "string"},
                "order": {"type": "integer"},
                "config": {"type": "object"},
            },
            "required": ["survey_id", "question_type", "text", "order"],
            "additionalProperties": False,
        },
        handler=add_survey_question,
    ),
]
