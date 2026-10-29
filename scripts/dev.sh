#!/usr/bin/env bash
# Run locally with long polling (no public URL or tunnel needed).
set -euo pipefail
cd "$(dirname "$0")/.."
export POLLING=1
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
