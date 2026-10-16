#!/bin/bash
# Web/API service — dashboard + REST API + campaign scheduler.
# Runs DB migrations first (idempotent, advisory-locked), then serves FastAPI in the
# foreground so the container's lifecycle tracks the server process.
set -e
cd "$(dirname "$0")"

echo "==> Applying database migrations"
python migrate.py || echo "    (migrations skipped or failed — see logs above)"

echo "==> FastAPI on :8000"
exec uvicorn server:app --host 0.0.0.0 --port 8000
