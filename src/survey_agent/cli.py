"""`uv run survey-agent` entrypoint.

Wires Settings -> Model -> ToolExecutor -> loop.run, and prints round/tool
progress to stderr as it happens. This streams at round/tool granularity
rather than raw token deltas: true token streaming would require a second,
divergent code path in `model.py` (the SDK's streaming API returns deltas,
not the same `ModelResponse` shape), which would break the "loop can't tell
Real/Mock apart" property that makes `--mock` meaningful (DESIGN.md §4, §8).
Round-granularity is also what a human watching a multi-step build chain
actually wants to see: which tool is running, not token-by-token text.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Settings
from .context import HandlerContext, RunContext
from .executor import ToolExecutor
from .http_client import CS14Client
from .loop import RunResult, run as run_loop
from .model import MockModel, RealModel
from .prompts import full_system_prompt
from .tools import TOOLS
from .tools.auth import ensure_researcher
from .tools.schema import anthropic_tools
from .trace import Tracer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="survey-agent", description="Build and publish a cs14 survey from one instruction."
    )
    parser.add_argument("instruction", nargs="?", default="", help="Natural-language build instruction (中/英)")
    parser.add_argument(
        "--mock",
        nargs="?",
        const="__default__",
        default=None,
        metavar="SCRIPT_JSON",
        help="Replay a scripted run instead of calling the model. No argument = built-in demo script.",
    )
    parser.add_argument(
        "--base-url", dest="anthropic_base_url", default=None, help="Override ANTHROPIC_BASE_URL (compatible endpoint/proxy)"
    )
    parser.add_argument("--cs14-base-url", dest="cs14_base_url", default=None, help="Override CS14_BASE_URL")
    parser.add_argument("--model", dest="model", default=None, help="Override the model id")
    parser.add_argument(
        "--trace", dest="trace_path", default=None, help="JSONL trace output path (default: traces/<ts>.jsonl)"
    )
    parser.add_argument("--max-turns", dest="max_turns", type=int, default=None)
    parser.add_argument("--lang", dest="lang", default=None, help="Preferred default_language hint appended to the instruction")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not hit the cs14 backend network — handlers get synthetic responses (see http_client.py's dry-run stub)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = Settings.from_env(
        model=args.model,
        anthropic_base_url=args.anthropic_base_url,
        cs14_base_url=args.cs14_base_url,
        max_turns=args.max_turns,
    )
    settings.trace_path = args.trace_path or f"traces/{int(time.time())}.jsonl"

    if not settings.anthropic_api_key and args.mock is None:
        print(
            "No ANTHROPIC_API_KEY (and no --mock) — either export ANTHROPIC_API_KEY / "
            "run `ant auth login`, or pass --mock to replay a scripted run offline.",
            file=sys.stderr,
        )
        return 2

    client = CS14Client(
        base_url=settings.cs14_base_url,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        retry_max_delay=settings.retry_max_delay,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        try:
            ensure_researcher(client, settings.cs14_email, settings.cs14_password)
        except Exception as exc:  # noqa: BLE001 - CLI top-level: report and exit, don't traceback
            print(f"Could not authenticate against cs14 backend at {settings.cs14_base_url}: {exc}", file=sys.stderr)
            return 2

    run_ctx = RunContext()
    handler_ctx = HandlerContext(client=client, run=run_ctx)
    executor = ToolExecutor(TOOLS, handler_ctx, result_max_chars=settings.tool_result_max_chars)
    tools_schema = anthropic_tools(TOOLS)

    fallback_model = None
    if args.mock is not None:
        script = _build_default_script() if args.mock == "__default__" else _load_script_file(args.mock)
        model = MockModel(script)
    else:
        model = RealModel(settings.model, settings.anthropic_api_key, settings.anthropic_base_url, settings.max_tokens)
        fallback_model = RealModel(
            settings.model_fallback, settings.anthropic_api_key, settings.anthropic_base_url, settings.max_tokens
        )

    tracer = Tracer(settings.trace_path)
    tracer.run_start(args.instruction, settings.model, settings.anthropic_base_url, args.mock is not None)

    instruction = args.instruction
    if args.lang:
        instruction = f"{instruction}\n\n(preferred default language: {args.lang})"

    result = run_loop(
        instruction,
        full_system_prompt(),
        model,
        tools_schema,
        executor,
        handler_ctx,
        settings,
        tracer,
        on_event=_make_progress_printer(),
        fallback_model=fallback_model,
    )
    tracer.close()
    client.close()
    _print_result(result)
    return 0 if result.reason == "done" else 1


def _make_progress_printer():
    def on_event(kind: str, payload: dict) -> None:
        if kind == "round":
            usage = payload["usage"]
            print(
                f"-- turn {payload['turn']} · {usage['input']}in/{usage['output']}out tok · "
                f"{payload['latency_ms']:.0f}ms",
                file=sys.stderr,
            )
        elif kind == "tool_call":
            status = "ERR" if payload["is_error"] else "ok"
            args_preview = json.dumps(payload["input"], ensure_ascii=False)
            result_preview = payload["result"][:200]
            print(f"   [{status}] {payload['name']}({args_preview}) -> {result_preview}", file=sys.stderr)
        elif kind == "model_fallback":
            print(f"-- primary model unavailable, falling back at turn {payload['turn']}", file=sys.stderr)

    return on_event


def _print_result(result: RunResult) -> None:
    print()
    if result.final_text:
        print(result.final_text)
    else:
        print(f"Run ended without a final answer (reason={result.reason}).", file=sys.stderr)
    print(json.dumps(result.state, ensure_ascii=False, indent=2))


def _load_script_file(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("mock_script", [])
    return data


def _build_default_script() -> list:
    """Built-in demo script: bilingual (en/zh-CN) A/B xiaohongshu-style
    survey, one post, one likert question, publish. Uses closures over
    `state` + the `(last_tool_results) -> entry` callable form so it reacts
    to ids the (dry-run or real) backend assigns, instead of hardcoding them.
    """
    state: dict = {}

    def step_create_survey(_last: list[dict]) -> dict:
        return {
            "tool_use": [
                {
                    "name": "create_survey",
                    "input": {
                        "title": "Bilingual A/B demo survey",
                        "platform_style": "xiaohongshu",
                        "platform_ui_style": "xiaohongshu",
                        "num_groups": 2,
                        "group_names": {"1": "with_likes", "2": "no_likes"},
                        "default_language": "en",
                        "supported_languages": ["en", "zh-CN"],
                    },
                }
            ]
        }

    def step_add_post(last: list[dict]) -> dict:
        state["survey_id"] = _extract(last, "survey_id")
        return {
            "tool_use": [
                {
                    "name": "add_post",
                    "input": {
                        "survey_id": state["survey_id"],
                        "original_url": "https://example.com/post-1",
                        "order": 1,
                    },
                }
            ]
        }

    def step_update_post_display(last: list[dict]) -> dict:
        state["post_id"] = _extract(last, "post_id")
        return {
            "tool_use": [
                {
                    "name": "update_post_display",
                    "input": {
                        "survey_id": state["survey_id"],
                        "post_id": state["post_id"],
                        "display_title": "Stimulus post 1",
                        "display_description": "A sample stimulus post for the study.",
                        "show_likes": True,
                        "display_likes": 128,
                        "group_overrides": {"2": {"show_likes": False}},
                    },
                }
            ]
        }

    def step_add_question(_last: list[dict]) -> dict:
        return {
            "tool_use": [
                {
                    "name": "add_survey_question",
                    "input": {
                        "survey_id": state["survey_id"],
                        "question_type": "likert",
                        "text": "How much do you trust this post?",
                        "order": 1,
                        "config": {"min": 1, "max": 5},
                    },
                }
            ]
        }

    def step_publish(_last: list[dict]) -> dict:
        return {"tool_use": [{"name": "publish_survey", "input": {"survey_id": state["survey_id"]}}]}

    def step_share_link(_last: list[dict]) -> dict:
        return {"tool_use": [{"name": "get_share_link", "input": {"survey_id": state["survey_id"]}}]}

    def step_final(last: list[dict]) -> dict:
        link = _extract(last, "share_link") or "(see get_share_link result above)"
        return {"final": f"Published the bilingual A/B demo survey. Share link: {link}"}

    return [
        step_create_survey,
        step_add_post,
        step_update_post_display,
        step_add_question,
        step_publish,
        step_share_link,
        step_final,
    ]


def _extract(last_tool_results: list[dict], key: str):
    for block in last_tool_results:
        try:
            data = json.loads(block.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
