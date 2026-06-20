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

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# load_dotenv(override=False) is a local-dev convenience only.
# On the VPS, Coolify injects env vars before process start, so this is a no-op.
load_dotenv(override=False)

from db import (
    init_db, log_error,
    get_all_settings, save_settings, get_setting, set_setting,
    get_errors, get_logs, clear_errors,
    insert_appointment, get_all_appointments, cancel_appointment,
    get_all_calls, update_call_notes, get_contacts, get_calls_by_phone,
    get_stats,
    create_campaign, get_all_campaigns, get_campaign, update_campaign_status,
    update_campaign_run_stats, delete_campaign,
    get_contact_memory, add_contact_memory,
    get_all_agent_profiles, get_agent_profile, create_agent_profile,
    update_agent_profile, delete_agent_profile, set_default_agent_profile,
)

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


# ── Campaign runner ───────────────────────────────────────────────────────────

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
    custom_prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
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
        if contact.get("service"):
            extra["service_type"] = contact["service"]
        if contact.get("business"):
            extra["business_name"] = contact["business"]

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
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── Single call ───────────────────────────────────────────────────────────────

@app.post("/call/single")
async def call_single(request: Request):
    body = await request.json()
    phone        = body.get("phone_number", "").strip()
    lead_name    = body.get("lead_name", "there").strip()
    business     = body.get("business_name", "our company").strip()
    service      = body.get("service_type", "our service").strip()
    custom_p     = body.get("system_prompt", "")
    profile_id   = body.get("agent_profile_id", "")

    if not phone:
        raise HTTPException(400, "phone_number is required")

    meta: dict = {"business_name": business, "service_type": service}
    if custom_p:
        meta["system_prompt"] = custom_p
    if profile_id:
        meta["agent_profile_id"] = profile_id

    ok = await _dispatch_call(phone, lead_name, meta)
    if not ok:
        raise HTTPException(500, "Failed to dispatch call — check LiveKit credentials and trunk ID")
    return {"status": "dispatched", "phone": phone}


# ── Batch CSV call ────────────────────────────────────────────────────────────

@app.post("/call/batch")
async def call_batch(
    file: UploadFile = File(...),
    business_name: str = Form("our company"),
    service_type: str = Form("our service"),
    call_delay_seconds: int = Form(3),
    system_prompt: str = Form(""),
    agent_profile_id: str = Form(""),
):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    contacts = [row for row in reader]

    if not contacts:
        raise HTTPException(400, "CSV has no rows")

    dispatched = 0
    failed = 0
    results = []

    for row in contacts:
        phone = (row.get("phone") or row.get("phone_number") or "").strip()
        name  = (row.get("name") or row.get("lead_name") or "there").strip()
        if not phone:
            failed += 1
            results.append({"phone": "", "status": "skipped", "reason": "no phone"})
            continue

        meta: dict = {"business_name": business_name, "service_type": service_type}
        if system_prompt:
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
    return await get_all_appointments(date_filter=date)


@app.post("/appointments")
async def create_appointment(request: Request):
    body = await request.json()
    required = ["name", "phone", "date", "time", "service"]
    for f in required:
        if not body.get(f):
            raise HTTPException(400, f"{f} is required")
    booking_id = await insert_appointment(
        body["name"], body["phone"], body["date"], body["time"], body["service"]
    )
    return {"booking_id": booking_id, "status": "booked"}


@app.delete("/appointments/{appointment_id}")
async def cancel_appt(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Call logs ─────────────────────────────────────────────────────────────────

@app.get("/calls")
async def list_calls(page: int = Query(1), limit: int = Query(20)):
    return await get_all_calls(page=page, limit=limit)


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
async def stats():
    return await get_stats()


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

    cid = await create_campaign(
        name=name,
        contacts_json=contacts_json,
        schedule_type=body.get("schedule_type", "once"),
        schedule_time=body.get("schedule_time", "09:00"),
        call_delay_seconds=int(body.get("call_delay_seconds", 3)),
        system_prompt=body.get("system_prompt"),
        agent_profile_id=body.get("agent_profile_id"),
    )

    campaign = await get_campaign(cid)
    if campaign:
        _schedule_campaign(campaign)

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
            if row.get("service"):
                entry["service"] = row["service"]
            if row.get("business"):
                entry["business"] = row["business"]
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
    )

    campaign = await get_campaign(cid)
    if campaign:
        _schedule_campaign(campaign)

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
