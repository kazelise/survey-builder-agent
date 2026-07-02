"""Sanity checks on the shared tool schema source (tools/__init__.py::TOOLS).

This is the seam a future MCP server would reuse verbatim (DESIGN.md §11) —
not implemented in this pass, but these tests lock down the invariants that
reuse depends on: unique names, strict schemas, and every `required` key
actually present in `properties`.
"""

from __future__ import annotations

from survey_agent.tools import TOOLS
from survey_agent.tools.schema import anthropic_tools


def test_exactly_twelve_tools_matching_design_catalog():
    names = [t.name for t in TOOLS]
    assert len(names) == 12
    assert len(set(names)) == 12  # no duplicates
    expected = {
        "create_survey", "update_survey", "get_survey", "list_surveys",
        "add_post", "update_post_display", "add_comment",
        "add_post_question", "add_survey_question", "publish_survey",
        "list_posts", "get_share_link",
    }
    assert set(names) == expected


def test_every_schema_is_strict_object_with_consistent_required():
    for spec in TOOLS:
        schema = spec.input_schema
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False, spec.name
        props = schema.get("properties", {})
        for required_key in schema.get("required", []):
            assert required_key in props, f"{spec.name}: required {required_key!r} not in properties"


def test_anthropic_tools_rendering_matches_specs_1to1():
    rendered = anthropic_tools(TOOLS)
    assert len(rendered) == len(TOOLS)
    for spec, tool_dict in zip(TOOLS, rendered):
        assert tool_dict["name"] == spec.name
        assert tool_dict["description"] == spec.description
        assert tool_dict["input_schema"] == spec.input_schema
        assert set(tool_dict.keys()) == {"name", "description", "input_schema"}


def test_language_enums_exclude_dropped_arabic_code():
    # #60 dropped Arabic support backend-side; the agent's schemas must not
    # resurrect it as a selectable language (DESIGN.md §3).
    for spec in TOOLS:
        props = spec.input_schema.get("properties", {})
        for field_schema in props.values():
            if field_schema.get("enum") and "en" in field_schema["enum"]:
                assert "ar" not in field_schema["enum"]
