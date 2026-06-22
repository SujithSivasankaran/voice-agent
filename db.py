"""
All database operations.

Single source of truth for configuration: VPS environment variables.
- get_setting() and get_all_settings() read ONLY from os.environ — never Supabase.
- save_settings() exists for display/audit purposes only; it does NOT affect runtime.
- _adb() is version-safe: works with supabase-py >= 2.0.0 and >= 2.4.0.
"""

import asyncio
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _now_iso() -> str:
    """Timezone-aware current timestamp used by all persisted records."""
    return datetime.now(IST).isoformat()

# ── Env helpers ───────────────────────────────────────────────────────────────

def _default(key: str, fallback: str = "") -> str:
    """Read a value from os.environ. Always reflects VPS env vars at call time."""
    return os.environ.get(key, fallback)


SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY", "LANGFUSE_SECRET_KEY", "VOBIZ_AUTH_TOKEN",
}

KNOWN_SETTINGS_KEYS = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
    "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
    "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN",
    "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
    "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
    "RECORDING_SYNC_SECONDS",
    "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
    "ENABLED_TOOLS",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_HOST",
]


# ── Supabase async client (version-safe singleton) ────────────────────────────

_db_client = None
_db_lock: Optional[asyncio.Lock] = None


async def _adb():
    """
    Returns a cached async Supabase client.
    Compatible with supabase-py 2.0.x (uses _async.client) and 2.4.x+ (uses acreate_client).
    """
    global _db_client, _db_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    async with _db_lock:
        if _db_client is None:
            url = _default("SUPABASE_URL")
            key = _default("SUPABASE_SERVICE_KEY")
            try:
                from supabase import acreate_client  # supabase >= 2.4.0
                _db_client = await acreate_client(url, key)
            except ImportError:
                from supabase._async.client import create_client as _ac  # supabase 2.0-2.3
                _db_client = await _ac(url, key)
    return _db_client


def _sdb():
    """Synchronous client — used only in init_db() startup check."""
    from supabase import create_client
    return create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


def init_db() -> None:
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("⚠️  SUPABASE_URL or SUPABASE_SERVICE_KEY not set in environment.")
        return
    try:
        _sdb().table("settings").select("key").limit(1).execute()
        print("✅ Supabase connected")
    except Exception as exc:
        print(f"⚠️  Supabase connection failed: {exc}")
        print("   Run supabase_schema.sql in your Supabase Dashboard → SQL Editor")


# ── Settings ──────────────────────────────────────────────────────────────────
# VPS environment variables are the ONLY source of runtime configuration.
# The Supabase settings table is NOT consulted for runtime values.

async def get_all_settings() -> dict:
    """Returns current values straight from VPS env vars. Supabase is not read."""
    out: dict = {}
    for k in KNOWN_SETTINGS_KEYS:
        env_val = _default(k)
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(env_val)}
        else:
            out[k] = {"value": env_val, "configured": bool(env_val)}
    return out


async def get_setting(key: str, default: str = "") -> str:
    """Always reads from VPS env vars. Supabase is NOT consulted."""
    return _default(key) or default


async def set_setting(key: str, value: str) -> None:
    """Writes to Supabase for audit/display only. Does not affect runtime."""
    db = await _adb()
    await db.table("settings").upsert(
        {"key": key, "value": value, "updated_at": _now_iso()},
        on_conflict="key",
    ).execute()


async def save_settings(data: dict) -> None:
    """Writes to Supabase for audit/display only. Does not affect runtime."""
    db = await _adb()
    updated_at = _now_iso()
    rows = [
        {"key": k, "value": str(v), "updated_at": updated_at}
        for k, v in data.items()
        if v is not None and v != ""
    ]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()



async def get_enabled_tools() -> list:
    """Reads ENABLED_TOOLS from VPS env var (JSON array string or empty → all tools)."""
    raw = _default("ENABLED_TOOLS")
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Error / event logs ────────────────────────────────────────────────────────

async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()),
            "source": source,
            "level": level,
            "message": message[:500],
            "detail": detail[:2000],
            "timestamp": _now_iso(),
        }).execute()
    except Exception:
        pass


async def get_errors(limit: int = 100) -> list:
    db = await _adb()
    result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    if source:
        query = query.eq("source", source)
    result = await query.execute()
    return result.data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


# ── Appointments ──────────────────────────────────────────────────────────────

