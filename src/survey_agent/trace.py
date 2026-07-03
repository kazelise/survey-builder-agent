"""JSONL trace writer — one event per line, so a run is grep-able / tailable
while it's happening, and diffable across runs. Event catalog: DESIGN.md §9.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._fh = None
        self._write_failed = False
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")

    def _write(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()  # a crash mid-run should still leave a readable trace
        except (OSError, ValueError) as exc:
            # Tracing is a side-channel (module docstring), not part of the
            # build chain's correctness — a disk-full, permission, NFS, or
            # log-rotation hiccup on the trace file must never abort an
            # otherwise-successful run. Warn once (so the failure isn't
            # silent) and keep going without tracing for the rest of the run.
            if not self._write_failed:
                self._write_failed = True
                print(f"[trace] disabled after write failure: {type(exc).__name__}: {exc}", file=sys.stderr)

    def run_start(self, instruction: str, model: str, base_url: str | None, mock: bool) -> None:
        self._write(
            {
                "event": "run_start",
                "instruction": instruction,
                "model": model,
                "base_url": base_url,
                "mock": mock,
            }
        )

    def round(
        self,
        turn: int,
        message_count: int,
        tool_count: int,
        usage: dict[str, int],
        latency_ms: float,
        est_cost_usd: float,
    ) -> None:
        self._write(
            {
                "event": "round",
                "turn": turn,
                "model_input_digest": {"message_count": message_count, "tool_count": tool_count},
                "usage": usage,
                "latency_ms": round(latency_ms, 1),
                "est_cost_usd": round(est_cost_usd, 6),
            }
        )

    def tool_call(
        self, name: str, input_digest: Any, result_digest: str, is_error: bool, duration_ms: float
    ) -> None:
        self._write(
            {
                "event": "tool_call",
                "name": name,
                "input": input_digest,
                # result_digest is already char-capped by executor.py; trim again
                # here so a single tool call can't blow up the trace file.
                "result": result_digest[:1000],
                "is_error": is_error,
                "duration_ms": round(duration_ms, 1),
            }
        )

    def run_summary(self, **fields: Any) -> None:
        self._write({"event": "run_summary", **fields})

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError as exc:
                print(f"[trace] error closing trace file: {exc}", file=sys.stderr)
