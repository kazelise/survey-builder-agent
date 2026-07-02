#!/usr/bin/env bash
# Offline demo: proves the full build chain (create -> post -> display ->
# question -> publish -> share link) with zero backend and zero API key,
# using --mock (scripted model decisions) + --dry-run (stubbed HTTP).
#
# To demo against a real backend: drop --dry-run and start the cs14 backend
# first (`docker compose up -d` from the repo root, or `uvicorn app.main:app`
# from backend/). To demo against a real model: drop --mock and export
# ANTHROPIC_API_KEY.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p traces

echo "=== 1/1: built-in bilingual A/B demo script (mock decisions, dry-run backend) ==="
uv run survey-agent "" --mock --dry-run --trace "traces/demo-$(date +%s).jsonl"
