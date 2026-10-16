import asyncio
import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# load_dotenv(override=False) is a local-dev convenience only.
# On the VPS, Coolify injects env vars before process start, so this is a no-op.
load_dotenv(override=False)

from business import (
    ASSISTANT_NAME, BRANCHES, BUSINESS_NAME, CAMPAIGNS, CLOSED_WEEKDAYS, DEFAULT_VOICE, FACTS,
    MISSED_CLASS, MISSED_REASONS, SESSION_TIMES, TRIAL, campaign_or_default,
)
from prompts import build_prompt

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
from db import (
    init_db, log_error, now_local, get_all_settings, get_logs, clear_errors,
    TrialSlotUnavailable, book_trial, get_all_trials, cancel_trial, next_free_trial_slots,
    get_all_calls, get_call, update_call_notes, get_contacts, get_calls_by_phone, get_stats,
    get_missed_classes, delete_missed_class,
    create_call_run, get_call_runs, get_call_run, update_call_run_status, pause_interrupted_runs,
    finish_call_run, delete_call_run,
    create_campaign_contacts, get_pending_campaign_contacts, claim_campaign_contact,
    mark_campaign_contact, count_campaign_contacts_by_status,
    get_contact_memory, add_contact_memory,
    add_dnc, remove_dnc, list_dnc, get_month_to_date_spend,
)
import compliance
from observability import langfuse_status
from recordings import presigned_recording_url, recording_sync_status, sync_vobiz_recordings
import auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

app = FastAPI(title="T-800", version="1.0.0")

# The dashboard is served same-origin, so it needs NO CORS. Only add cross-origin
# access if ALLOWED_ORIGINS is explicitly set (comma-separated). We never combine a
# wildcard origin with credentials — browsers reject it and it defeats the purpose.
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Authentication gate ───────────────────────────────────────────────────────
# Everything is behind the session cookie except this small public allowlist:
# the login endpoints, logout, health check, and the favicon.
PUBLIC_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # CORS preflight and explicitly-public routes bypass the gate.
    if request.method == "OPTIONS" or path in PUBLIC_PATHS:
        return await call_next(request)

    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    user = auth.verify_session_token(token)
    if user:
        request.state.user = user
        return await call_next(request)

    # Unauthenticated: send browsers to the login page, APIs a 401 they can handle.
    accept = request.headers.get("accept", "")
    wants_html = request.method == "GET" and "text/html" in accept
    if wants_html:
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


# ── APScheduler ───────────────────────────────────────────────────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()


# ── Dispatch concurrency + spend guards ───────────────────────────────────────
# Bound the number of calls dispatched at once so a large CSV/campaign cannot blow
# past the SIP trunk's channel count and the model quota, causing mass failures.
try:
    _MAX_CONCURRENT_DISPATCH = max(1, int(os.environ.get("MAX_CONCURRENT_DISPATCH", "5")))
except ValueError:
    _MAX_CONCURRENT_DISPATCH = 5
_dispatch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DISPATCH)

# Monthly hard spend cap (USD) across the instance. 0 => unlimited. Since we host
# and front telephony/model costs, this stops a runaway campaign. Spend is cached
# briefly to avoid summing call_logs on every single dial.
try:
    _MONTHLY_SPEND_CAP_USD = float(os.environ.get("MONTHLY_SPEND_CAP_USD", "0") or 0)
except ValueError:
    _MONTHLY_SPEND_CAP_USD = 0.0
_spend_cache: dict = {"value": 0.0, "at": 0.0}
_SPEND_CACHE_TTL = 60.0


async def _over_spend_cap() -> bool:
    """True if this month's spend has reached the cap. Cached for _SPEND_CACHE_TTL."""
    if _MONTHLY_SPEND_CAP_USD <= 0:
        return False
    import time as _t
    now = _t.monotonic()
    if now - _spend_cache["at"] > _SPEND_CACHE_TTL:
        try:
            _spend_cache["value"] = await get_month_to_date_spend()
        except Exception as exc:
            logger.warning("Spend-cap lookup failed (allowing call): %s", exc)
            return False
        _spend_cache["at"] = now
    return _spend_cache["value"] >= _MONTHLY_SPEND_CAP_USD


