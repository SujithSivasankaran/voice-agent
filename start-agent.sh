#!/bin/bash
# Voice agent worker — the LiveKit job process that handles each call.
# Runs as its own service so voice capacity scales (and restarts) independently of
# the web/API service. Migrations are owned by the web service; the advisory lock in
# migrate.py makes it safe even if both run it, but we skip it here to keep one owner.
set -e
cd "$(dirname "$0")"

echo "==> LiveKit agent worker"
exec python agent.py start
