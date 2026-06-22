import asyncio
import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# load_dotenv(override=False) is a local-dev convenience only.
# On the VPS, Coolify injects env vars before process start, so this is a no-op.
load_dotenv(override=False)

from db import (
    init_db, log_error,
    get_all_settings, save_settings, get_setting, set_setting,
    get_default_prompt, save_default_prompt,
    get_default_feedback, append_default_feedback,
    get_errors, get_logs, clear_errors,
    TrialSlotUnavailable, insert_trial, get_all_trials, cancel_trial,
    get_next_available_trial_slots,
    get_all_calls, get_call, update_call_notes, get_contacts, get_calls_by_phone,
    get_stats,
    create_campaign, get_all_campaigns, get_campaign, update_campaign_status,
    update_campaign_run_stats, delete_campaign,
    get_active_campaigns, update_campaign_generated, set_campaign_prompt_status,
    append_campaign_feedback,
    get_contact_memory, add_contact_memory,
    get_all_agent_profiles, get_agent_profile, get_default_agent_profile, create_agent_profile,
    update_agent_profile, delete_agent_profile, set_default_agent_profile,
    get_all_brands, get_brand, get_default_brand, create_brand,
    update_brand, delete_brand, set_default_brand,
)
from observability import langfuse_status
from recordings import presigned_recording_url, recording_sync_status, sync_vobiz_recordings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

app = FastAPI(title="T-800", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── APScheduler ───────────────────────────────────────────────────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

scheduler = AsyncIOScheduler()
_scheduled_campaign_jobs: dict = {}


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


# ── Campaign prompt generation (background, LLM) ───────────────────────────────

async def _generate_campaign_assets(name: str, purpose: str, feedback_items: list, default_base: str = None):
    """Turn a campaign's purpose (+ cumulative feedback) into a full outbound script
    and a short summary, via the LLM. Returns (prompt, summary) or (None, None).
    default_base is the brand's outbound script used as the style reference."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or not purpose:
        return None, None
    from prompts import CAMPAIGN_GEN_INSTRUCTIONS, DEFAULT_SYSTEM_PROMPT
    fb = "\n".join(f"- {f.get('text','')}" for f in (feedback_items or [])) or "(none yet)"
    base = (default_base or DEFAULT_SYSTEM_PROMPT)[:2200]
    instr = CAMPAIGN_GEN_INSTRUCTIONS.format(
        default_base=base, name=name, purpose=purpose, feedback=fb,
    )
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        model = os.environ.get("CAMPAIGN_GEN_MODEL", "gemini-2.5-flash")
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: client.models.generate_content(model=model, contents=instr)
        )
        text = (resp.text or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
            return data.get("prompt"), data.get("summary")
    except Exception as exc:
        logger.error("Campaign prompt generation failed: %s", exc)
        await log_error("server", f"Campaign prompt generation failed: {exc}", name, "error")
    return None, None


async def _regenerate_default_prompt() -> None:
    """Revise the default base script from its cumulative feedback (background)."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return
    feedback = await get_default_feedback()
    if not feedback:
        return
    from prompts import DEFAULT_REVISE_INSTRUCTIONS, DEFAULT_SYSTEM_PROMPT
    current = await get_default_prompt() or DEFAULT_SYSTEM_PROMPT
    fb = "\n".join(f"- {f.get('text','')}" for f in feedback)
    instr = DEFAULT_REVISE_INSTRUCTIONS.format(current=current, feedback=fb)
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        model = os.environ.get("CAMPAIGN_GEN_MODEL", "gemini-2.5-flash")
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: client.models.generate_content(model=model, contents=instr)
        )
        text = (resp.text or "").strip()
        if text:
            await save_default_prompt(text)
            logger.info("Default prompt revised from feedback")
    except Exception as exc:
        logger.error("Default prompt revision failed: %s", exc)
        await log_error("server", f"Default prompt revision failed: {exc}", "default", "error")


