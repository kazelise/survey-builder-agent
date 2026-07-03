"""Central settings: env-driven, CLI-overridable.

Nothing else in this project reads ``os.environ`` directly — every other
module takes plain constructor args instead. That's what makes the rest of
the codebase (loop, executor, http_client, model) testable without env-var
gymnastics; only this module and ``cli.py`` know about the process
environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - python-dotenv is a hard dependency,
    # but staying defensive here means `import config` never explodes in an
    # environment where it hasn't been installed yet (e.g. a bare `python -c`).
    pass

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_FALLBACK_MODEL = "claude-sonnet-5"

# USD per 1M tokens. cache_read is priced at a fraction of input per
# Anthropic's published discount; kept as a lookup table (not a formula) so
# adding a new model id is a one-line change. See DESIGN.md §9.
PRICE_TABLE_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08},
}
_ZERO_PRICE = {"input": 0.0, "output": 0.0, "cache_read": 0.0}


@dataclass
class Settings:
    # Model client
    model: str = DEFAULT_MODEL
    model_fallback: str = DEFAULT_FALLBACK_MODEL
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    max_tokens: int = 8000

    # cs14 backend. Must include the /api/v1 prefix -- http_client.py calls
    # relative paths like "/auth/login" with no prefix added internally, so
    # a base_url without it 404s on every single call (see .env.example and
    # VERIFY.md's "Bug found during smoke test"). cs14_password mirrors the
    # backend seed script's actual default (DEMO_RESEARCHER_PASSWORD), not
    # an arbitrary placeholder -- a mismatch here is a 401 login loop, not
    # just a cosmetic default.
    cs14_base_url: str = "http://localhost:8000/api/v1"
    cs14_email: str = "cs14.demo@example.com"
    cs14_password: str = "change-me-client-demo"

    # Agent loop
    max_turns: int = 20

    # HTTP retry/backoff (§10)
    max_retries: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0

    # Context management (§10)
    tool_result_max_chars: int = 4000
    context_budget_tokens: int = 120_000

    # Tracing (§9)
    trace_path: str | None = None

    @classmethod
    def from_env(cls, **overrides: object) -> "Settings":
        """Build Settings from env vars, then apply any non-None CLI overrides."""
        base = cls(
            model=os.environ.get("MODEL", DEFAULT_MODEL),
            model_fallback=os.environ.get("MODEL_FALLBACK", DEFAULT_FALLBACK_MODEL),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            cs14_base_url=os.environ.get("CS14_BASE_URL", "http://localhost:8000/api/v1"),
            cs14_email=os.environ.get("CS14_EMAIL", "cs14.demo@example.com"),
            cs14_password=os.environ.get("CS14_PASSWORD", "change-me-client-demo"),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(base, key, value)
        return base

    def price_for(self, model: str | None = None) -> dict[str, float]:
        return PRICE_TABLE_PER_MTOK.get(model or self.model, _ZERO_PRICE)


def estimate_cost(usage: dict[str, int], price: dict[str, float]) -> float:
    """usage is {"input":, "output":, "cache_read":, "cache_creation":} in
    raw token counts; price is USD per 1M tokens. cache_creation is billed
    at the input rate (Anthropic's write premium is out of scope for v1)."""
    tokens_cost = (
        usage.get("input", 0) * price.get("input", 0.0)
        + usage.get("output", 0) * price.get("output", 0.0)
        + usage.get("cache_read", 0) * price.get("cache_read", 0.0)
        + usage.get("cache_creation", 0) * price.get("input", 0.0)
    )
    return tokens_cost / 1_000_000
