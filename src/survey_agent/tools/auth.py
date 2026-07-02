"""Login/register bootstrap.

Not a model tool — the CLI calls this once before the first loop turn so
`create_survey` and friends never have to carry credentials through tool
arguments the model could mishandle.
"""

from __future__ import annotations

from ..http_client import CS14Client


def ensure_researcher(client: CS14Client, email: str, password: str, name: str | None = None) -> str:
    return client.ensure_researcher(email, password, name)