async def _regenerate_campaign(campaign_id: str, pending_status: str = "generating") -> None:
    """Background task: (re)generate a campaign's outbound prompt + summary."""
    c = await get_campaign(campaign_id)
    if not c or not c.get("purpose"):
        return
    await set_campaign_prompt_status(campaign_id, pending_status)
    feedback = []
    if c.get("feedback"):
        try:
            feedback = json.loads(c["feedback"])
        except Exception:
            feedback = []
    # Use the campaign's brand outbound script as the style reference, so
    # generated scripts match that brand rather than the global Harry's default.
    brand_base = None
    if c.get("brand_id"):
        try:
            brand = await get_brand(c["brand_id"])
            brand_base = (brand or {}).get("outbound_prompt") or None
        except Exception:
            brand_base = None
    prompt, summary = await _generate_campaign_assets(
        c.get("name", ""), c.get("purpose", ""), feedback, brand_base,
    )
    if prompt:
        await update_campaign_generated(campaign_id, prompt, summary or "", "ready")
        logger.info("Campaign %s prompt regenerated", campaign_id)
    else:
        await set_campaign_prompt_status(campaign_id, "error")


# ── Campaign runner ───────────────────────────────────────────────────────────

async def _assemble_campaign_call_prompt(campaign: dict) -> str:
    """Compact primary campaign prompt; other campaigns are fetched on demand."""
    from prompts import assemble_outbound_prompt
    return assemble_outbound_prompt(
        campaign.get("name") or "Campaign",
        campaign.get("summary") or "",
        campaign.get("purpose") or "",
    )


async def _default_outbound_profile_id() -> Optional[str]:
    """The profile marked default is authoritative for every outbound call."""
    try:
        profile = await get_default_agent_profile()
        return profile.get("id") if profile else None
    except Exception as exc:
        logger.warning("Could not load default outbound agent profile: %s", exc)
        return None


async def _resolve_call_brand_id(brand_id: Optional[str]) -> Optional[str]:
    """Validate a chosen brand id, else fall back to the default brand's id."""
    if brand_id:
        try:
            brand = await get_brand(brand_id)
            if brand:
                return brand["id"]
        except Exception as exc:
            logger.warning("Could not load brand %s: %s", brand_id, exc)
    try:
        brand = await get_default_brand()
        return brand["id"] if brand else None
    except Exception as exc:
        logger.warning("Could not load default brand: %s", exc)
        return None

async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        logger.warning("Campaign %s not found", campaign_id)
        return
    if campaign.get("status") == "paused":
        logger.info("Campaign %s is paused — skipping run", campaign_id)
        return

    logger.info("Running campaign: %s", campaign.get("name"))
    await log_error("server", f"Campaign started: {campaign.get('name')}", campaign_id, "info")

    try:
        contacts = json.loads(campaign.get("contacts_json", "[]"))
    except Exception:
        contacts = []

    delay = int(campaign.get("call_delay_seconds", 3))
    agent_profile_id = await _default_outbound_profile_id()
    brand_id = await _resolve_call_brand_id(campaign.get("brand_id"))

    base_prompt = campaign.get("system_prompt")
    custom_prompt = await _assemble_campaign_call_prompt(campaign) if base_prompt else None

    dispatched = 0
    failed = 0

    for contact in contacts:
        phone = contact.get("phone", "").strip()
        name  = contact.get("name", "there").strip()
        if not phone:
            failed += 1
            continue

        extra: dict = {}
        if custom_prompt:
            extra["system_prompt"] = custom_prompt
        if agent_profile_id:
            extra["agent_profile_id"] = agent_profile_id
        if brand_id:
            extra["brand_id"] = brand_id

        ok = await _dispatch_call(phone, name, extra)
        if ok:
            dispatched += 1
        else:
            failed += 1

        if delay > 0:
            await asyncio.sleep(delay)

    await update_campaign_run_stats(campaign_id, dispatched, failed)
    logger.info("Campaign %s done — dispatched=%d failed=%d", campaign_id, dispatched, failed)
    await log_error("server", f"Campaign complete: dispatched={dispatched} failed={failed}", campaign_id, "info")


