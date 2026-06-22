# Self-hosted LiveKit (Mumbai) — deploy runbook

Moves T-800 off LiveKit Cloud (US) onto a self-hosted LiveKit **on the same Mumbai VPS**,
removing the India↔US round-trip that dominates call latency. Phone-only setup (no browser
clients), so **no public domain / TLS is required** — the agent talks to LiveKit over the
local network, and only the **SIP ports** face the internet (for Vobiz).

**Topology** (all in `docker-compose.yml`, host networking):
```
Caller → Vobiz → [ sip :5060 + RTP 10000-20000 ] ─┐         (only this faces the internet)
                                                   ├─ localhost ─ livekit :7880 (SFU)
your app (agent + FastAPI) ── ws://172.17.0.1:7880 ┘             ↕ redis :6379 (loopback)
```

> ⚠️ Honest heads-up: SIP self-hosting is the fiddly part. **Keep your LiveKit Cloud creds
> handy** — rollback is just switching the app's env back (Step 6). Test fully before cutover.

---

## Step 0 — Generate an API key/secret pair
Any strings work; make the secret long and random. Run locally:
```bash
echo "KEY=API$(openssl rand -hex 6)"
echo "SECRET=$(openssl rand -hex 32)"
```
You'll use this **same pair** in the LiveKit stack **and** the app. Call them `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.

## Step 1 — Firewall (Hostinger / ufw)
Open to the internet **only** what Vobiz needs; keep everything else local:
```bash
ufw allow 5060/udp        # SIP signaling   (add 5060/tcp if Vobiz uses TCP)
ufw allow 10000:20000/udp # RTP media
# Do NOT open 7880, 7881, 50000-50100, or 6379 — they're localhost-only.
```
Tighten further by restricting 5060/RTP to Vobiz's signaling/media IPs (and set `allowed_addresses` in `trunks/inbound-trunk.json`).

## Step 2 — Deploy the stack in Coolify
1. New Resource → **Docker Compose** → point it at this repo, compose path `deploy/livekit/docker-compose.yml`.
2. Add environment variables: `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (from Step 0). Coolify substitutes them into the config bodies.
3. Deploy. The three containers run with **host networking** (already set in the compose).
4. (Production) pin image tags — replace `:latest` with specific releases from
   <https://github.com/livekit/livekit/releases> and <https://github.com/livekit/sip/releases>.

## Step 3 — Verify the server is reachable (from the VPS shell)
Install the CLI (`lk`) — <https://github.com/livekit/livekit-cli> — then:
```bash
export LIVEKIT_URL=ws://127.0.0.1:7880
export LIVEKIT_API_KEY=...        # from Step 0
export LIVEKIT_API_SECRET=...
lk room list                       # should return (empty) without auth errors
```
Check `docker logs` of the `sip` container shows it connected to LiveKit + Redis.

## Step 4 — Create the SIP trunks + dispatch rule
Fill the `REPLACE_WITH_*` placeholders in `trunks/*.json` with your Vobiz values
(`VOBIZ_SIP_DOMAIN`, `VOBIZ_OUTBOUND_NUMBER`, `VOBIZ_USERNAME`, `VOBIZ_PASSWORD`) and your DID,
then (command names vary by CLI version — check `lk sip --help`):
```bash
lk sip outbound create trunks/outbound-trunk.json   # ← copy the returned ST_… id
lk sip inbound  create trunks/inbound-trunk.json
lk sip dispatch create trunks/dispatch-rule.json     # routes inbound → room → dispatches outbound-caller
```
The **outbound** trunk's `ST_…` id becomes your app's `OUTBOUND_TRUNK_ID`.

Optional inbound hardening — add to `trunks/inbound-trunk.json` only if Vobiz requires it:
```jsonc
"trunk": {
  "name": "Vobiz Inbound",
  "numbers": ["+918049280363"],
  "allowed_addresses": ["<VOBIZ_SIGNALING_IP>/32"],   // accept INVITEs only from Vobiz
  "auth_username": "<user>", "auth_password": "<pass>" // only if Vobiz authenticates inbound
}
```

## Step 5 — Point Vobiz at your server
- **Inbound:** in the Vobiz portal, route your DID (+918049280363) to `sip:<VPS_PUBLIC_IP>:5060`.
- **Outbound:** the outbound trunk above authenticates to Vobiz with your username/password; confirm
  Vobiz allows calls from your server IP.

## Step 6 — Switch the app over (the cutover)
On the **app** resource in Coolify, change these env vars and redeploy:
```
LIVEKIT_URL=ws://172.17.0.1:7880     # docker0 gateway → host. (host.docker.internal:7880 also works if host-gateway is set; or run the app with network_mode: host and use 127.0.0.1)
LIVEKIT_API_KEY=...                  # the Step 0 pair
LIVEKIT_API_SECRET=...
OUTBOUND_TRUNK_ID=ST_…               # from Step 4
```
`VOBIZ_*` now feed the SIP trunk config (Step 4), not the app at runtime. No code or Dockerfile
change is needed — the app just points at the new URL.

## Step 7 — Test & measure
- **Outbound:** dispatch a single call from the dashboard → confirm it rings and the agent talks.
- **Inbound:** call the DID → confirm the brand greeting answers (your DID→brand routing still applies).
- **Latency:** read the worker's `metrics_collected` logs — you should see a clear drop now that
  media + model + SFU are co-located (no trans-Pacific hop).

## Rollback
Set the app's `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` / `OUTBOUND_TRUNK_ID` back to
the LiveKit Cloud values and redeploy. The self-hosted stack can keep running idle.

---

### Notes
- **No TLS/domain needed** because there are no browser/WebRTC clients — only phone (SIP) + a local
  agent. If you ever add a web client, you'll need WSS via a reverse proxy in front of `:7880`.
- **`use_external_ip`:** `false` on the SFU (all its WebRTC peers are local → avoids hairpin-NAT
  breakage), `true` on the SIP service (it must advertise the public IP to Vobiz for RTP).
- **Redis** is loopback-only and unauthenticated by design (not reachable off-box). If you later
  split services across hosts, add a Redis password + private networking.
- **Capacity:** this co-locates the SFU with your app on 2 vCPU. Fine for your ~9-call target with the
  CPU-isolation guidance from the architecture notes; give the `livekit` container a CPU reservation
  so a Python spike can't starve the media threads.
