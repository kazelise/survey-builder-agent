"""Tracer tests: JSONL writing, and I/O-failure resilience.

Tracing is an explicit side-channel (trace.py's module docstring: "grep-able
/ tailable ... diffable") — a disk-full, permission, NFS, or log-rotation
hiccup on the trace file must never crash an otherwise-successful agent run.
"""

from __future__ import annotations

import json

from survey_agent.trace import Tracer


def test_round_and_tool_call_write_jsonl_lines(tmp_path):
    path = tmp_path / "run.jsonl"
    tracer = Tracer(str(path))
    tracer.run_start("hi", "claude-opus-4-8", None, False)
    tracer.round(0, 1, 13, {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0}, 12.3, 0.001)
    tracer.tool_call("create_survey", {"title": "t"}, '{"id": 1}', False, 5.0)
    tracer.close()

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["run_start", "round", "tool_call"]


def test_write_failure_is_swallowed_not_raised(tmp_path):
    # Regression: Tracer._write had zero exception handling. Closing the
    # underlying file handle out from under it (a stand-in for a
    # disk-full/permission/NFS hiccup) used to raise
    # `ValueError: I/O operation on closed file.` straight out of
    # tracer.round()/tool_call()/run_summary(), which would abort an
    # otherwise-successful agent run (loop.py calls these unguarded).
    path = tmp_path / "run.jsonl"
    tracer = Tracer(str(path))
    tracer._fh.close()  # simulate an I/O failure on the underlying handle

    # None of these must raise.
    tracer.round(0, 1, 1, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}, 0.0, 0.0)
    tracer.tool_call("create_survey", {}, "{}", False, 0.0)
    tracer.run_summary(reason="done", turns=1)
    tracer.close()  # must not raise either, even though _fh is already closed


def test_write_failure_is_reported_once_on_stderr_not_spammed(tmp_path, capsys):
    path = tmp_path / "run.jsonl"
    tracer = Tracer(str(path))
    tracer._fh.close()

    tracer.round(0, 1, 1, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}, 0.0, 0.0)
    tracer.tool_call("create_survey", {}, "{}", False, 0.0)
    tracer.run_summary(reason="done", turns=1)

    err = capsys.readouterr().err
    assert err.count("[trace]") == 1  # warned once, not once per failed write


def test_tracer_with_no_path_is_a_safe_no_op():
    tracer = Tracer(None)
    tracer.run_start("hi", "m", None, True)
    tracer.round(0, 1, 1, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}, 0.0, 0.0)
    tracer.close()
