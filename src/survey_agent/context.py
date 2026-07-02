"""Shared build-chain state, threaded through every tool handler.

Handlers mutate ``RunContext`` in addition to returning a tool_result. That
duplication is deliberate: ``loop.trim_context`` is allowed to drop old
(assistant tool_use, user tool_result) round pairs to stay under the context
budget, and if ``survey_id``/``share_code`` only lived in the transcript,
trimming could lose them. Living in ``RunContext`` means the final answer and
the grader can always find them regardless of what got trimmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .http_client import CS14Client


@dataclass
class RunContext:
    survey_id: int | None = None
    share_code: str | None = None
    status: str | None = None
    default_language: str = "en"
    supported_languages: list[str] = field(default_factory=list)
    num_groups: int = 1
    post_ids: list[int] = field(default_factory=list)
    question_count: int = 0

    def apply_survey(self, survey: dict) -> None:
        """Sync from a survey payload returned by create/get/patch/publish."""
        self.survey_id = survey.get("id", self.survey_id)
        self.share_code = survey.get("share_code", self.share_code)
        self.status = survey.get("status", self.status)
        self.default_language = survey.get("default_language", self.default_language)
        if survey.get("supported_languages") is not None:
            self.supported_languages = survey["supported_languages"]
        self.num_groups = survey.get("num_groups", self.num_groups)

    def share_link(self, language: str | None = None) -> str | None:
        if not self.share_code:
            return None
        return f"/survey/{self.share_code}?lang={language or self.default_language}"

    def as_state(self) -> dict:
        return {
            "survey_id": self.survey_id,
            "share_code": self.share_code,
            "status": self.status,
            "default_language": self.default_language,
            "supported_languages": list(self.supported_languages),
            "num_groups": self.num_groups,
            "post_ids": list(self.post_ids),
            "question_count": self.question_count,
            "share_link": self.share_link(),
        }


@dataclass
class HandlerContext:
    """Everything a tool handler needs: `(ctx, args) -> dict`."""

    client: CS14Client
    run: RunContext
