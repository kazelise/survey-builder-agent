"""RealModel error-classification tests.

`anthropic.APIStatusError` is the base class for EVERY non-2xx response the
SDK raises (400/401/403/404/409/422 as well as 429/5xx) -- confirmed
against the installed SDK's exception hierarchy. Catching it broadly and
wrapping it as ModelUnavailableError conflates permanent, non-retryable
4xx failures (bad request shape, invalid key, locked-out account) with
genuinely transient ones (rate limit, 5xx), which would burn loop.py's
one-shot fallback-model retry (_complete_with_fallback) on an identical,
still-doomed request -- and contradicts http_client.py's own explicit
"4xx is not retried" policy (RETRYABLE_STATUS).
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from survey_agent.model import ModelUnavailableError, RealModel


def _real_model() -> RealModel:
    return RealModel(model="claude-opus-4-8", api_key="test-key")


def _status_error(status: int) -> anthropic.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", "http://x/v1/messages"))
    return anthropic.APIStatusError("boom", response=resp, body={"error": {"message": "boom"}})


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_4xx_errors_are_not_wrapped_as_model_unavailable(status, monkeypatch):
    model = _real_model()
    exc = _status_error(status)
    monkeypatch.setattr(model._client.messages, "create", lambda **kw: (_ for _ in ()).throw(exc))

    with pytest.raises(anthropic.APIStatusError) as exc_info:
        model.complete("sys", [{"role": "user", "content": "hi"}], [])

    assert exc_info.value.status_code == status
    assert not isinstance(exc_info.value, ModelUnavailableError)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_errors_are_still_wrapped_as_model_unavailable(status, monkeypatch):
    model = _real_model()
    exc = _status_error(status)
    monkeypatch.setattr(model._client.messages, "create", lambda **kw: (_ for _ in ()).throw(exc))

    with pytest.raises(ModelUnavailableError):
        model.complete("sys", [{"role": "user", "content": "hi"}], [])


def test_connection_error_is_still_wrapped_as_model_unavailable(monkeypatch):
    model = _real_model()
    exc = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x/v1/messages"))
    monkeypatch.setattr(model._client.messages, "create", lambda **kw: (_ for _ in ()).throw(exc))

    with pytest.raises(ModelUnavailableError):
        model.complete("sys", [{"role": "user", "content": "hi"}], [])
