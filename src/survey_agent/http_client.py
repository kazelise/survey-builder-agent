"""Thin httpx wrapper around the cs14 backend REST API.

This is the ONLY module that touches the network. Handlers in ``tools/*.py``
are thin adapters on top of the typed methods here — that split is what lets
the exact same handler back both this SDK loop and the MCP server
(``mcp_server.py``) without duplicating request-building or retry logic
(DESIGN.md §5, §11).
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# 429/5xx are transient — worth a retry. 4xx semantic errors (400/404/409/422)
# are NOT retried: retrying a "language code invalid" response can't fix it,
# only the model changing its arguments can (DESIGN.md §10).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CS14ApiError(Exception):
    """A non-2xx response the caller should react to semantically, not retry."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"cs14 API error {status}: {body}")


@dataclass
class CS14Client:
    base_url: str
    max_retries: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    timeout: float = 30.0
    # Injection seam for tests: pass an httpx.MockTransport instead of hitting
    # the network. Production code leaves this None and gets a real socket.
    transport: httpx.BaseTransport | None = None
    # When True, every method below returns a synthetic-but-shaped response
    # without touching the network at all — lets `cli.py --dry-run --mock`
    # demo the full build chain with no backend and no API key.
    dry_run: bool = False
    sleep_fn: Any = field(default=time.sleep, repr=False)

    _token: str | None = field(default=None, init=False, repr=False)
    _client: httpx.Client = field(init=False, repr=False)
    _dry_seq: dict[str, int] = field(
        default_factory=lambda: {"survey": 0, "post": 0, "question": 0, "comment": 0},
        init=False,
        repr=False,
    )
    _dry_store: dict[str, dict[int, dict]] = field(
        default_factory=lambda: {"surveys": {}, "posts": {}}, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport)

    # ── auth ──────────────────────────────────────────────────────────
    def login(self, email: str, password: str) -> str:
        resp = self._request(
            "POST", "/auth/login", json={"email": email, "password": password}, auth=False
        )
        self._token = resp["access_token"]
        return self._token

    def register(self, email: str, password: str, name: str) -> dict:
        return self._request(
            "POST",
            "/auth/register",
            json={"email": email, "password": password, "name": name},
            auth=False,
        )

    def ensure_researcher(self, email: str, password: str, name: str | None = None) -> str:
        """Login; on 401 (account doesn't exist yet) register then login.

        Idempotent, so the CLI can call this on every run instead of needing
        a separate one-time signup step.
        """
        try:
            return self.login(email, password)
        except CS14ApiError as exc:
            if exc.status != 401:
                raise
            self.register(email, password, name or email.split("@")[0])
            return self.login(email, password)

    # ── surveys ───────────────────────────────────────────────────────
    def create_survey(self, payload: dict) -> dict:
        return self._request("POST", "/surveys", json=payload)

    def patch_survey(self, survey_id: int, payload: dict) -> dict:
        return self._request("PATCH", f"/surveys/{survey_id}", json=payload)

    def get_survey(self, survey_id: int) -> dict:
        return self._request("GET", f"/surveys/{survey_id}")

    def list_surveys(self, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._request("GET", "/surveys", params=params)  # {items, total} — wrapped

    def publish_survey(self, survey_id: int) -> dict:
        return self._request("POST", f"/surveys/{survey_id}/publish")

    # ── posts ─────────────────────────────────────────────────────────
    def create_post(self, survey_id: int, payload: dict) -> dict:
        return self._request("POST", f"/surveys/{survey_id}/posts", json=payload)

    def patch_post(self, survey_id: int, post_id: int, payload: dict) -> dict:
        return self._request("PATCH", f"/surveys/{survey_id}/posts/{post_id}", json=payload)

    def list_posts(self, survey_id: int, limit: int = 50, offset: int = 0) -> list:
        return self._request(
            "GET", f"/surveys/{survey_id}/posts", params={"limit": limit, "offset": offset}
        )  # bare array — NOT {items, total} (see DESIGN.md §3 pagination row)

    def add_comment(self, survey_id: int, post_id: int, payload: dict) -> dict:
        return self._request(
            "POST", f"/surveys/{survey_id}/posts/{post_id}/comments", json=payload
        )

    # ── questions ─────────────────────────────────────────────────────
    def create_post_question(self, survey_id: int, post_id: int, payload: dict) -> dict:
        return self._request(
            "POST", f"/surveys/{survey_id}/posts/{post_id}/questions", json=payload
        )

    def create_survey_question(self, survey_id: int, payload: dict) -> dict:
        return self._request("POST", f"/surveys/{survey_id}/questions", json=payload)

    # ── core request + retry ─────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
    ) -> Any:
        if self.dry_run:
            return self._dry_run_response(method, path, json)

        headers = {}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        delay = self.retry_base_delay
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, json=json, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                self._backoff_sleep(delay, None)
                delay = min(delay * 2, self.retry_max_delay)
                continue

            if resp.status_code < 400:
                return resp.json() if resp.content else None

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                self._backoff_sleep(delay, float(retry_after) if retry_after else None)
                delay = min(delay * 2, self.retry_max_delay)
                continue

            # Semantic 4xx, or a retryable status with retries exhausted:
            # surface it as a typed error instead of raising httpx's generic one.
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise CS14ApiError(resp.status_code, body)

        # Unreachable in practice (the loop always returns or raises), but
        # keeps mypy/pyright happy about the function always returning.
        raise CS14ApiError(599, "retries exhausted")

    def _backoff_sleep(self, delay: float, retry_after: float | None) -> None:
        wait = retry_after if retry_after is not None else delay + random.uniform(0, delay * 0.25)
        self.sleep_fn(wait)

    def close(self) -> None:
        self._client.close()

    # ── dry-run stub backend ─────────────────────────────────────────
    # Deliberately not exhaustive: it covers the happy path of the 11
    # HTTP-backed tools (of TOOLS' 13 total — get_share_link and
    # search_handbook are local, no HTTP, so they never reach this stub)
    # well enough to demo/CI the CLI with zero network and zero backend, but
    # it does not replicate every validation rule the real FastAPI app has
    # (that's what test_http_client.py's MockTransport tests are for).
    def _dry_run_response(self, method: str, path: str, json_body: dict | None) -> Any:
        json_body = json_body or {}
        now = "2026-01-01T00:00:00Z"

        if path == "/auth/login":
            return {"access_token": "dry-run-token", "token_type": "bearer", "expires_in": 3600}
        if path == "/auth/register":
            return {"id": 1, "email": json_body.get("email"), "name": json_body.get("name"), "created_at": now}

        if path == "/surveys" and method == "POST":
            self._dry_seq["survey"] += 1
            sid = self._dry_seq["survey"]
            survey = {
                "id": sid,
                "title": json_body.get("title", "Untitled survey"),
                "description": json_body.get("description"),
                "status": "draft",
                "share_code": f"dry{sid:04d}",
                "platform_style": json_body.get("platform_style", "x"),
                "num_groups": json_body.get("num_groups", 1),
                "group_names": json_body.get("group_names"),
                "gaze_tracking_enabled": True,
                "gaze_interval_ms": 1000,
                "click_tracking_enabled": True,
                "calibration_enabled": True,
                "calibration_points": 9,
                "platform_ui_style": json_body.get("platform_ui_style", "twitter"),
                "default_language": json_body.get("default_language", "en"),
                "supported_languages": json_body.get("supported_languages", ["en"]),
                "share_code_expires_at": None,
                "published_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._dry_store["surveys"][sid] = survey
            return survey

        m = re.fullmatch(r"/surveys/(\d+)", path)
        if m and method == "GET":
            return self._dry_survey_or_404(int(m.group(1)))
        if m and method == "PATCH":
            survey = self._dry_survey_or_404(int(m.group(1)))
            survey.update(json_body)
            survey["updated_at"] = now
            return survey

        if path == "/surveys" and method == "GET":
            items = list(self._dry_store["surveys"].values())
            return {"items": items, "total": len(items)}

        m = re.fullmatch(r"/surveys/(\d+)/publish", path)
        if m:
            survey = self._dry_survey_or_404(int(m.group(1)))
            survey["status"] = "published"
            survey["published_at"] = now
            return survey

        m = re.fullmatch(r"/surveys/(\d+)/posts", path)
        if m and method == "POST":
            self._dry_seq["post"] += 1
            pid = self._dry_seq["post"]
            post = {
                "id": pid,
                "survey_id": int(m.group(1)),
                "order": json_body.get("order", 1),
                "original_url": json_body.get("original_url"),
                "fetched_title": None,
                "fetched_image_url": None,
                "fetched_description": None,
                "fetched_source": None,
                "display_title": None,
                "display_image_url": None,
                "display_description": None,
                "source_label": None,
                "display_likes": 0,
                "display_comments_count": 0,
                "display_shares": 0,
                "show_likes": True,
                "show_comments": True,
                "show_shares": True,
                "visible_to_groups": None,
                "group_overrides": None,
                "more_info_label": "More Information",
                "comments": [],
                "questions": [],
                "created_at": now,
            }
            self._dry_store["posts"][pid] = post
            return post
        if m and method == "GET":
            return [p for p in self._dry_store["posts"].values() if p["survey_id"] == int(m.group(1))]

        m = re.fullmatch(r"/surveys/(\d+)/posts/(\d+)", path)
        if m and method == "PATCH":
            post = self._dry_post_or_404(int(m.group(2)))
            post.update({k: v for k, v in json_body.items() if v is not None})
            return post

        m = re.fullmatch(r"/surveys/(\d+)/posts/(\d+)/comments", path)
        if m and method == "POST":
            self._dry_seq["comment"] += 1
            return {
                "id": self._dry_seq["comment"],
                "order": self._dry_seq["comment"],
                "author_name": json_body.get("author_name"),
                "author_avatar_url": json_body.get("author_avatar_url"),
                "text": json_body.get("text"),
            }

        m = re.fullmatch(r"/surveys/(\d+)/posts/(\d+)/questions", path)
        if m and method == "POST":
            self._dry_seq["question"] += 1
            return {
                "id": self._dry_seq["question"],
                "survey_id": int(m.group(1)),
                "post_id": int(m.group(2)),
                "order": json_body.get("order", 1),
                "question_type": json_body.get("question_type"),
                "text": json_body.get("text"),
                "config": json_body.get("config"),
                "created_at": now,
            }

        m = re.fullmatch(r"/surveys/(\d+)/questions", path)
        if m and method == "POST":
            self._dry_seq["question"] += 1
            return {
                "id": self._dry_seq["question"],
                "survey_id": int(m.group(1)),
                "post_id": None,
                "order": json_body.get("order", 1),
                "question_type": json_body.get("question_type"),
                "text": json_body.get("text"),
                "config": json_body.get("config"),
                "created_at": now,
            }

        raise CS14ApiError(404, f"dry-run: no stub for {method} {path}")

    def _dry_survey_or_404(self, survey_id: int) -> dict:
        survey = self._dry_store["surveys"].get(survey_id)
        if survey is None:
            raise CS14ApiError(404, "Survey not found")
        return survey

    def _dry_post_or_404(self, post_id: int) -> dict:
        post = self._dry_store["posts"].get(post_id)
        if post is None:
            raise CS14ApiError(404, "Post not found")
        return post