# Default (Harry's Fitcamp) booking rules. A brand with no booking_config of its
# own falls back to these, so the seeded default brand behaves exactly as before.
TRIAL_LOCATIONS = ("ADAYAR", "ECR")
TRIAL_SLOT_TIMES = (
    "06:00", "07:00", "08:00", "09:00",
    "16:30", "17:30", "18:30", "19:30",
)
DEFAULT_TRIAL_DURATION_MINUTES = 60
DEFAULT_TRIAL_OFF_DAYS = (6,)  # weekday ints (Mon=0 … Sun=6); Sunday closed


class TrialSlotUnavailable(Exception):
    pass


def resolve_booking_config(config: Optional[dict]) -> dict:
    """Normalise a brand's booking_config.

    config=None means the legacy / no-brand path → fall back to the full Harry's
    defaults (locations, slot times, off-days). A dict (even {}) is used as-is:
    a missing field means that dimension is unconstrained — no locations (book
    without one), no fixed slot times (any time), and no closed days.
    Returns locations (UPPER), slot_times, duration, off_days.
    """
    if config is None:
        return {
            "locations": TRIAL_LOCATIONS,
            "slot_times": TRIAL_SLOT_TIMES,
            "duration_minutes": DEFAULT_TRIAL_DURATION_MINUTES,
            "off_days": DEFAULT_TRIAL_OFF_DAYS,
        }
    locations = tuple(
        str(loc).strip().upper()
        for loc in (config.get("locations") or [])
        if str(loc).strip()
    )
    slot_times = tuple(config.get("slot_times") or [])
    try:
        duration = int(config.get("duration_minutes") or DEFAULT_TRIAL_DURATION_MINUTES)
    except (TypeError, ValueError):
        duration = DEFAULT_TRIAL_DURATION_MINUTES
    off_days_raw = config.get("off_days")
    off_days = tuple(off_days_raw) if off_days_raw is not None else ()
    return {
        "locations": locations,
        "slot_times": slot_times,
        "duration_minutes": duration,
        "off_days": off_days,
    }


def normalize_trial_location(location: str, config: Optional[dict] = None) -> str:
    """Resolve the booking location against the brand's configured locations:
    - no locations configured → return "" (book without a location);
    - exactly one location    → use it, even if the caller didn't say it;
    - two or more             → the caller's choice must match one of them.
    """
    cfg = resolve_booking_config(config)
    locations = cfg["locations"]
    value = (location or "").strip().upper()
    if not locations:
        return ""
    if len(locations) == 1:
        # Single branch — no need to ask; accept it whether or not the caller named it.
        if value and value != locations[0]:
            raise ValueError(f"the only location is {locations[0]}")
        return locations[0]
    if value not in locations:
        raise ValueError("location must be " + " or ".join(locations))
    return value


def validate_trial_slot(
    date: str, time: str, location: str, config: Optional[dict] = None,
) -> tuple[str, str, str]:
    cfg = resolve_booking_config(config)
    location = normalize_trial_location(location, config)
    try:
        slot = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("date and time must use YYYY-MM-DD and HH:MM") from exc
    if cfg["off_days"] and slot.weekday() in cfg["off_days"]:
        raise ValueError("bookings are unavailable on that day")
    if cfg["slot_times"] and time not in cfg["slot_times"]:
        raise ValueError("time must be one of: " + ", ".join(cfg["slot_times"]))
    now = datetime.now(IST).replace(tzinfo=None)
    if slot <= now:
        raise ValueError("the slot must be in the future")
    return date, time, location


async def check_trial_slot(
    date: str, time: str, location: str,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
) -> bool:
    """One active trial is allowed per brand/location/start time."""
    date, time, location = validate_trial_slot(date, time, location, config)
    db = await _adb()
    query = (
        db.table("trials").select("id")
        .eq("date", date).eq("time", time).eq("location", location)
        .eq("status", "booked")
    )
    if brand_id:
        query = query.eq("brand_id", brand_id)
    result = await query.limit(1).execute()
    return not bool(result.data)