def _schedule_campaign(campaign: dict) -> None:
    cid = campaign["id"]
    schedule_type = campaign.get("schedule_type", "once")
    schedule_time = campaign.get("schedule_time", "09:00")

    # Remove old job if any
    if cid in _scheduled_campaign_jobs:
        try:
            scheduler.remove_job(_scheduled_campaign_jobs[cid])
        except Exception:
            pass

    try:
        hour, minute = map(int, schedule_time.split(":"))
    except Exception:
        hour, minute = 9, 0

    if schedule_type == "once":
        job = scheduler.add_job(
            _run_campaign, DateTrigger(run_date=datetime.now()),
            args=[cid], id=f"campaign_{cid}",
        )
    elif schedule_type == "daily":
        job = scheduler.add_job(
            _run_campaign, CronTrigger(hour=hour, minute=minute),
            args=[cid], id=f"campaign_{cid}",
        )
    elif schedule_type == "weekdays":
        job = scheduler.add_job(
            _run_campaign, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            args=[cid], id=f"campaign_{cid}",
        )
    else:
        return

    _scheduled_campaign_jobs[cid] = f"campaign_{cid}"
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", cid, schedule_type, hour, minute)


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
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
    # Re-schedule any active campaigns on restart
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") != "once":
                _schedule_campaign(c)
    except Exception as exc:
        logger.warning("Could not restore campaign schedules: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


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
    return {
        "status": "ok",
        "timestamp": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "langfuse": langfuse_status(),
        "recordings": recording_sync_status(),
    }


# ── Single call ───────────────────────────────────────────────────────────────

async def _get_call_campaign(campaign_id: str) -> Optional[dict]:
    """Validate a selected campaign and ensure it has a usable outbound script."""
    if not campaign_id:
        return None
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Selected campaign was not found")
    if campaign.get("status") != "active":
        raise HTTPException(409, "Selected campaign is not active")
    if not campaign.get("system_prompt"):
        raise HTTPException(409, "Selected campaign script is not ready yet")
    campaign["call_prompt"] = await _assemble_campaign_call_prompt(campaign)
    return campaign

@app.post("/call/single")
async def call_single(request: Request):
    body = await request.json()
    phone        = (body.get("phone_number") or "").strip()
    lead_name    = (body.get("lead_name") or "").strip() or "there"
    custom_p     = body.get("system_prompt", "")
    campaign_id  = body.get("campaign_id", "")
    brand_id     = (body.get("brand_id") or "").strip()

    if not phone:
        raise HTTPException(400, "phone_number is required")

    meta: dict = {}
    campaign = await _get_call_campaign(campaign_id)
    if campaign:
        meta["campaign_id"] = campaign["id"]
        meta["campaign_name"] = campaign["name"]
        meta["system_prompt"] = campaign["call_prompt"]
    if custom_p:
        if campaign:
            meta["system_prompt"] += (
                "\n\n━━━ ADDITIONAL CALL-SPECIFIC INSTRUCTIONS ━━━\n" + custom_p.strip()
            )
        else:
            meta["system_prompt"] = custom_p
    profile_id = await _default_outbound_profile_id()
    if profile_id:
        meta["agent_profile_id"] = profile_id
    # Explicit brand wins; else the selected campaign's brand; else the default.
    meta["brand_id"] = await _resolve_call_brand_id(brand_id or (campaign or {}).get("brand_id"))

    ok = await _dispatch_call(phone, lead_name, meta)
    if not ok:
        raise HTTPException(500, "Failed to dispatch call — check LiveKit credentials and trunk ID")
    return {"status": "dispatched", "phone": phone}


# ── Batch CSV call ────────────────────────────────────────────────────────────

@app.post("/call/batch")
async def call_batch(
    file: UploadFile = File(...),
    call_delay_seconds: int = Form(3),
    system_prompt: str = Form(""),
    campaign_id: str = Form(""),
    brand_id: str = Form(""),
):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    contacts = [row for row in reader]

    if not contacts:
        raise HTTPException(400, "CSV has no rows")

    campaign = await _get_call_campaign(campaign_id)
    agent_profile_id = await _default_outbound_profile_id()
    resolved_brand_id = await _resolve_call_brand_id(brand_id or (campaign or {}).get("brand_id"))

    dispatched = 0
    failed = 0
    results = []

    for row in contacts:
        phone = (row.get("phone") or row.get("phone_number") or "").strip()
        name  = (row.get("name") or row.get("lead_name") or "").strip() or "there"
        if not phone:
            failed += 1
            results.append({"phone": "", "status": "skipped", "reason": "no phone"})
            continue

        meta: dict = {}
        if resolved_brand_id:
            meta["brand_id"] = resolved_brand_id
        if campaign:
            meta["campaign_id"] = campaign["id"]
            meta["campaign_name"] = campaign["name"]
            meta["system_prompt"] = campaign["call_prompt"]
        if system_prompt:
            if campaign:
                meta["system_prompt"] += (
                    "\n\n━━━ ADDITIONAL CALL-SPECIFIC INSTRUCTIONS ━━━\n" + system_prompt.strip()
                )
            else:
                meta["system_prompt"] = system_prompt
        if agent_profile_id:
            meta["agent_profile_id"] = agent_profile_id

        ok = await _dispatch_call(phone, name, meta)
        if ok:
            dispatched += 1
            results.append({"phone": phone, "name": name, "status": "dispatched"})
        else:
            failed += 1
            results.append({"phone": phone, "name": name, "status": "failed"})

        if call_delay_seconds > 0:
            await asyncio.sleep(call_delay_seconds)

    return {"dispatched": dispatched, "failed": failed, "results": results}


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/appointments")
async def list_appointments(date: Optional[str] = Query(None)):
    return await get_all_trials(date_filter=date)


@app.post("/appointments")
async def create_appointment(request: Request):
    body = await request.json()
    required = ["name", "phone", "date", "time", "location"]
    for f in required:
        if not body.get(f):
            raise HTTPException(400, f"{f} is required")
    try:
        booking_id = await insert_trial(
            body["name"], body["phone"], body["date"], body["time"], body["location"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TrialSlotUnavailable as exc:
        alternatives = await get_next_available_trial_slots(
            body["date"], body["time"], body["location"]
        )
        raise HTTPException(409, {
            "message": "Trial slot is already booked",
            "alternatives": alternatives,
        }) from exc
    return {"booking_id": booking_id, "status": "booked"}


@app.delete("/appointments/{appointment_id}")
async def cancel_appt(appointment_id: str):
    ok = await cancel_trial(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Call logs ─────────────────────────────────────────────────────────────────

@app.get("/calls")
async def list_calls(
    page: int = Query(1), limit: int = Query(20), brand_id: Optional[str] = Query(None),
):
    return await get_all_calls(page=page, limit=limit, brand_id=brand_id)


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


# ── Stats / Charts ────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats(brand_id: Optional[str] = Query(None)):
    return await get_stats(brand_id=brand_id)


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.get("/campaigns")
async def list_campaigns():
    return await get_all_campaigns()


@app.post("/campaigns")
async def new_campaign(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    contacts = body.get("contacts", [])
    contacts_json = json.dumps(contacts)

    purpose = (body.get("purpose") or "").strip()
    cid = await create_campaign(
        name=name,
        contacts_json=contacts_json,
        schedule_type=body.get("schedule_type", "once"),
        schedule_time=body.get("schedule_time", "09:00"),
        call_delay_seconds=int(body.get("call_delay_seconds", 3)),
        system_prompt=body.get("system_prompt"),
        agent_profile_id=body.get("agent_profile_id"),
        purpose=purpose or None,
        brand_id=await _resolve_call_brand_id((body.get("brand_id") or "").strip()),
    )

    campaign = await get_campaign(cid)
    if campaign:
        _schedule_campaign(campaign)

    # If a purpose was given (and no explicit prompt), generate the script in the background.
    if purpose and not body.get("system_prompt"):
        asyncio.create_task(_regenerate_campaign(cid))

    return {"id": cid, "status": "created"}


@app.post("/campaigns/upload")
async def upload_campaign(
    file: UploadFile = File(...),
    name: str = Form(...),
    schedule_type: str = Form("once"),
    schedule_time: str = Form("09:00"),
    call_delay_seconds: int = Form(3),
    system_prompt: str = Form(""),
    agent_profile_id: str = Form(""),
    purpose: str = Form(""),
    brand_id: str = Form(""),
):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    contacts = []
    for row in reader:
        phone = (row.get("phone") or row.get("phone_number") or "").strip()
        nm    = (row.get("name") or row.get("lead_name") or "there").strip()
        if phone:
            entry: dict = {"phone": phone, "name": nm}
            contacts.append(entry)

    if not contacts:
        raise HTTPException(400, "CSV has no valid rows with a phone column")

    cid = await create_campaign(
        name=name,
        contacts_json=json.dumps(contacts),
        schedule_type=schedule_type,
        schedule_time=schedule_time,
        call_delay_seconds=call_delay_seconds,
        system_prompt=system_prompt or None,
        agent_profile_id=agent_profile_id or None,
        purpose=(purpose or "").strip() or None,
        brand_id=await _resolve_call_brand_id(brand_id.strip()),
    )

    campaign = await get_campaign(cid)
    if campaign:
        _schedule_campaign(campaign)

    if purpose.strip() and not system_prompt:
        asyncio.create_task(_regenerate_campaign(cid))

    return {"id": cid, "contacts_loaded": len(contacts), "status": "created"}


@app.get("/campaigns/{campaign_id}")
async def get_campaign_detail(campaign_id: str):
    c = await get_campaign(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@app.patch("/campaigns/{campaign_id}/status")
async def set_campaign_status(campaign_id: str, request: Request):
    body = await request.json()
    status = body.get("status", "")
    if status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be active | paused | completed")
    ok = await update_campaign_status(campaign_id, status)
    if not ok:
        raise HTTPException(404, "Campaign not found")

    if status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign:
            _schedule_campaign(campaign)
    elif status == "paused":
        job_id = _scheduled_campaign_jobs.get(campaign_id)
        if job_id:
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass

    return {"status": status}


@app.post("/campaigns/{campaign_id}/feedback")
async def campaign_feedback(campaign_id: str, request: Request):
    body = await request.json()
    text = (body.get("feedback") or "").strip()
    if not text:
        raise HTTPException(400, "feedback is required")
    c = await get_campaign(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    await append_campaign_feedback(campaign_id, text)
    # Persist the visible state before returning so the UI never briefly shows
    # the old script as ready while the background rewrite is queued.
    await set_campaign_prompt_status(campaign_id, "rewriting")
    # Regenerate the prompt in the background using purpose + all feedback so far.
    asyncio.create_task(_regenerate_campaign(campaign_id, "rewriting"))
    return {"status": "feedback_received", "regenerating": True}


@app.post("/campaigns/{campaign_id}/run")
async def run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "running", "campaign_id": campaign_id}


@app.delete("/campaigns/{campaign_id}")
async def remove_campaign(campaign_id: str):
    job_id = _scheduled_campaign_jobs.pop(campaign_id, None)
    if job_id:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    return {"status": "deleted"}


# ── Brands ────────────────────────────────────────────────────────────────────

def _as_json_text(value, default: str) -> str:
    """Accept a list/dict (serialise to JSON) or an already-JSON string."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    try:
        return json.dumps(value)
    except Exception:
        return default


@app.get("/brand-defaults")
async def brand_defaults():
    """The effective fallback prompt content, so the Brands editor can pre-fill
    empty fields. The default brand falls back to Harry's built-in content; a
    non-default brand falls back to the neutral generic content."""
    from prompts import (
        COMPACT_OUTBOUND_SYSTEM_PROMPT, INBOUND_SYSTEM_PROMPT, DEFAULT_BUSINESS_CONTEXT,
        GENERIC_OUTBOUND_PROMPT, GENERIC_INBOUND_PROMPT,
    )
    return {
        "builtin": {
            "outbound_prompt": COMPACT_OUTBOUND_SYSTEM_PROMPT,
            "inbound_prompt": INBOUND_SYSTEM_PROMPT,
            "business_context": DEFAULT_BUSINESS_CONTEXT,
        },
        "generic": {
            "outbound_prompt": GENERIC_OUTBOUND_PROMPT,
            "inbound_prompt": GENERIC_INBOUND_PROMPT,
            "business_context": "",
        },
    }


@app.get("/brands")
async def list_brands():
    return await get_all_brands()


@app.get("/brands/{brand_id}")
async def get_brand_detail(brand_id: str):
    brand = await get_brand(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    return brand


@app.post("/brands")
async def new_brand(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    bid = await create_brand(
        name=name,
        assistant_name=(body.get("assistant_name") or "Tina").strip() or "Tina",
        inbound_numbers=_as_json_text(body.get("inbound_numbers"), "[]"),
        outbound_prompt=body.get("outbound_prompt") or None,
        inbound_prompt=body.get("inbound_prompt") or None,
        business_context=body.get("business_context") or None,
        booking_config=_as_json_text(body.get("booking_config"), "{}"),
        voice=body.get("voice") or None,
        model=body.get("model") or None,
        is_default=bool(body.get("is_default", False)),
    )
    return {"id": bid, "status": "created"}


@app.put("/brands/{brand_id}")
async def edit_brand(brand_id: str, request: Request):
    body = await request.json()
    updates: dict = {}
    for field in ("name", "assistant_name", "outbound_prompt", "inbound_prompt",
                  "business_context", "voice", "model"):
        if field in body:
            updates[field] = body[field]
    if "inbound_numbers" in body:
        updates["inbound_numbers"] = _as_json_text(body["inbound_numbers"], "[]")
    if "booking_config" in body:
        updates["booking_config"] = _as_json_text(body["booking_config"], "{}")
    if "is_default" in body:
        updates["is_default"] = 1 if body["is_default"] else 0
    if not updates:
        raise HTTPException(400, "No updatable fields provided")
    ok = await update_brand(brand_id, updates)
    if not ok:
        raise HTTPException(404, "Brand not found")
    return {"status": "updated"}


@app.delete("/brands/{brand_id}")
async def remove_brand(brand_id: str):
    ok = await delete_brand(brand_id)
    if not ok:
        raise HTTPException(404, "Brand not found")
    return {"status": "deleted"}


@app.post("/brands/{brand_id}/set-default")
async def make_brand_default(brand_id: str):
    brand = await get_brand(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    await set_default_brand(brand_id)
    return {"status": "default set"}


# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/agent-profiles")
async def list_agent_profiles():
    return await get_all_agent_profiles()


@app.post("/agent-profiles")
async def new_agent_profile(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    pid = await create_agent_profile(
        name=name,
        voice=body.get("voice", "Aoede"),
        model=body.get("model", "gemini-3.1-flash-live-preview"),
        system_prompt=body.get("system_prompt"),
        enabled_tools=json.dumps(body.get("enabled_tools", [])),
        is_default=bool(body.get("is_default", False)),
    )
    return {"id": pid, "status": "created"}


@app.put("/agent-profiles/{profile_id}")
async def edit_agent_profile(profile_id: str, request: Request):
    body = await request.json()
    updates: dict = {}
    for field in ("name", "voice", "model", "system_prompt"):
        if field in body:
            updates[field] = body[field]
    if "enabled_tools" in body:
        updates["enabled_tools"] = json.dumps(body["enabled_tools"])
    if "is_default" in body:
        updates["is_default"] = 1 if body["is_default"] else 0
    if not updates:
        raise HTTPException(400, "No updatable fields provided")
    if updates.get("is_default"):
        await set_default_agent_profile(profile_id)
    else:
        ok = await update_agent_profile(profile_id, updates)
        if not ok:
            raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/agent-profiles/{profile_id}")
async def remove_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/agent-profiles/{profile_id}/set-default")
async def set_profile_default(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    await set_default_agent_profile(profile_id)
    return {"status": "default set"}


# ── Settings (BYOK) ───────────────────────────────────────────────────────────

@app.get("/settings")
async def get_settings():
    return await get_all_settings()


@app.post("/settings")
async def post_settings(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    await save_settings(body)
    return {"status": "saved", "keys_updated": list(body.keys())}


@app.get("/default-prompt")
async def get_default_prompt_ep():
    """The default base script (read-only in the UI) + how much feedback it carries."""
    from prompts import DEFAULT_SYSTEM_PROMPT
    saved = await get_default_prompt()
    feedback = await get_default_feedback()
    return {"value": saved or DEFAULT_SYSTEM_PROMPT, "custom": bool(saved), "feedback_count": len(feedback)}


@app.post("/default-prompt/feedback")
async def default_prompt_feedback(request: Request):
    body = await request.json()
    text = (body.get("feedback") or "").strip()
    if not text:
        raise HTTPException(400, "feedback is required")
    await append_default_feedback(text)
    asyncio.create_task(_regenerate_default_prompt())
    return {"status": "feedback_received", "regenerating": True}


@app.post("/default-prompt/reset")
async def reset_default_prompt_ep():
    """Clear the custom default + its feedback → fall back to the built-in script."""
    await save_default_prompt("")
    await set_setting("DEFAULT_PROMPT_FEEDBACK", "[]")
    return {"status": "reset"}


@app.get("/settings/{key}")
async def get_one_setting(key: str):
    value = await get_setting(key)
    return {"key": key, "value": value, "configured": bool(value)}


@app.post("/settings/{key}")
async def set_one_setting(key: str, request: Request):
    body = await request.json()
    value = str(body.get("value", ""))
    await set_setting(key, value)
    return {"key": key, "status": "saved"}


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
