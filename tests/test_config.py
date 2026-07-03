"""Settings default/env-loading tests.

config.py is the only module (besides cli.py) that reads os.environ
directly, so its dataclass defaults ARE the actual runtime behavior
whenever no .env/env var is set -- not just documentation. VERIFY.md
documented a real integration bug from this: the default base_url has no
`/api/v1` prefix (http_client.py calls relative paths like "/auth/login"
with no prefix added internally, so every request 404s) and the default
password didn't match the backend seed script's actual default (a 401
login loop). `.env.example` was corrected at some point, but these
hardcoded defaults were not, so anyone constructing `Settings()` directly
(or relying on `from_env()`'s fallback with no env var set) still hit both
bugs.
"""

from __future__ import annotations

from survey_agent.config import Settings


def test_default_cs14_base_url_includes_api_v1_prefix():
    assert Settings().cs14_base_url == "http://localhost:8000/api/v1"


def test_default_cs14_password_matches_env_example_not_the_stale_placeholder():
    assert Settings().cs14_password == "change-me-client-demo"


def test_from_env_fallback_matches_the_dataclass_default_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("CS14_BASE_URL", raising=False)
    monkeypatch.delenv("CS14_PASSWORD", raising=False)
    settings = Settings.from_env()
    assert settings.cs14_base_url == Settings().cs14_base_url
    assert settings.cs14_password == Settings().cs14_password