async def dispatch_guarded(phone: str, lead_name: str, meta: dict) -> str:
    """Pre-dial compliance/spend/concurrency gate around _dispatch_call.

    Returns one of: 'dispatched' | 'failed' | 'suppressed' | 'blocked_window'
    | 'spend_cap'. Callers map these to per-contact status / single-call errors.
    """
    # 1. Do-Not-Call + calling-window (respects APP_TIMEZONE).
    ok, reason = await compliance.check_dialable(phone)
    if not ok:
        logger.info("Skipping %s — %s", phone, reason)
        await log_error("server", f"Call skipped: {reason}", f"phone={phone}", "warning")
        return "suppressed" if "Do-Not-Call" in reason else "blocked_window"

    # 2. Monthly spend cap.
    if await _over_spend_cap():
        logger.warning("Monthly spend cap reached — refusing to dial %s", phone)
        await log_error("server", "Monthly spend cap reached — call refused", f"phone={phone}", "warning")
        return "spend_cap"

    # 3. Bounded concurrency around the actual LiveKit dispatch.
    async with _dispatch_semaphore:
        ok = await _dispatch_call(phone, lead_name, meta)
    return "dispatched" if ok else "failed"


# ── LiveKit dispatch helper ───────────────────────────────────────────────────

async def _dispatch_call(phone: str, lead_name: str, meta: dict) -> bool:
    """Create a LiveKit room + dispatch agent job. All credentials from os.environ."""
    lk_url    = os.environ.get("LIVEKIT_URL", "")
    lk_key    = os.environ.get("LIVEKIT_API_KEY", "")
    lk_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    trunk_id  = os.environ.get("OUTBOUND_TRUNK_ID", "")

    if not all([lk_url, lk_key, lk_secret, trunk_id]):
        logger.error("Missing LiveKit credentials or OUTBOUND_TRUNK_ID in environment")
        return False

    room_name = f"call-{phone.replace('+', '')}-{uuid.uuid4().hex[:8]}"
    room_meta = json.dumps({
        "phone_number": phone,
        "lead_name": lead_name,
        "trunk_id": trunk_id,
        **meta,
    })

    try:
        from livekit import api as lk_api
        async with lk_api.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret) as lk:
            await lk.room.create_room(
                lk_api.CreateRoomRequest(name=room_name, metadata=room_meta)
            )
            await lk.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(
                    agent_name="outbound-caller",
                    room=room_name,
                    metadata=room_meta,
                )
            )
        logger.info("Dispatched call to %s in room %s", phone, room_name)
        return True

    except Exception as exc:
        logger.error("Dispatch failed for %s: %s", phone, exc)
        await log_error("server", f"Call dispatch failed: {exc}", f"phone={phone}", "error")
        return False


# ── Call-list runner ──────────────────────────────────────────────────────────
# Each uploaded list is a "run" of one of the two campaigns. Contacts are claimed
# one at a time (queued → dialing → done/failed/skipped), so a paused or
# interrupted run resumes where it left off and nobody is called twice.

_running: set = set()
_tasks: set = set()  # strong refs so background runs are not garbage-collected


