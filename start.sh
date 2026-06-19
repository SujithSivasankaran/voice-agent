#!/bin/bash
# Production startup — environment variables are injected by Coolify (VPS).
# Never source a .env file here; the VPS env is the single source of truth.
set -e
cd "$(dirname "$0")"

echo "==> OutboundAI starting up"
echo "    LiveKit : ${LIVEKIT_URL}"
echo "    Gemini  : ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
echo "    Supabase: ${SUPABASE_URL}"

echo "==> FastAPI on :8000"
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

sleep 2

echo "==> LiveKit agent worker"
python agent.py start

kill "$SERVER_PID" 2>/dev/null || true