async def get_next_available_trial_slots(
    date: str, time: str, location: str, limit: int = 3,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
) -> list[str]:
    """Return the next valid trial starts at the requested location for a brand."""
    cfg = resolve_booking_config(config)
    location = normalize_trial_location(location, config)
    try:
        requested = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        requested = datetime.now(IST).replace(tzinfo=None)
    now = datetime.now(IST).replace(tzinfo=None)
    start = max(requested, now)
    found: list[str] = []
    for day_offset in range(15):
        day = (start + timedelta(days=day_offset)).date()
        if day.weekday() in cfg["off_days"]:
            continue
        for slot_time in cfg["slot_times"]:
            candidate = datetime.strptime(f"{day.isoformat()} {slot_time}", "%Y-%m-%d %H:%M")
            if candidate <= start:
                continue
            if await check_trial_slot(day.isoformat(), slot_time, location, brand_id, config):
                found.append(f"{day.isoformat()} at {slot_time}")
                if len(found) >= limit:
                    return found
    return found


async def insert_trial(
    name: str, phone: str, date: str, time: str, location: str,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
) -> str:
    date, time, location = validate_trial_slot(date, time, location, config)
    cfg = resolve_booking_config(config)
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    row = {
        "id": full_id, "booking_id": booking_id,
        "name": name.strip(), "phone": phone.strip(),
        "date": date, "time": time, "location": location,
        "duration_minutes": cfg["duration_minutes"], "status": "booked",
        "created_at": _now_iso(),
    }
    if brand_id:
        row["brand_id"] = brand_id
    try:
        await db.table("trials").insert(row).execute()
    except Exception as exc:
        # The DB unique index is the final concurrency guard. Re-checking lets
        # us distinguish a slot race from an unrelated database failure.
        if not await check_trial_slot(date, time, location, brand_id, config):
            raise TrialSlotUnavailable("trial slot was just booked") from exc
        raise
    return booking_id


async def get_all_trials(date_filter: Optional[str] = None, brand_id: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("trials").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    if brand_id:
        query = query.eq("brand_id", brand_id)
    result = await query.execute()
    return result.data or []


async def cancel_trial(trial_id: str) -> bool:
    db = await _adb()
    result = await (
        db.table("trials").update({"status": "cancelled"})
        .eq("id", trial_id).eq("status", "booked").execute()
    )
    return len(result.data or []) > 0

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    await db.table("appointments").insert({
        "id": full_id, "name": name, "phone": phone,
        "date": date, "time": time, "service": service,
        "status": "booked", "created_at": _now_iso(),
    }).execute()
    return booking_id


async def check_slot(date: str, time: str) -> bool:
    """Returns True if slot is available (no existing booking)."""
    db = await _adb()
    result = await (
        db.table("appointments").select("id")
        .eq("date", date).eq("time", time).eq("status", "booked")
        .maybe_single().execute()
    )
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now(IST).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    result = await (
        db.table("appointments").update({"status": "cancelled"})
        .eq("id", appointment_id).eq("status", "booked").execute()
    )
    return len(result.data or []) > 0


async def get_appointments_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
    return result.data or []


# ── Call logs ─────────────────────────────────────────────────────────────────

async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    transcript: Optional[str] = None, brand_id: Optional[str] = None,
) -> str:
    db = await _adb()
    call_id = str(uuid.uuid4())
    row: dict = {
        "id": call_id, "phone_number": phone_number, "lead_name": lead_name,
        "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds,
        "timestamp": _now_iso(),
    }
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    if transcript:
        row["transcript"] = transcript
    if brand_id:
        row["brand_id"] = brand_id
    await db.table("call_logs").insert(row).execute()
    return call_id


async def update_call_transcript(call_id: str, transcript: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"transcript": transcript}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_all_calls(page: int = 1, limit: int = 20, brand_id: Optional[str] = None) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    query = db.table("call_logs").select("*").order("timestamp", desc=True)
    if brand_id:
        query = query.eq("brand_id", brand_id)
    result = await query.range(offset, offset + limit - 1).execute()
    return result.data or []


