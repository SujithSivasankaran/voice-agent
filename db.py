"""
All database operations.

Single source of truth for configuration: VPS environment variables.
- get_all_settings() reads ONLY from os.environ — never Supabase.
- _adb() is version-safe: works with supabase-py >= 2.0.0 and >= 2.4.0.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from business import (
    MISSED_REASONS, SESSION_MINUTES, WEEKDAY_NAMES, location_key, schedule_from_slots, slots_for_weekday,
)

logger = logging.getLogger("outbound-db")

# Per-deploy timezone (IANA name, e.g. "Asia/Kolkata"). Drives timestamps, "today"
# boundaries, booking checks and calling-hour rules. The `IST` alias name is kept so
# existing call sites keep working; it holds the configured zone.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "").strip() or "Asia/Kolkata"
try:
    IST = ZoneInfo(APP_TIMEZONE)
except Exception:
    logger.warning("Invalid APP_TIMEZONE %r — falling back to Asia/Kolkata", APP_TIMEZONE)
    APP_TIMEZONE = "Asia/Kolkata"
    IST = ZoneInfo(APP_TIMEZONE)

APP_TZ = IST


def now_local() -> datetime:
    """Timezone-aware 'now' in the deploy's configured local zone."""
    return datetime.now(IST)


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
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY",
    "DEEPGRAM_API_KEY", "LANGFUSE_SECRET_KEY", "VOBIZ_AUTH_TOKEN",
    "DATABASE_URL",
}

KNOWN_SETTINGS_KEYS = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
    "TELEPHONY_COST_PER_MIN_USD",
    "APP_TIMEZONE", "DATABASE_URL",
    "CALLING_WINDOW_START", "CALLING_WINDOW_END", "CALLING_BLOCKED_DAYS",
    "MAX_CONCURRENT_DISPATCH", "MONTHLY_SPEND_CAP_USD",
    "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
    "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN",
    "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
    "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
    "RECORDING_SYNC_SECONDS",
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
    # Use logging (not print) so a non-UTF-8 console (e.g. Windows cp1252) can never
    # crash startup on an unencodable character, and keep the text plain ASCII.
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in environment.")
        return
    try:
        _sdb().table("settings").select("key").limit(1).execute()
        logger.info("Supabase connected")
    except Exception as exc:
        logger.warning("Supabase connection failed: %s", exc)
        logger.warning("Run the files in migrations/ in your Supabase Dashboard -> SQL Editor")


# ── Settings ──────────────────────────────────────────────────────────────────
# VPS environment variables are the ONLY source of runtime configuration. The
# Supabase settings table only holds auth material (see auth.py).

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


async def set_setting(key: str, value: str) -> None:
    """Persist a value in the Supabase settings table (auth material only)."""
    db = await _adb()
    await db.table("settings").upsert(
        {"key": key, "value": value, "updated_at": _now_iso()},
        on_conflict="key",
    ).execute()


async def get_stored_setting(key: str, default: str = "") -> str:
    """Read a value straight from the Supabase settings table (NOT env vars).

    Used for auth material (admin password hash, session secret) that the operator
    can rotate from the dashboard without redeploying.
    """
    try:
        db = await _adb()
        result = await db.table("settings").select("value").eq("key", key).limit(1).execute()
        rows = result.data or []
        if rows and rows[0].get("value"):
            return rows[0]["value"]
    except Exception as exc:
        logger.warning("get_stored_setting(%s) failed: %s", key, exc)
    return default


# ── Dashboard users ───────────────────────────────────────────────────────────
# Extra logins created by an admin. Password hashing lives in auth.py; this layer
# only stores and fetches rows. The built-in admin is not in this table.

ROLES = ("admin", "employee")


async def list_users() -> list:
    db = await _adb()
    rows = (await db.table("users").select("id, username, role, created_at").order("created_at").execute()).data
    return rows or []


async def get_user(user_id: str) -> Optional[dict]:
    db = await _adb()
    rows = (await db.table("users").select("*").eq("id", user_id).limit(1).execute()).data or []
    return rows[0] if rows else None


