#!/usr/bin/env bash
# One-shot SIP setup for the self-hosted LiveKit server.
# Creates: Vobiz OUTBOUND trunk + INBOUND trunk + inbound→agent dispatch rule,
# then prints the OUTBOUND_TRUNK_ID to put on your app resource (step 7).
# Re-runnable. Requires the `lk` CLI: https://github.com/livekit/livekit-cli
#
# Run on the VPS (or anywhere that can reach the server) with these env vars set:
#   LIVEKIT_API_KEY / LIVEKIT_API_SECRET   self-hosted pair (same as the LiveKit stack)
#   VOBIZ_SIP_DOMAIN                        outbound trunk address (Vobiz SIP host)
#   VOBIZ_USERNAME / VOBIZ_PASSWORD         Vobiz SIP auth
#   VOBIZ_OUTBOUND_NUMBER                   caller-ID for outbound (e.g. +91...)
#   INBOUND_DID                            your DID (default below)
# Optional:
#   LIVEKIT_URL          (default ws://127.0.0.1:7880 — run this on the VPS)
#   AGENT_NAME           (default outbound-caller)
#   VOBIZ_SIGNALING_IP   restrict inbound INVITEs to Vobiz's IP (recommended)
#
# Example:
#   export LIVEKIT_API_KEY=API... LIVEKIT_API_SECRET=...
#   export VOBIZ_SIP_DOMAIN=sip.vobiz... VOBIZ_USERNAME=... VOBIZ_PASSWORD=...
#   export VOBIZ_OUTBOUND_NUMBER=+9144... INBOUND_DID=+918049280363
#   bash deploy/livekit/bootstrap-sip.sh

set -euo pipefail

export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
AGENT_NAME="${AGENT_NAME:-outbound-caller}"
INBOUND_DID="${INBOUND_DID:-+918049280363}"

req() { [ -n "${!1:-}" ] || { echo "ERROR: env var $1 is required" >&2; exit 1; }; }
for v in LIVEKIT_API_KEY LIVEKIT_API_SECRET VOBIZ_SIP_DOMAIN VOBIZ_USERNAME VOBIZ_PASSWORD VOBIZ_OUTBOUND_NUMBER; do req "$v"; done
command -v lk >/dev/null || { echo "ERROR: 'lk' CLI not found — install from https://github.com/livekit/livekit-cli" >&2; exit 1; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/outbound.json" <<JSON
{ "trunk": { "name": "Vobiz Outbound", "address": "$VOBIZ_SIP_DOMAIN",
  "numbers": ["$VOBIZ_OUTBOUND_NUMBER"],
  "auth_username": "$VOBIZ_USERNAME", "auth_password": "$VOBIZ_PASSWORD" } }
JSON

ALLOWED=""
[ -n "${VOBIZ_SIGNALING_IP:-}" ] && ALLOWED="\"allowed_addresses\": [\"${VOBIZ_SIGNALING_IP}/32\"],"
cat > "$tmp/inbound.json" <<JSON
{ "trunk": { "name": "Vobiz Inbound", $ALLOWED "numbers": ["$INBOUND_DID"] } }
JSON

cat > "$tmp/dispatch.json" <<JSON
{ "name": "inbound-to-agent", "trunk_ids": [],
  "rule": { "dispatchRuleIndividual": { "roomPrefix": "inbound-call" } },
  "roomConfig": { "agents": [ { "agentName": "$AGENT_NAME" } ] } }
JSON

echo "==> Server: $LIVEKIT_URL"
echo "==> Creating OUTBOUND trunk (Vobiz)…"
OUT="$(lk sip outbound create "$tmp/outbound.json")"; echo "$OUT"
TRUNK_ID="$(printf '%s' "$OUT" | grep -oE 'ST_[A-Za-z0-9]+' | head -n1 || true)"

echo "==> Creating INBOUND trunk (DID $INBOUND_DID)…"
lk sip inbound create "$tmp/inbound.json"

echo "==> Creating DISPATCH rule (→ agent '$AGENT_NAME')…"
lk sip dispatch create "$tmp/dispatch.json"

echo
echo "✅ SIP setup complete."
if [ -n "$TRUNK_ID" ]; then
  echo "   OUTBOUND_TRUNK_ID=$TRUNK_ID   ← set this on your APP resource, then redeploy (step 7)"
else
  echo "   ⚠ Could not auto-detect the trunk id — run 'lk sip outbound list' and copy the ST_… value."
fi
echo "   Verify: lk sip outbound list && lk sip inbound list && lk sip dispatch list"
