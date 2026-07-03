"""CS14Client tests against an httpx.MockTransport — no real network, no
running backend required. Covers retry/backoff, pagination shape
differences, and error mapping (DESIGN.md §5, §10)."""

from __future__ import annotations

import email.utils

import httpx
import pytest

from survey_agent.http_client import CS14ApiError, CS14Client, safe_error_text


def _client(handler, **kwargs) -> CS14Client:
    transport = httpx.MockTransport(handler)
    return CS14Client(
        base_url="http://testserver",
        transport=transport,
        max_retries=kwargs.pop("max_retries", 3),
        retry_base_delay=0.001,
        retry_max_delay=0.01,
        sleep_fn=lambda _seconds: None,  # no real sleeping in tests
        **kwargs,
    )


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(201, json={"id": 1, "title": "t"})

    client = _client(handler)
    result = client.create_survey({"title": "t"})
    assert result == {"id": 1, "title": "t"}
    assert calls["n"] == 3  # two failed attempts + one success


def test_gives_up_after_max_retries_and_raises_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "still down"})

    client = _client(handler, max_retries=2)
    with pytest.raises(CS14ApiError) as exc_info:
        client.create_survey({"title": "t"})
    assert exc_info.value.status == 503


def test_semantic_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "invalid language code"})

    client = _client(handler)
    with pytest.raises(CS14ApiError) as exc_info:
        client.create_survey({"title": "t", "default_language": "xx"})
    assert exc_info.value.status == 422
    assert calls["n"] == 1  # no retry on a semantic error


def test_pagination_shapes_differ_surveys_wrapped_posts_bare():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/surveys":
            return httpx.Response(200, json={"items": [{"id": 1}], "total": 1})
        if request.url.path == "/surveys/1/posts":
            return httpx.Response(200, json=[{"id": 10}, {"id": 11}])
        return httpx.Response(404, json={"detail": "not found"})

    client = _client(handler)
    surveys = client.list_surveys()
    posts = client.list_posts(1)
    assert surveys == {"items": [{"id": 1}], "total": 1}
    assert posts == [{"id": 10}, {"id": 11}]


def test_ensure_researcher_falls_back_to_register_on_401():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/auth/login" and calls.count("/auth/login") == 1:
            return httpx.Response(401, json={"detail": "no such account"})
        if request.url.path == "/auth/register":
            return httpx.Response(201, json={"id": 1, "email": "a@b.com", "name": "a"})
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "jwt-123", "expires_in": 3600})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    token = client.ensure_researcher("a@b.com", "pw")
    assert token == "jwt-123"
    assert calls == ["/auth/login", "/auth/register", "/auth/login"]


def test_retry_after_header_is_honored_on_429():
    waits: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"id": 1})

    client = _client(handler)
    client.sleep_fn = lambda seconds: waits.append(seconds)
    result = client.get_survey(1)
    assert result == {"id": 1}
    assert waits == [0.0]


def test_retry_after_header_with_rfc7231_http_date_form_does_not_crash():
    # Regression: Retry-After (RFC 7231 §7.1.3) may be an HTTP-date string
    # instead of delta-seconds, e.g. "Wed, 21 Oct 2026 07:28:00 GMT".
    # `float(retry_after)` on that form raised ValueError, uncaught
    # anywhere in _request, aborting the whole request instead of falling
    # back to the computed exponential backoff delay.
    calls = {"n": 0}
    retry_date = email.utils.formatdate(usegmt=True)  # RFC 1123 HTTP-date, "now"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": retry_date}, json={"detail": "slow down"})
        return httpx.Response(200, json={"id": 1})

    client = _client(handler)
    waits: list[float] = []
    client.sleep_fn = lambda seconds: waits.append(seconds)

    result = client.get_survey(1)  # must not raise ValueError

    assert result == {"id": 1}
    assert len(waits) == 1
    assert waits[0] >= 0.0


def test_safe_error_text_redacts_password_and_token_shaped_top_level_fields():
    # Regression: mcp_server.py's build_executor and cli.py's main both
    # print(f"...{exc}...") an auth-failure exception straight to stderr
    # with zero redaction guarantee -- CS14ApiError.__str__ embeds the raw
    # backend response body verbatim.
    exc = CS14ApiError(422, {"password": "hunter2", "token": "abc123", "email": "a@b.com"})
    text = safe_error_text(exc)
    assert "hunter2" not in text
    assert "abc123" not in text
    assert "<REDACTED>" in text
    assert "a@b.com" in text  # non-secret fields stay readable for debugging


def test_safe_error_text_redacts_pydantic_422_loc_input_echo_pattern():
    # The realistic leak path: FastAPI/Pydantic v2 422 validation-error
    # details commonly echo the submitted value under a generic "input"
    # key, correlated with a "loc" path naming which field failed -- not a
    # literal top-level "password" key.
    exc = CS14ApiError(
        422,
        {"detail": [{"type": "string_too_short", "loc": ["body", "password"], "msg": "...", "input": "hunter2"}]},
    )
    text = safe_error_text(exc)
    assert "hunter2" not in text


def test_safe_error_text_falls_back_to_plain_str_for_non_cs14_errors():
    assert safe_error_text(RuntimeError("boom")) == "boom"


def test_cs14_api_error_body_attribute_stays_unredacted_for_the_model_to_read():
    # executor.py forwards exc.body verbatim to the model as a tool_result
    # so it can read and self-correct real validation errors (DESIGN.md
    # §10) -- redaction must be opt-in at the log/stderr call site
    # (safe_error_text) only, never applied to the raw .body attribute.
    exc = CS14ApiError(422, {"password": "hunter2"})
    assert exc.body == {"password": "hunter2"}


def test_retry_after_header_with_garbage_value_falls_back_to_computed_delay():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "not-a-valid-value"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"id": 1})

    client = _client(handler)
    waits: list[float] = []
    client.sleep_fn = lambda seconds: waits.append(seconds)

    result = client.get_survey(1)  # must not raise ValueError

    assert result == {"id": 1}
    assert len(waits) == 1