def _start_run(run_id: str) -> None:
    """Start the runner for a list in the background, unless it is already running."""
    if run_id not in _running:
        task = asyncio.create_task(_run_list(run_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _run_list(run_id: str) -> None:
    if run_id in _running:
        return
    _running.add(run_id)
    try:
        await _run_list_inner(run_id)
    except Exception as exc:
        logger.exception("Call list %s crashed", run_id)
        await log_error("server", f"Call list stopped with an error: {exc}", run_id, "error")
        await update_call_run_status(run_id, "paused")
    finally:
        _running.discard(run_id)


async def _run_list_inner(run_id: str) -> None:
    run = await get_call_run(run_id)
    if not run or run.get("status") != "active":
        return
    campaign = campaign_or_default(run.get("kind"))
    delay = run.get("call_delay_seconds")
    delay = 3 if delay is None else int(delay)
    await log_error("server", f"Call list started: {run.get('name')}", run_id, "info")

    dispatched = failed = skipped = 0
    stopped = False
    for contact in await get_pending_campaign_contacts(run_id):
        # Re-check between contacts so a pause takes effect promptly.
        latest = await get_call_run(run_id)
        if not latest or latest.get("status") != "active":
            stopped = True
            break
        if not await claim_campaign_contact(contact["id"]):
            continue

        meta = {"campaign": campaign, "campaign_id": run_id}
        if contact.get("missed_date"):
            meta["missed_date"] = contact["missed_date"]
        result = await dispatch_guarded(
            (contact.get("phone_number") or "").strip(),
            (contact.get("lead_name") or "there").strip(),
            meta,
        )
        if result == "dispatched":
            dispatched += 1
            await mark_campaign_contact(contact["id"], "done", outcome="dispatched")
        elif result == "spend_cap":
            # Stop the whole run; leave this contact queued so a resume retries it.
            await mark_campaign_contact(contact["id"], "queued", skip_reason="spend_cap")
            await update_call_run_status(run_id, "paused")
            logger.warning("Call list %s paused — monthly spend cap reached", run_id)
            stopped = True
            break
        elif result in ("suppressed", "blocked_window"):
            skipped += 1
            await mark_campaign_contact(contact["id"], "skipped", skip_reason=result)
        else:
            failed += 1
            await mark_campaign_contact(contact["id"], "failed", outcome="dispatch_failed")

        if delay > 0:
            await asyncio.sleep(delay)

    counts = await count_campaign_contacts_by_status(run_id)
    remaining = counts.get("queued", 0)
    if not stopped and remaining == 0:
        await finish_call_run(run_id, counts.get("done", 0), counts.get("failed", 0))
    await log_error(
        "server",
        f"Call list run: dispatched={dispatched} failed={failed} skipped={skipped} remaining={remaining}",
        run_id, "info",
    )

# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    try:
        await auth.init_auth()
    except Exception as exc:
        logger.error("Auth initialisation failed: %s", exc)
    scheduler.start()
    try:
        sync_seconds = max(30, int(os.environ.get("RECORDING_SYNC_SECONDS", "60")))
    except ValueError:
        sync_seconds = 60
    scheduler.add_job(
        sync_vobiz_recordings,
        "interval",
        seconds=sync_seconds,
        id="vobiz_recording_sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=datetime.now(),
    )
    # A restart interrupts any list that was mid-run; show it as paused so it can be resumed.
    try:
        interrupted = await pause_interrupted_runs()
        if interrupted:
            logger.info("Paused %d call list(s) interrupted by the restart", interrupted)
    except Exception as exc:
        logger.warning("Could not pause interrupted call lists: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


# ── Authentication routes ─────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already signed in? Skip the form.
    if auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE_NAME, "")):
        return RedirectResponse(url="/", status_code=302)
    login_path = Path(__file__).parent / "ui" / "login.html"
    if login_path.exists():
        return HTMLResponse(login_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Login</h1><p>ui/login.html not found</p>", status_code=500)


@app.post("/login")
async def login(request: Request):
    ip = auth.client_ip(request)
    if auth.is_rate_limited(ip):
        raise HTTPException(429, f"Too many attempts. Try again in {auth.retry_after_seconds(ip)}s.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not await auth.verify_credentials(username, password):
        auth.record_failure(ip)
        logger.warning("Failed login for '%s' from %s", username, ip)
        raise HTTPException(401, "Invalid username or password")
    auth.reset_failures(ip)
    token = auth.create_session_token(username)
    response = JSONResponse({"ok": True})
    auth.set_session_cookie(response, token)
    return response


@app.post("/logout")
async def logout_post():
    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response)
    return response


@app.get("/logout")
async def logout_get():
    response = RedirectResponse(url="/login", status_code=302)
    auth.clear_session_cookie(response)
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    return {"username": getattr(request.state, "user", None)}


@app.post("/auth/change-password")
async def change_password(request: Request):
    if auth.is_password_env_managed():
        raise HTTPException(
            409,
            "Password is managed via the ADMIN_PASSWORD environment variable. "
            "Change it there and redeploy.",
        )
    body = await request.json()
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""
    if len(new) < 10:
        raise HTTPException(400, "New password must be at least 10 characters")
    username = getattr(request.state, "user", "") or await auth.get_admin_username()
    if not await auth.verify_credentials(username, current):
        raise HTTPException(401, "Current password is incorrect")
    await auth.set_admin_password(new)
    logger.info("Admin password changed")
    # Rotate the session so the current browser stays signed in with a fresh cookie.
    response = JSONResponse({"ok": True})
    auth.set_session_cookie(response, auth.create_session_token(username))
    return response


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    ui_path = Path(__file__).parent / "ui" / "index.html"
    if ui_path.exists():
        return HTMLResponse(ui_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>T-800</h1><p>ui/index.html not found</p>")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from db import APP_TIMEZONE
    return {
        "status": "ok",
        "timestamp": now_local().isoformat(),
        "timezone": APP_TIMEZONE,
        "langfuse": langfuse_status(),
        "recordings": recording_sync_status(),
    }


# ── Single call ───────────────────────────────────────────────────────────────

_DISPATCH_ERRORS = {
    "suppressed": (409, "Number is on the Do-Not-Call list"),
    "blocked_window": (409, "Outside the permitted calling hours"),
    "spend_cap": (409, "Monthly spend cap reached — call refused"),
    "failed": (500, "Failed to dispatch call — check LiveKit credentials and trunk ID"),
}


@app.get("/business")
async def business_info():
    """The fixed business setup (from business.py), so the dashboard never duplicates it."""
    return {
        "name": BUSINESS_NAME, "assistant": ASSISTANT_NAME, "voice": DEFAULT_VOICE,
        "branches": list(BRANCHES), "session_times": list(SESSION_TIMES),
        "closed_days": [_WEEKDAYS[d] for d in sorted(CLOSED_WEEKDAYS)], "facts": FACTS,
        "campaigns": [{"key": k, **v} for k, v in CAMPAIGNS.items()],
        "missed_reasons": MISSED_REASONS,
    }


@app.get("/prompts/{campaign}")
async def campaign_prompt(campaign: str):
    """The exact script Tara is given for a campaign. Per-call details (name, number,
    missed date) are shown as [placeholders]; today's date is filled in at call time."""
    if campaign not in CAMPAIGNS:
        raise HTTPException(404, "Unknown campaign")
    prompt = build_prompt(campaign, "[name]", caller_phone="[their number]", missed_date="[missed date]")
    return {"campaign": campaign, "label": CAMPAIGNS[campaign]["label"], "prompt": prompt}


@app.post("/call/single")
async def call_single(request: Request):
    body = await request.json()
    phone       = (body.get("phone_number") or "").strip()
    lead_name   = (body.get("lead_name") or "").strip() or "there"
    campaign    = campaign_or_default(body.get("campaign"))
    missed_date = (body.get("missed_date") or "").strip()
    if not phone:
        raise HTTPException(400, "phone_number is required")

    meta: dict = {"campaign": campaign}
    if campaign == MISSED_CLASS and missed_date:
        meta["missed_date"] = missed_date
    result = await dispatch_guarded(phone, lead_name, meta)
    if result == "dispatched":
        return {"status": "dispatched", "phone": phone, "campaign": campaign}
    status_code, message = _DISPATCH_ERRORS.get(result, (500, "Call not dispatched"))
    raise HTTPException(status_code, message)


# ── Call lists (one uploaded CSV = one run of a campaign) ─────────────────────

def _parse_call_list(text: str) -> list:
    """Read a CSV of people to call. Needs a phone column; name and missed_date
    (the class they missed) are optional. Header names are case-insensitive."""
    reader = csv.DictReader(io.StringIO(text))
    contacts = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        phone = row.get("phone") or row.get("phone_number") or row.get("mobile") or ""
        if not phone:
            continue
        contacts.append({
            "phone": phone,
            "name": row.get("name") or row.get("lead_name") or "there",
            "missed_date": row.get("missed_date") or row.get("date") or "",
        })
    return contacts


@app.post("/campaigns/upload")
async def upload_call_list(
    file: UploadFile = File(...),
    campaign: str = Form(TRIAL),
    call_delay_seconds: int = Form(3),
):
    contacts = _parse_call_list((await file.read()).decode("utf-8-sig"))
    if not contacts:
        raise HTTPException(400, "The CSV has no rows with a phone number")
    campaign = campaign_or_default(campaign)
    label = CAMPAIGNS[campaign]["label"]
    name = f"{label} — {file.filename or 'call list'} ({now_local().strftime('%d %b %H:%M')})"
    run_id = await create_call_run(name, campaign, max(0, int(call_delay_seconds)))
    loaded = await create_campaign_contacts(run_id, contacts)
    _start_run(run_id)
    return {"id": run_id, "contacts_loaded": loaded, "status": "running"}


@app.get("/campaigns")
async def list_call_runs():
    runs = await get_call_runs()
    for run in runs:
        counts = await count_campaign_contacts_by_status(run["id"])
        run["counts"] = counts
        run["total"] = sum(counts.values())
        run["running"] = run["id"] in _running
    return runs


@app.patch("/campaigns/{run_id}/status")
async def set_call_run_status(run_id: str, request: Request):
    body = await request.json()
    status = body.get("status", "")
    if status not in ("active", "paused"):
        raise HTTPException(400, "status must be active | paused")
    if not await update_call_run_status(run_id, status):
        raise HTTPException(404, "Call list not found")
    if status == "active":
        _start_run(run_id)
    return {"status": status}


@app.delete("/campaigns/{run_id}")
async def remove_call_run(run_id: str):
    await update_call_run_status(run_id, "paused")  # stop the runner before deleting
    if not await delete_call_run(run_id):
        raise HTTPException(404, "Call list not found")
    return {"status": "deleted"}


# ── Trial bookings ────────────────────────────────────────────────────────────

@app.get("/appointments")
async def list_appointments(date: Optional[str] = Query(None)):
    return await get_all_trials(date_filter=date)


@app.post("/appointments")
async def create_appointment(request: Request):
    body = await request.json()
    for f in ("name", "phone", "date", "time", "location"):
        if not body.get(f):
            raise HTTPException(400, f"{f} is required")
    try:
        result = await book_trial(body["name"], body["phone"], body["date"], body["time"], body["location"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TrialSlotUnavailable as exc:
        alternatives = await next_free_trial_slots(body["date"], body["time"], body["location"])
        raise HTTPException(409, {
            "message": "That slot is already booked",
            "alternatives": alternatives,
        }) from exc
    return {"booking_id": result["booking_id"], "status": "booked"}


@app.delete("/appointments/{appointment_id}")
async def cancel_appt(appointment_id: str):
    ok = await cancel_trial(appointment_id)
    if not ok:
        raise HTTPException(404, "Booking not found or already cancelled")
    return {"status": "cancelled"}


# ── Missed classes ────────────────────────────────────────────────────────────

@app.get("/missed-classes")
async def list_missed_classes():
    return await get_missed_classes()


@app.delete("/missed-classes/{row_id}")
async def remove_missed_class(row_id: str):
    if not await delete_missed_class(row_id):
        raise HTTPException(404, "Record not found")
    return {"status": "deleted"}

# ── Call logs ─────────────────────────────────────────────────────────────────

@app.get("/calls")
async def list_calls(
    page: int = Query(1), limit: int = Query(20), campaign: Optional[str] = Query(None),
):
    return await get_all_calls(page=page, limit=limit, campaign=campaign or None)


@app.post("/calls/recordings/sync")
async def sync_call_recordings():
    """Manually trigger Vobiz-to-S3 reconciliation."""
    try:
        return await sync_vobiz_recordings()
    except Exception as exc:
        logger.exception("Manual recording sync failed")
        raise HTTPException(502, f"Recording sync failed: {exc}") from exc


@app.get("/calls/{call_id}/recording")
async def play_call_recording(call_id: str):
    call = await get_call(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    recording_ref = call.get("recording_url") or ""
    if not recording_ref:
        raise HTTPException(404, "Recording is not available yet")
    try:
        url = await presigned_recording_url(recording_ref, expires_seconds=900)
    except Exception as exc:
        logger.exception("Could not sign recording URL for call %s", call_id)
        raise HTTPException(502, "Could not prepare recording playback") from exc
    return RedirectResponse(url=url, status_code=307)


@app.patch("/calls/{call_id}/notes")
async def patch_call_notes(call_id: str, request: Request):
    body = await request.json()
    notes = body.get("notes", "")
    ok = await update_call_notes(call_id, notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# ── CRM / Contacts ────────────────────────────────────────────────────────────

@app.get("/contacts")
async def list_contacts():
    return await get_contacts()


@app.get("/contacts/{phone}/history")
async def contact_history(phone: str):
    calls = await get_calls_by_phone(phone)
    memories = await get_contact_memory(phone)
    return {"phone": phone, "calls": calls, "memories": memories}


@app.post("/contacts/{phone}/memory")
async def add_memory(phone: str, request: Request):
    body = await request.json()
    insight = body.get("insight", "").strip()
    if not insight:
        raise HTTPException(400, "insight is required")
    await add_contact_memory(phone, insight)
    return {"status": "saved"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats():
    return await get_stats()

# ── Settings (read-only view of env) ───────────────────────────────────────────────────────────

@app.get("/settings")
async def get_settings():
    return await get_all_settings()


# ── Do-Not-Call list ──────────────────────────────────────────────────────────

@app.get("/dnc")
async def get_dnc():
    return await list_dnc()


@app.post("/dnc")
async def add_to_dnc(request: Request):
    body = await request.json()
    phone = (body.get("phone") or body.get("phone_number") or "").strip()
    if not phone:
        raise HTTPException(400, "phone is required")
    try:
        entry_id = await add_dnc(
            phone,
            reason=(body.get("reason") or "manual").strip(),
            source="dashboard",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": entry_id, "status": "suppressed"}


@app.delete("/dnc/{entry_id}")
async def delete_from_dnc(entry_id: str):
    ok = await remove_dnc(entry_id)
    if not ok:
        raise HTTPException(404, "DNC entry not found")
    return {"status": "removed"}


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
async def list_logs(
    level:  Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit:  int           = Query(200),
):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/logs")
async def delete_logs():
    await clear_errors()
    return {"status": "cleared"}