async def get_call(call_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("id", call_id).maybe_single().execute()
    return result.data


async def get_recent_calls_without_recording(limit: int = 200) -> list:
    """Recent dashboard calls eligible for carrier-recording reconciliation."""
    db = await _adb()
    result = await (
        db.table("call_logs").select("id, phone_number, timestamp, duration_seconds, recording_url")
        .is_("recording_url", "null").order("timestamp", desc=True).limit(limit).execute()
    )
    return result.data or []


async def set_call_recording(call_id: str, recording_ref: str) -> bool:
    db = await _adb()
    result = await (
        db.table("call_logs").update({"recording_url": recording_ref})
        .eq("id", call_id).execute()
    )
    return len(result.data or []) > 0


async def get_calls_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
    return result.data or []


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts() -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).execute()
    rows = result.data or []
    contacts: dict = {}
    for row in rows:
        phone = row["phone_number"]
        if phone not in contacts:
            contacts[phone] = {
                "phone_number": phone, "lead_name": row.get("lead_name"),
                "total_calls": 0, "booked": 0,
                "last_call": row["timestamp"], "last_outcome": row.get("outcome"),
            }
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(brand_id: Optional[str] = None) -> dict:
    db = await _adb()
    stats_query = db.table("call_logs").select("outcome, duration_seconds, timestamp")
    if brand_id:
        stats_query = stats_query.eq("brand_id", brand_id)
    rows = (await stats_query.execute()).data or []
    total_calls    = len(rows)
    booked         = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations      = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    avg_dur        = sum(durations) / len(durations) if durations else 0
    booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)
    outcomes: dict = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
    daily: dict = defaultdict(int)
    for r in rows:
        ts = (r.get("timestamp") or "")[:10]
        if ts:
            daily[ts] += 1
    today = datetime.now(IST).date()
    timeline = [
        {"date": (today - timedelta(days=i)).isoformat(),
         "count": daily.get((today - timedelta(days=i)).isoformat(), 0)}
        for i in range(13, -1, -1)
    ]
    dur_sum: dict = defaultdict(float)
    dur_cnt: dict = defaultdict(int)
    for r in rows:
        o = r.get("outcome") or "unknown"
        sec = r.get("duration_seconds")
        if sec:
            dur_sum[o] += sec
            dur_cnt[o] += 1
    duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}
    return {
        "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1), "booking_rate_percent": booking_rate,
        "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
    }


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 3,
    system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None,
    purpose: Optional[str] = None, brand_id: Optional[str] = None,
) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row: dict = {
        "id": campaign_id, "name": name, "status": "active",
        "contacts_json": contacts_json, "schedule_type": schedule_type,
        "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
        "created_at": _now_iso(), "total_dispatched": 0, "total_failed": 0,
    }
    if system_prompt:
        row["system_prompt"] = system_prompt
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    if brand_id:
        row["brand_id"] = brand_id
    if purpose:
        row["purpose"] = purpose
        # Pending generation if a purpose is given and no explicit prompt supplied.
        row["prompt_status"] = "generating" if not system_prompt else "ready"
    await db.table("campaigns").insert(row).execute()
    return campaign_id


async def get_active_campaigns(brand_id: Optional[str] = None) -> list:
    """Active campaigns with their short summaries — used to build inbound/outbound scope.
    When brand_id is given, only that brand's active campaigns are returned."""
    db = await _adb()
    query = (
        db.table("campaigns").select("id, name, summary, status, brand_id")
        .eq("status", "active")
    )
    if brand_id:
        query = query.eq("brand_id", brand_id)
    result = await query.execute()
    return result.data or []


async def update_campaign_generated(campaign_id: str, system_prompt: str, summary: str, status: str = "ready") -> None:
    """Save the LLM-generated outbound script + short summary for a campaign."""
    db = await _adb()
    await db.table("campaigns").update({
        "system_prompt": system_prompt, "summary": summary, "prompt_status": status,
    }).eq("id", campaign_id).execute()


async def set_campaign_prompt_status(campaign_id: str, status: str) -> None:
    db = await _adb()
    await db.table("campaigns").update({"prompt_status": status}).eq("id", campaign_id).execute()


async def append_campaign_feedback(campaign_id: str, feedback: str) -> list:
    """Append a feedback entry (cumulative) and return the full feedback list."""
    import json
    db = await _adb()
    c = await get_campaign(campaign_id)
    items: list = []
    if c and c.get("feedback"):
        try:
            items = json.loads(c["feedback"])
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
    items.append({"text": feedback, "at": _now_iso()})
    await db.table("campaigns").update({"feedback": json.dumps(items)}).eq("id", campaign_id).execute()
    return items


async def get_all_campaigns() -> list:
    db = await _adb()
    result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return result.data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({
        "last_run_at": _now_iso(),
        "total_dispatched": dispatched, "total_failed": failed, "status": "completed",
    }).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


# ── Contact Memory ────────────────────────────────────────────────────────────