async def get_user_by_username(username: str) -> Optional[dict]:
    db = await _adb()
    # Usernames are stored lower-case (see auth.clean_username), so an exact match is enough.
    rows = (await db.table("users").select("*").eq("username", (username or "").strip().lower()).limit(1).execute()).data
    return rows[0] if rows else None


async def insert_user(username: str, password_hash: str, role: str) -> str:
    user_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("users").insert({
        "id": user_id, "username": username, "password_hash": password_hash,
        "role": role, "created_at": _now_iso(),
    }).execute()
    return user_id


async def update_user_fields(user_id: str, fields: dict) -> bool:
    db = await _adb()
    result = await db.table("users").update(fields).eq("id", user_id).execute()
    return len(result.data or []) > 0


async def delete_user(user_id: str) -> bool:
    db = await _adb()
    result = await db.table("users").delete().eq("id", user_id).execute()
    return len(result.data or []) > 0


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


# ── Do-Not-Call suppression list ──────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Loose E.164-ish normalisation for DNC matching: keep a leading '+' and digits
    only, so '+1 (555) 123-4567' and '+15551234567' compare equal."""
    phone = (phone or "").strip()
    if not phone:
        return ""
    plus = phone.startswith("+")
    digits = "".join(ch for ch in phone if ch.isdigit())
    return ("+" + digits) if plus else digits


async def is_dnc(phone: str) -> Optional[dict]:
    """Return the matching DNC row if `phone` is suppressed, else None."""
    norm = _normalize_phone(phone)
    if not norm:
        return None
    db = await _adb()
    result = await db.table("dnc_list").select("*").eq("phone_number", norm).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


async def add_dnc(phone: str, reason: str = "manual", source: str = "dashboard") -> str:
    """Add a suppression. Idempotent: an already-listed number returns its row id."""
    norm = _normalize_phone(phone)
    if not norm:
        raise ValueError("A phone number is required")
    existing = await is_dnc(norm)
    if existing:
        return existing["id"]
    entry_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("dnc_list").insert({
        "id": entry_id, "phone_number": norm, "reason": reason,
        "source": source, "created_at": _now_iso(),
    }).execute()
    return entry_id


async def remove_dnc(entry_id: str) -> bool:
    db = await _adb()
    result = await db.table("dnc_list").delete().eq("id", entry_id).execute()
    return len(result.data or []) > 0


async def list_dnc() -> list:
    db = await _adb()
    result = await db.table("dnc_list").select("*").order("created_at", desc=True).execute()
    return result.data or []


# ── Locations (branches) ──────────────────────────────────────────────────────
# Each branch has the weekdays it is open and the start times of its one-hour
# sessions. Edited from the dashboard; read by booking and by the call scripts.

def _normalize_time(time: str) -> str:
    """Accept '6:00', '06:00' or '16:30' and return zero-padded HH:MM."""
    try:
        hh, mm = str(time).strip().split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return f"{h:02d}:{m:02d}"
    except Exception as exc:
        raise ValueError("time must use HH:MM (24-hour)") from exc


def _loads_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _days(values) -> list:
    return sorted({int(d) for d in (values or []) if str(d).strip().isdigit() and 0 <= int(d) <= 6})


def _parse_location(row: dict) -> dict:
    """A branch with its timings. Rows saved before per-day timings existed only have
    `days` + `slot_times`; their timings are derived from those."""
    schedule = []
    for b in _loads_list(row.get("schedule")):
        try:
            schedule.append({"days": _days(b.get("days")), "start": _normalize_time(b["start"]),
                             "end": _normalize_time(b["end"])})
        except Exception:
            continue
    if not schedule:
        legacy_times = sorted({t for t in _loads_list(row.get("slot_times")) if isinstance(t, str)})
        schedule = schedule_from_slots(_days(_loads_list(row.get("days"))), legacy_times)
    return {"id": row.get("id"), "name": row.get("name") or "", "address": row.get("address") or "",
            "schedule": schedule}


def clean_location(body: dict) -> dict:
    """Validate a branch from the dashboard. `schedule` is a list of timings
    {days, start, end}. Raises ValueError with a readable message."""
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        raise ValueError("the branch name is required")
    schedule = []
    for i, b in enumerate(body.get("schedule") or [], start=1):
        days = _days((b or {}).get("days"))
        if not days:
            raise ValueError(f"timing {i}: pick at least one day")
        try:
            start, end = _normalize_time(b.get("start")), _normalize_time(b.get("end"))
        except ValueError as exc:
            raise ValueError(f"timing {i}: enter a start and end time") from exc
        if end <= start:
            raise ValueError(f"timing {i}: the end time must be after the start time")
        if int(end[:2]) * 60 + int(end[3:]) - (int(start[:2]) * 60 + int(start[3:])) < SESSION_MINUTES:
            raise ValueError(f"timing {i}: must be at least {SESSION_MINUTES} minutes long")
        schedule.append({"days": days, "start": start, "end": end})
    if not schedule:
        raise ValueError("add at least one timing")
    # Also keep the older columns filled, so nothing that reads them breaks.
    all_days = sorted({d for b in schedule for d in b["days"]})
    all_times = sorted({t for d in all_days for t in slots_for_weekday(schedule, d)})
    return {"name": name.upper(), "address": " ".join(str(body.get("address") or "").split()) or None,
            "schedule": json.dumps(schedule), "days": json.dumps(all_days), "slot_times": json.dumps(all_times)}


async def get_locations() -> list:
    db = await _adb()
    rows = (await db.table("locations").select("*").order("created_at").order("name").execute()).data or []
    return [_parse_location(r) for r in rows]


async def create_location(body: dict) -> str:
    row = clean_location(body)
    if any(location_key(l["name"]) == location_key(row["name"]) for l in await get_locations()):
        raise ValueError(f"a branch called {row['name']} already exists")
    row.update({"id": str(uuid.uuid4()), "created_at": _now_iso()})
    db = await _adb()
    await db.table("locations").insert(row).execute()
    return row["id"]


async def update_location(location_id: str, body: dict) -> bool:
    row = clean_location(body)
    if any(l["id"] != location_id and location_key(l["name"]) == location_key(row["name"])
           for l in await get_locations()):
        raise ValueError(f"a branch called {row['name']} already exists")
    db = await _adb()
    result = await db.table("locations").update(row).eq("id", location_id).execute()
    return len(result.data or []) > 0


async def delete_location(location_id: str) -> bool:
    db = await _adb()
    result = await db.table("locations").delete().eq("id", location_id).execute()
    return len(result.data or []) > 0


# ── Trial bookings ────────────────────────────────────────────────────────────
# One-hour trials at each branch's start times, on the days it is open, one per
# branch per slot. A unique index on (location, date, time) for booked rows
# (migration 0004) stops two calls taking the same slot at once.

class TrialSlotUnavailable(Exception):
    pass


def find_location(location: str, locations: list) -> dict:
    """Match what the caller/model said to a branch, or raise ValueError."""
    if not locations:
        raise ValueError("no branches are set up for bookings")
    key = location_key(location)
    for loc in locations:
        if location_key(loc["name"]) == key:
            return loc
    raise ValueError("the branch must be " + " or ".join(l["name"] for l in locations))


def validate_trial_slot(date: str, time: str, location: str, locations: list,
                        now: Optional[datetime] = None) -> tuple[str, str, str]:
    """Check a requested trial against that branch's schedule. Returns (date, time,
    branch name) normalised, or raises ValueError explaining what to fix."""
    loc = find_location(location, locations)
    time = _normalize_time(time)
    try:
        start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc
    starts = slots_for_weekday(loc["schedule"], start.weekday())
    if not starts:
        raise ValueError(f"{loc['name']} is closed on {WEEKDAY_NAMES[start.weekday()]}")
    if time not in starts:
        raise ValueError(f"trials at {loc['name']} on {WEEKDAY_NAMES[start.weekday()]} start at " + ", ".join(starts))
    now = (now or now_local()).replace(tzinfo=None)
    if start <= now:
        raise ValueError("the slot must be in the future")
    return start.strftime("%Y-%m-%d"), time, loc["name"]


async def _booked_times(date: str, branch: str) -> set:
    db = await _adb()
    rows = (await (
        db.table("trials").select("time")
        .eq("date", date).eq("location", branch).eq("status", "booked").execute()
    )).data or []
    return {r.get("time") for r in rows}


async def is_trial_slot_free(date: str, time: str, location: str) -> bool:
    date, time, branch = validate_trial_slot(date, time, location, await get_locations())
    return time not in await _booked_times(date, branch)


async def next_free_trial_slots(date: str, time: str, location: str, limit: int = 3) -> list[str]:
    """The next few free start times at the same branch, from the requested time on."""
    loc = find_location(location, await get_locations())
    now = now_local().replace(tzinfo=None)
    try:
        start = datetime.strptime(f"{date} {_normalize_time(time)}", "%Y-%m-%d %H:%M")
    except ValueError:
        start = now
    start = max(start, now)
    found: list[str] = []
    for day_offset in range(15):
        day = (start + timedelta(days=day_offset)).date()
        starts = slots_for_weekday(loc["schedule"], day.weekday())
        if not starts:
            continue
        taken = await _booked_times(day.isoformat(), loc["name"])
        for slot in starts:
            candidate = datetime.strptime(f"{day.isoformat()} {slot}", "%Y-%m-%d %H:%M")
            if candidate > start and slot not in taken:
                found.append(f"{day.isoformat()} at {slot} ({loc['name']})")
                if len(found) >= limit:
                    return found
    return found


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "23505" in str(exc) or "duplicate key" in str(exc)


async def book_trial(name: str, phone: str, date: str, time: str, location: str) -> dict:
    """Book a one-hour trial. Returns {booking_id, date, time, location}; raises
    ValueError for an invalid request and TrialSlotUnavailable if it is taken."""
    date, time, branch = validate_trial_slot(date, time, location, await get_locations())
    if not (name or "").strip():
        raise ValueError("the caller's name is required")
    if not (phone or "").strip():
        raise ValueError("the caller's phone number is required")
    if time in await _booked_times(date, branch):
        raise TrialSlotUnavailable("that slot is already booked")
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    end = datetime.strptime(time, "%H:%M") + timedelta(minutes=SESSION_MINUTES)
    db = await _adb()
    try:
        await db.table("trials").insert({
            "id": full_id, "booking_id": booking_id,
            "name": name.strip(), "phone": phone.strip(),
            "date": date, "time": time, "end_time": end.strftime("%H:%M"),
            "location": branch, "duration_minutes": SESSION_MINUTES,
            "status": "booked", "created_at": _now_iso(),
        }).execute()
    except Exception as exc:
        if _is_unique_violation(exc):
            raise TrialSlotUnavailable("that slot was just booked") from exc
        raise
    return {"booking_id": booking_id, "date": date, "time": time, "location": branch}


async def get_all_trials(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("trials").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_trial(trial_id: str) -> bool:
    db = await _adb()
    result = await (
        db.table("trials").update({"status": "cancelled"})
        .eq("id", trial_id).eq("status", "booked").execute()
    )
    return len(result.data or []) > 0


# ── Missed classes ────────────────────────────────────────────────────────────

async def record_missed_class(phone: str, member_name: Optional[str], category: str, reason: str,
                              coming_back: str = "", missed_date: str = "") -> str:
    """Save why a member missed class. Unknown categories are stored as 'other'."""
    category = (category or "").strip().lower()
    if category not in MISSED_REASONS:
        category = "other"
    row_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("missed_classes").insert({
        "id": row_id, "phone_number": phone, "member_name": member_name,
        "missed_date": (missed_date or "").strip() or None,
        "reason_category": category, "reason": (reason or "").strip()[:500],
        "coming_back": (coming_back or "").strip()[:200] or None,
        "created_at": _now_iso(),
    }).execute()
    return row_id


async def link_missed_class_call(row_id: str, call_id: str) -> None:
    db = await _adb()
    await db.table("missed_classes").update({"call_id": call_id}).eq("id", row_id).execute()


async def get_missed_classes(limit: int = 500) -> list:
    db = await _adb()
    result = await (
        db.table("missed_classes").select("*").order("created_at", desc=True).limit(limit).execute()
    )
    return result.data or []


async def delete_missed_class(row_id: str) -> bool:
    db = await _adb()
    result = await db.table("missed_classes").delete().eq("id", row_id).execute()
    return len(result.data or []) > 0


# ── Call logs ─────────────────────────────────────────────────────────────────

async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    transcript: Optional[str] = None, campaign: Optional[str] = None,
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
    if campaign:
        row["campaign"] = campaign
    await db.table("call_logs").insert(row).execute()
    return call_id


async def update_call_transcript(call_id: str, transcript: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"transcript": transcript}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def update_call_cost(call_id: str, estimated_cost: float) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"estimated_cost": estimated_cost}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_all_calls(page: int = 1, limit: int = 20, campaign: Optional[str] = None) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    query = db.table("call_logs").select("*").order("timestamp", desc=True)
    if campaign:
        query = query.eq("campaign", campaign)
    result = await query.range(offset, offset + limit - 1).execute()
    return result.data or []


async def get_call(call_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("id", call_id).maybe_single().execute()
    return result.data if result else None


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


async def get_month_to_date_spend() -> float:
    """Estimated spend (USD) since the first of the current local month. Used by the
    monthly spend cap. Returns 0.0 if the cost column is unmigrated."""
    start = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    db = await _adb()
    try:
        rows = (await (
            db.table("call_logs").select("estimated_cost").gte("timestamp", start.isoformat()).execute()
        )).data or []
    except Exception:
        return 0.0
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("estimated_cost") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


# ── Stats ─────────────────────────────────────────────────────────────────────

# Outcomes where the agent never actually connected/spoke with a person.
NON_CONNECTED_OUTCOMES = {"no_answer", "busy", "failed", "declined", "timeout"}


async def get_stats() -> dict:
    """The four dashboard numbers: total calls, answer rate, average duration and spend."""
    db = await _adb()
    rows = (await db.table("call_logs").select(
        "outcome, duration_seconds, timestamp, estimated_cost"
    ).execute()).data or []
    today_iso = datetime.now(IST).date().isoformat()

    total_calls = len(rows)
    connected   = sum(1 for r in rows if (r.get("outcome") or "") not in NON_CONNECTED_OUTCOMES)
    durations   = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    total_cost  = sum(float(r.get("estimated_cost") or 0) for r in rows)
    return {
        "total_calls": total_calls,
        "calls_today": sum(1 for r in rows if (r.get("timestamp") or "")[:10] == today_iso),
        "connected": connected,
        "answer_rate_percent": round((connected / total_calls * 100) if total_calls else 0, 1),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "total_cost": round(total_cost, 4),
        "cost_per_call": round((total_cost / total_calls) if total_calls else 0, 4),
    }


# ── Call-list runs (the `campaigns` table) ────────────────────────────────────
# Each uploaded call list is one run of one of the two campaigns (`kind`). Its
# contacts live in campaign_contacts so a run can be paused and resumed without
# calling anyone twice.

async def create_call_run(name: str, kind: str, call_delay_seconds: int = 3) -> str:
    run_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("campaigns").insert({
        "id": run_id, "name": name, "kind": kind, "status": "active",
        "contacts_json": "[]", "schedule_type": "once",
        "call_delay_seconds": call_delay_seconds,
        "created_at": _now_iso(), "total_dispatched": 0, "total_failed": 0,
    }).execute()
    return run_id


async def get_call_runs() -> list:
    """All call-list runs, newest first. Rows from the old free-form campaigns
    (no kind) are ignored."""
    db = await _adb()
    result = await (
        db.table("campaigns").select("*").not_.is_("kind", "null")
        .order("created_at", desc=True).execute()
    )
    return result.data or []


async def get_call_run(run_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", run_id).maybe_single().execute()
    return result.data if result else None


async def update_call_run_status(run_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", run_id).execute()
    return len(result.data or []) > 0


async def pause_interrupted_runs() -> int:
    """On startup, any run still marked active was interrupted by the restart. Mark
    it paused so the dashboard shows Resume instead of pretending it is running."""
    db = await _adb()
    result = await (
        db.table("campaigns").update({"status": "paused"})
        .eq("status", "active").not_.is_("kind", "null").execute()
    )
    return len(result.data or [])


async def finish_call_run(run_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({
        "last_run_at": _now_iso(),
        "total_dispatched": dispatched, "total_failed": failed, "status": "completed",
    }).eq("id", run_id).execute()


async def delete_call_run(run_id: str) -> bool:
    db = await _adb()
    try:
        await db.table("campaign_contacts").delete().eq("campaign_id", run_id).execute()
    except Exception:
        pass
    result = await db.table("campaigns").delete().eq("id", run_id).execute()
    return len(result.data or []) > 0


async def create_campaign_contacts(run_id: str, contacts: list) -> int:
    """Seed one queued row per contact. Duplicate numbers in the list are dropped.
    Returns rows inserted."""
    if not contacts:
        return 0
    now = _now_iso()
    rows, seen = [], set()
    for c in contacts:
        phone = _normalize_phone(str(c.get("phone") or ""))
        if not phone or phone in seen:
            continue
        seen.add(phone)
        rows.append({
            "id": str(uuid.uuid4()), "campaign_id": run_id, "phone_number": phone,
            "lead_name": c.get("name") or "there",
            "missed_date": c.get("missed_date") or None,
            "status": "queued", "attempts": 0, "created_at": now,
        })
    if not rows:
        return 0
    db = await _adb()
    await db.table("campaign_contacts").upsert(
        rows, on_conflict="campaign_id,phone_number", ignore_duplicates=True,
    ).execute()
    return len(rows)


async def get_pending_campaign_contacts(run_id: str, limit: int = 5000) -> list:
    db = await _adb()
    result = await (
        db.table("campaign_contacts").select("*")
        .eq("campaign_id", run_id).eq("status", "queued")
        .order("created_at").limit(limit).execute()
    )
    return result.data or []


async def claim_campaign_contact(row_id: str) -> bool:
    """Atomically transition a contact queued → dialing. Returns True only if THIS
    caller won, so a contact is never dispatched twice."""
    db = await _adb()
    result = await (
        db.table("campaign_contacts")
        .update({"status": "dialing", "last_attempt_at": _now_iso()})
        .eq("id", row_id).eq("status", "queued").execute()
    )
    return len(result.data or []) > 0


async def mark_campaign_contact(row_id: str, status: str,
                                outcome: Optional[str] = None,
                                skip_reason: Optional[str] = None) -> None:
    """Set a contact's state after a dispatch attempt."""
    db = await _adb()
    updates: dict = {"status": status, "last_attempt_at": _now_iso()}
    if outcome is not None:
        updates["outcome"] = outcome
    if skip_reason is not None:
        updates["skip_reason"] = skip_reason
    try:
        cur = await db.table("campaign_contacts").select("attempts").eq("id", row_id).maybe_single().execute()
        prev = int((cur.data or {}).get("attempts") or 0) if cur else 0
        updates["attempts"] = prev + 1
    except Exception:
        pass
    await db.table("campaign_contacts").update(updates).eq("id", row_id).execute()


async def count_campaign_contacts_by_status(run_id: str) -> dict:
    db = await _adb()
    result = await db.table("campaign_contacts").select("status").eq("campaign_id", run_id).execute()
    counts: dict = defaultdict(int)
    for row in (result.data or []):
        counts[row.get("status") or "unknown"] += 1
    return dict(counts)


# ── Contact memory (staff notes and messages taken on incoming calls) ─────────

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
