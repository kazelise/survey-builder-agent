"""Tests for scripts/build_handbook_index.py's secret-redaction guard.

Security finding: a real staging password + a real server IP were once
committed into data/handbook_index.json (a deliberately public, checked-in
build artifact) via this exact ingestion path. redact_secrets() is the
second line of defense so re-running this script never reintroduces them.
"""

from __future__ import annotations

from scripts.build_handbook_index import redact_secrets


def test_redacts_password_and_email_table_cells():
    text = (
        "| Field | Value |\n"
        "|-------|-------|\n"
        "| Sign-in URL | `https://cs14.kazelis.top/auth` |\n"
        "| Email | `researcher.demo@example.com` |\n"
        "| Password | `Fak3Password!Demo` |"
    )
    redacted = redact_secrets(text)
    assert "Fak3Password!Demo" not in redacted
    assert "researcher.demo@example.com" not in redacted
    assert "<REDACTED>" in redacted
    # Non-secret rows are left alone.
    assert "https://cs14.kazelis.top/auth" in redacted


def test_redacts_token_and_secret_labeled_rows_case_insensitively():
    text = "| CLOUDFLARE_TUNNEL_TOKEN | `abc123supersecret` |\n| secret | `xyz` |"
    redacted = redact_secrets(text)
    assert "abc123supersecret" not in redacted
    assert "xyz" not in redacted


def test_redacts_public_ipv4_literal():
    text = "e.g. `203.0.113.56.sslip.io`"
    redacted = redact_secrets(text)
    assert "203.0.113.56" not in redacted
    assert "sslip.io" in redacted  # only the address is redacted, not the whole example


def test_does_not_redact_loopback_or_private_ip_examples():
    text = "run it locally on 127.0.0.1, or on a private net at 10.0.0.5 / 192.168.1.5 / 172.20.0.4"
    redacted = redact_secrets(text)
    assert redacted == text  # unchanged: none of these identify a real public server


def test_leaves_ordinary_text_unchanged():
    text = "The backend runs FastAPI + Postgres. See docs/README.md for setup."
    assert redact_secrets(text) == text