async def add_contact_memory(phone: str, insight: str) -> None:
    db = await _adb()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": insight[:1000], "created_at": _now_iso(),
    }).execute()


async def get_contact_memory(phone: str) -> list:
    db = await _adb()
    result = await (
        db.table("contact_memory").select("insight, created_at")
        .eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()
    )
    return result.data or []


async def compress_contact_memory(phone: str, compressed: str) -> None:
    db = await _adb()
    await db.table("contact_memory").delete().eq("phone_number", phone).execute()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": compressed[:2000], "created_at": _now_iso(),
    }).execute()


# ── Agent Profiles ────────────────────────────────────────────────────────────

async def get_all_agent_profiles() -> list:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").order("created_at").execute()
    return result.data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def get_default_agent_profile() -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("is_default", 1).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


async def create_agent_profile(
    name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview",
    system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False,
) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({
        "id": profile_id, "name": name, "voice": voice, "model": model,
        "system_prompt": system_prompt, "enabled_tools": enabled_tools,
        "is_default": 1 if is_default else 0, "created_at": _now_iso(),
    }).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()


# ── Brands (multi-tenant) ─────────────────────────────────────────────────────

def _phone_digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _numbers_match(a: str, b: str) -> bool:
    """Lenient DID comparison: equal digit strings, or one is a suffix of the
    other (handles +country-code vs national formats)."""
    da, db = _phone_digits(a), _phone_digits(b)
    if not da or not db:
        return False
    if da == db:
        return True
    short, long = (da, db) if len(da) <= len(db) else (db, da)
    return len(short) >= 7 and long.endswith(short)


async def get_all_brands() -> list:
    db = await _adb()
    result = await db.table("brands").select("*").order("created_at").execute()
    return result.data or []


async def get_brand(brand_id: str) -> Optional[dict]:
    if not brand_id:
        return None
    db = await _adb()
    result = await db.table("brands").select("*").eq("id", brand_id).maybe_single().execute()
    return result.data if result else None


async def get_default_brand() -> Optional[dict]:
    """The brand flagged is_default; falls back to the oldest brand if none is flagged."""
    db = await _adb()
    result = await db.table("brands").select("*").eq("is_default", 1).limit(1).execute()
    rows = result.data or []
    if rows:
        return rows[0]
    result = await db.table("brands").select("*").order("created_at").limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


async def get_brand_by_number(did: str) -> Optional[dict]:
    """Match an inbound dialed number (DID) to a brand by scanning each brand's
    inbound_numbers JSON array."""
    import json
    if not did:
        return None
    for brand in await get_all_brands():
        raw = brand.get("inbound_numbers")
        try:
            numbers = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            numbers = []
        if not isinstance(numbers, list):
            continue
        if any(_numbers_match(did, str(n)) for n in numbers):
            return brand
    return None


async def create_brand(
    name: str, assistant_name: str = "Tina", inbound_numbers: str = "[]",
    outbound_prompt: Optional[str] = None, inbound_prompt: Optional[str] = None,
    business_context: Optional[str] = None, booking_config: str = "{}",
    voice: Optional[str] = None, model: Optional[str] = None, is_default: bool = False,
) -> str:
    brand_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("brands").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("brands").insert({
        "id": brand_id, "name": name, "assistant_name": assistant_name,
        "inbound_numbers": inbound_numbers, "outbound_prompt": outbound_prompt,
        "inbound_prompt": inbound_prompt, "business_context": business_context,
        "booking_config": booking_config, "voice": voice, "model": model,
        "is_default": 1 if is_default else 0, "created_at": _now_iso(),
    }).execute()
    return brand_id


async def update_brand(brand_id: str, updates: dict) -> bool:
    db = await _adb()
    if updates.get("is_default"):
        await set_default_brand(brand_id)
        updates = {k: v for k, v in updates.items() if k != "is_default"}
        if not updates:
            return True
    result = await db.table("brands").update(updates).eq("id", brand_id).execute()
    return len(result.data or []) > 0


async def delete_brand(brand_id: str) -> bool:
    db = await _adb()
    result = await db.table("brands").delete().eq("id", brand_id).execute()
    return len(result.data or []) > 0


async def set_default_brand(brand_id: str) -> None:
    db = await _adb()
    await db.table("brands").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("brands").update({"is_default": 1}).eq("id", brand_id).execute()
