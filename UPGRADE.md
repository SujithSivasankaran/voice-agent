# Simplified to Harry's Fitcamp only (October 2026)

The app now serves **one business with two fixed campaigns**. There is no brand
editor, no agent profiles, no AI-written campaign scripts and no scheduling.

| Campaign | What Tara does | Where results show |
|---|---|---|
| **Trial booking** | Calls new leads, explains Harry's, books a free one-hour trial | Trial bookings |
| **Missed class** | Calls members who missed class, asks why, records the reason, encourages regular attendance | Missed classes |

Incoming calls still work: Tara answers as the front desk and can book trials.

**Where things live now**
- `business.py` — name, branches (ADAYAR, ECR), trial start times, closed days, facts, voice.
- `prompts.py` — the trial, missed-class and incoming-call scripts.
- To change any of it, edit those files and redeploy.

**Using it** — *Campaigns* page: call one person, or upload a CSV (`phone`, `name`,
and for missed class an optional `missed_date`). Each upload becomes a call list
you can pause, resume or delete. A restart pauses any list that was mid-run;
press Resume to carry on — nobody is called twice.

**Deploying** — migration `0004_fitcamp_campaigns.sql` runs automatically on deploy
(it only adds columns, a `missed_classes` table and a one-booking-per-slot index;
nothing is deleted). Old brand/profile tables stay in the database, unused.

---

# Productization changes — per-customer deploy

This release moves the app from a hand-operated single-tenant tool toward something
you can stamp out as an **isolated instance per customer** and upgrade safely across
a fleet. Summary of what changed and what you must do to deploy it.

## What changed

### 1. Per-deploy timezone (`APP_TIMEZONE`)
All timestamps, "today" boundaries, booking-window checks, and the new calling-hour
rules now use the instance's configured IANA timezone instead of a hardcoded
`Asia/Kolkata`. Set `APP_TIMEZONE` per customer (e.g. `America/New_York`). The
dashboard reads it from `/health` and formats times accordingly; it falls back to
the browser's zone until that resolves.

### 2. Versioned migrations (`migrate.py` + `migrations/`)
Schema changes are now ordered SQL files applied exactly once and tracked in a
`schema_migrations` table, so every instance converges automatically on deploy —
no more pasting SQL into the Supabase console.

- DDL needs a **direct Postgres connection**: set `DATABASE_URL` (or
  `SUPABASE_DB_URL`) to the Supabase URI connection string.
- Without a DB URL, `migrate.py` is a safe no-op and you fall back to running the
  SQL by hand (`supabase_schema.sql` still works).
- `0001_baseline.sql` is the existing schema (idempotent), so applying it to an
  already-provisioned database changes nothing.
- Run manually with `python migrate.py` or `python migrate.py --status`. The start
  scripts run it automatically.

### 3. Telephony compliance (legal exposure since we host + dial)
- **Do-Not-Call list** (`dnc_list`, migration `0002`). Numbers on it are never
  dialed. Managed via the API: `GET/POST /dnc`, `DELETE /dnc/{id}`. Global or
  per-brand scope. The agent gets a `mark_do_not_call` tool so it records an
  opt-out the moment a caller asks to stop being contacted.
- **Calling window**: no dial is placed outside `CALLING_WINDOW_START..END` (local
  time, may wrap past midnight) or on `CALLING_BLOCKED_DAYS`.

### 4. Dispatch safety caps
- `MAX_CONCURRENT_DISPATCH` bounds simultaneous calls so a big campaign can't blow
  past the SIP trunk channel count / model quota.
- `MONTHLY_SPEND_CAP_USD` refuses new dials and stops campaigns once the instance's
  month-to-date estimated spend hits the cap. `0` = unlimited.
- Both are enforced in a single `dispatch_guarded()` gate used by single, batch,
  and campaign calls.

### 5. Resumable, idempotent campaigns (`campaign_contacts`, migration `0003`)
Each (campaign, contact) now has an explicit state row
(`queued → dialing → done/failed/skipped`). A contact is claimed atomically before
dialing, so a restart mid-run **resumes** instead of re-dialing everyone, and a
number is never dialed twice for the same campaign. Progress:
`GET /campaigns/{id}/progress`. Old campaigns are seeded lazily from their existing
contact list on first run.

### 6. Split web / agent processes
The API and the LiveKit agent worker now run as **separate services** so voice
capacity scales and restarts independently:
- `docker compose up -d --build` runs both (`start-web.sh`, `start-agent.sh`).
- `docker compose up -d --scale agent=3` adds voice capacity.
- The combined `start.sh` still works for a single-container/legacy box.

### 7. Tests
`tests/` holds a pytest suite for the pure-logic pieces (calling window, phone
normalisation, migration discovery). Run: `pip install -r requirements-dev.txt`
then `pytest`.

## Deploy / upgrade checklist for an instance

1. Set new env vars (see `.env.example`): `APP_TIMEZONE`, `DATABASE_URL`,
   `CALLING_WINDOW_START/END`, `CALLING_BLOCKED_DAYS`, `MAX_CONCURRENT_DISPATCH`,
   `MONTHLY_SPEND_CAP_USD`.
2. Deploy the image. Migrations run automatically on start (or run
   `python migrate.py` once). Verify with `python migrate.py --status`.
3. Confirm `/health` shows the correct `timezone`.
4. Import/verify the customer's DNC list and confirm the calling window matches
   their market's rules.

## Still open (next phases — not in this change)
- Provisioning automation to stand up a new customer instance (Terraform/Ansible).
- Fleet control plane (health/spend/version across instances) + alerting.
- Recording consent capture + retention TTL + region pinning.
- Move secrets out of the DB write path; CSRF tokens; general API rate limiting.
