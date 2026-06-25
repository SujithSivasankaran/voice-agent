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
    "TELEPHONY_COST_PER_MIN_USD",
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
            "slot_mode": "fixed",
            "open_hours": {},
            "slot_interval_minutes": 30,
            "align_start_to_grid": True,
            "services": [],
            "resources": (),
            "capacity_per_slot": 1,
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

    # Services: each {name, duration_minutes}; duration falls back to the brand default.
    services = []
    for s in (config.get("services") or []):
        if not isinstance(s, dict):
            continue
        nm = str(s.get("name") or "").strip()
        if not nm:
            continue
        try:
            d = int(s.get("duration_minutes") or duration)
        except (TypeError, ValueError):
            d = duration
        services.append({"name": nm, "duration_minutes": d})

    # Named, individually-bookable resources (stylists / courts / rooms).
    resources = tuple(
        str((r.get("name") if isinstance(r, dict) else r) or "").strip()
        for r in (config.get("resources") or [])
        if str((r.get("name") if isinstance(r, dict) else r) or "").strip()
    )

    open_hours = config.get("open_hours") or {}
    slot_mode = str(config.get("slot_mode") or "").strip().lower()
    if slot_mode not in ("fixed", "open_hours"):
        slot_mode = "open_hours" if (open_hours and not slot_times) else "fixed"
    try:
        slot_interval = int(config.get("slot_interval_minutes") or 30)
    except (TypeError, ValueError):
        slot_interval = 30

    # Whether a range booking's start must sit on the interval grid (open-hours mode).
    # True → only open_start, +interval, +2·interval… are valid starts; False → any
    # start is fine as long as the block length is a whole multiple of the interval.
    raw_align = config.get("align_start_to_grid")
    if raw_align is None:
        align_start_to_grid = True
    elif isinstance(raw_align, str):
        align_start_to_grid = raw_align.strip().lower() not in ("false", "0", "no", "")
    else:
        align_start_to_grid = bool(raw_align)

    # Pooled capacity: absent/blank → 1; explicit 0 → unlimited. Ignored in resource mode.
    raw_cap = config.get("capacity_per_slot")
    if raw_cap in (None, ""):
        capacity = 1
    else:
        try:
            capacity = max(0, int(raw_cap))
        except (TypeError, ValueError):
            capacity = 1

    return {
        "locations": locations,
        "slot_times": slot_times,
        "duration_minutes": duration,
        "off_days": off_days,
        "slot_mode": slot_mode,
        "open_hours": open_hours,
        "slot_interval_minutes": slot_interval,
        "align_start_to_grid": align_start_to_grid,
        "services": services,
        "resources": resources,
        "capacity_per_slot": capacity,
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


def _hhmm_to_min(value: str) -> int:
    h, m = str(value).split(":")
    return int(h) * 60 + int(m)


def _min_to_hhmm(mins: int) -> str:
    mins = max(0, int(mins))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _intervals_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    """Half-open [start, end) overlap test."""
    return s1 < e2 and s2 < e1


def resolve_service(service: str, config: Optional[dict] = None) -> tuple[str, int]:
    """Return (canonical service name, duration_minutes). Name is "" when the brand
    defines no services (a single default service of the brand's default duration).
    Raises ValueError if a service is required but missing/unknown."""
    cfg = resolve_booking_config(config)
    services = cfg["services"]
    if not services:
        return "", cfg["duration_minutes"]
    value = (service or "").strip().lower()
    if not value:
        if len(services) == 1:
            return services[0]["name"], services[0]["duration_minutes"]
        raise ValueError("please choose a service: " + ", ".join(s["name"] for s in services))
    for s in services:
        if s["name"].strip().lower() == value:
            return s["name"], s["duration_minutes"]
    raise ValueError("service must be one of: " + ", ".join(s["name"] for s in services))


def resolve_resource(resource: str, config: Optional[dict] = None):
    """Canonical resource name, "" when the brand has no named resources, or None
    when resources exist but the caller didn't pick one (auto-assign the first free)."""
    cfg = resolve_booking_config(config)
    resources = cfg["resources"]
    if not resources:
        return ""
    value = (resource or "").strip().lower()
    if not value:
        return None
    for r in resources:
        if r.strip().lower() == value:
            return r
    raise ValueError("must be one of: " + ", ".join(resources))


def _prepare_booking(date, time, location, service, resource, config, end_time=None) -> dict:
    """Validate + normalise a requested booking against the brand's rules. Returns
    the resolved slot (with end_time and canonical service/resource/location) or
    raises ValueError describing what to fix.

    end_time (open-hours brands only): book the whole [start, end) block instead of the
    fixed service duration. The block length must be a whole multiple of the slot
    interval, and — when align_start_to_grid is set — the start must sit on the grid."""
    cfg = resolve_booking_config(config)
    location = normalize_trial_location(location, config)
    svc_name, duration = resolve_service(service, config)
    res = resolve_resource(resource, config)
    try:
        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("date and time must use YYYY-MM-DD and HH:MM") from exc
    if cfg["off_days"] and start_dt.weekday() in cfg["off_days"]:
        raise ValueError("bookings are unavailable on that day")
    start_min = _hhmm_to_min(time)
    # Range booking: caller supplied an end time → book the whole block; its length
    # overrides the fixed service duration. Only honoured in open-hours mode.
    range_mode = bool(end_time) and cfg["slot_mode"] == "open_hours"
    if range_mode:
        try:
            end_min = _hhmm_to_min(end_time)
        except Exception as exc:
            raise ValueError("end time must use HH:MM") from exc
        if end_min <= start_min:
            raise ValueError("the end time must be after the start time")
        duration = end_min - start_min
    else:
        end_min = start_min + duration
    if cfg["slot_mode"] == "fixed":
        if cfg["slot_times"] and time not in cfg["slot_times"]:
            raise ValueError("time must be one of: " + ", ".join(cfg["slot_times"]))
    else:
        oh = cfg["open_hours"]
        o_start = _hhmm_to_min(oh["start"]) if oh.get("start") else None
        o_end = _hhmm_to_min(oh["end"]) if oh.get("end") else None
        if o_start is not None and o_end is not None and (start_min < o_start or end_min > o_end):
            raise ValueError(f"time must be within {oh['start']}-{oh['end']}")
        if range_mode:
            interval = max(1, int(cfg["slot_interval_minutes"]))
            if duration % interval != 0:
                raise ValueError(
                    f"bookings are in {interval}-minute blocks, so the length must be a "
                    f"multiple of {interval} minutes — ask the caller to adjust the time"
                )
            if cfg["align_start_to_grid"] and o_start is not None and (start_min - o_start) % interval != 0:
                raise ValueError(
                    f"the start must fall on a {interval}-minute boundary from {oh['start']}"
                )
    if start_dt <= datetime.now(IST).replace(tzinfo=None):
        raise ValueError("the slot must be in the future")
    return {
        "cfg": cfg, "date": date, "time": time, "end_time": _min_to_hhmm(end_min),
        "location": location, "service": svc_name, "resource": res,
        "duration": duration, "start_min": start_min, "end_min": end_min,
    }


def _row_interval(row: dict) -> tuple[int, int]:
    """Booked [start, end) in minutes, using end_time or duration fallback."""
    start = _hhmm_to_min(row.get("time") or "00:00")
    end_time = row.get("end_time")
    if end_time:
        try:
            return start, _hhmm_to_min(end_time)
        except Exception:
            pass
    try:
        dur = int(row.get("duration_minutes") or 0)
    except (TypeError, ValueError):
        dur = 0
    return start, start + dur


def _overlapping_rows(start_min: int, end_min: int, rows: list) -> list:
    return [r for r in rows if _intervals_overlap(start_min, end_min, *_row_interval(r))]


def _evaluate_availability(cfg, requested_resource, start_min, end_min, rows):
    """Pure decision: is [start, end) bookable given the day's booked rows?
    Returns (ok, assigned_resource). For resource mode, auto-assigns the first
    free named resource when none was requested."""
    overlapping = _overlapping_rows(start_min, end_min, rows)
    if cfg["resources"]:
        if requested_resource:
            taken = any((r.get("resource") or "") == requested_resource for r in overlapping)
            return (not taken, requested_resource if not taken else None)
        for res in cfg["resources"]:
            if not any((r.get("resource") or "") == res for r in overlapping):
                return True, res
        return False, None
    cap = cfg["capacity_per_slot"]
    if cap == 0:
        return True, None
    return (len(overlapping) < cap, None)


async def _booked_rows(brand_id, date, location) -> list:
    db = await _adb()
    q = (
        db.table("trials").select("id, time, end_time, duration_minutes, resource, created_at")
        .eq("date", date).eq("status", "booked")
    )
    if brand_id:
        q = q.eq("brand_id", brand_id)
    if location:
        q = q.eq("location", location)
    return (await q.execute()).data or []


async def find_booking_slot(date, time, location, brand_id=None, config=None, service=None, resource=None, end_time=None):
    """Validate + check availability. Returns (ok, assigned_resource, info)."""
    info = _prepare_booking(date, time, location, service, resource, config, end_time)
    rows = await _booked_rows(brand_id, date, info["location"])
    ok, assigned = _evaluate_availability(
        info["cfg"], info["resource"], info["start_min"], info["end_min"], rows
    )
    return ok, assigned, info


async def check_trial_slot(
    date: str, time: str, location: str,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
    service: Optional[str] = None, resource: Optional[str] = None,
    end_time: Optional[str] = None,
) -> bool:
    """True if the requested booking fits (capacity not full / a resource is free)."""
    ok, _assigned, _info = await find_booking_slot(date, time, location, brand_id, config, service, resource, end_time)
    return ok


def _candidate_starts(cfg: dict, duration: int) -> list:
    """Start times to try when suggesting alternatives: the fixed slot list, or
    open-hours stepped by the configured interval and bounded by duration."""
    if cfg["slot_mode"] == "open_hours":
        oh = cfg["open_hours"]
        if not (oh.get("start") and oh.get("end")):
            return []
        o_start, o_end = _hhmm_to_min(oh["start"]), _hhmm_to_min(oh["end"])
        step = max(5, cfg["slot_interval_minutes"])
        out, t = [], o_start
        while t + duration <= o_end:
            out.append(_min_to_hhmm(t))
            t += step
        return out
    return list(cfg["slot_times"])


async def get_next_available_trial_slots(
    date: str, time: str, location: str, limit: int = 3,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
    service: Optional[str] = None, resource: Optional[str] = None,
    end_time: Optional[str] = None,
) -> list[str]:
    """Next available start times for the requested service/location/resource. When a
    custom range (end_time) was requested, suggest starts that fit that block length."""
    cfg = resolve_booking_config(config)
    try:
        _svc, duration = resolve_service(service, config)
    except ValueError:
        duration = cfg["duration_minutes"]
    # For a range request, look for starts where the full requested block is free.
    range_mode = bool(end_time) and cfg["slot_mode"] == "open_hours"
    if range_mode:
        try:
            req = _hhmm_to_min(end_time) - _hhmm_to_min(time)
            if req > 0:
                duration = req
            else:
                range_mode = False
        except Exception:
            range_mode = False
    try:
        requested = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        requested = datetime.now(IST).replace(tzinfo=None)
    start = max(requested, datetime.now(IST).replace(tzinfo=None))
    found: list[str] = []
    for day_offset in range(15):
        day = (start + timedelta(days=day_offset)).date()
        if cfg["off_days"] and day.weekday() in cfg["off_days"]:
            continue
        for slot_time in _candidate_starts(cfg, duration):
            candidate = datetime.strptime(f"{day.isoformat()} {slot_time}", "%Y-%m-%d %H:%M")
            if candidate <= start:
                continue
            cand_end = _min_to_hhmm(_hhmm_to_min(slot_time) + duration) if range_mode else None
            try:
                if await check_trial_slot(day.isoformat(), slot_time, location, brand_id, config, service, resource, cand_end):
                    found.append(f"{day.isoformat()} at {slot_time}")
                    if len(found) >= limit:
                        return found
            except ValueError:
                continue
    return found


def _survives_after_insert(cfg, info, our_id, assigned, rows) -> bool:
    """Deterministic post-insert race guard: order overlapping booked rows by
    (created_at, id) and keep only the first N that fit. Our row survives if it
    falls within that allowed set, so concurrent over-bookings roll themselves back."""
    overlapping = sorted(
        _overlapping_rows(info["start_min"], info["end_min"], rows),
        key=lambda r: (r.get("created_at") or "", r.get("id") or ""),
    )
    if cfg["resources"]:
        same = [r for r in overlapping if (r.get("resource") or "") == (assigned or "")]
        return bool(same) and same[0].get("id") == our_id
    cap = cfg["capacity_per_slot"]
    if cap == 0:
        return True
    return our_id in {r.get("id") for r in overlapping[:cap]}


async def insert_trial(
    name: str, phone: str, date: str, time: str, location: str,
    brand_id: Optional[str] = None, config: Optional[dict] = None,
    service: Optional[str] = None, resource: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    """Atomically book a slot. Returns the booking details (booking_id, service,
    resource, time, end_time, location). Raises TrialSlotUnavailable if full."""
    ok, assigned, info = await find_booking_slot(date, time, location, brand_id, config, service, resource, end_time)
    if not ok:
        raise TrialSlotUnavailable("that slot is fully booked")
    cfg = info["cfg"]
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    row = {
        "id": full_id, "booking_id": booking_id,
        "name": name.strip(), "phone": phone.strip(),
        "date": info["date"], "time": info["time"], "end_time": info["end_time"],
        "location": info["location"], "duration_minutes": info["duration"],
        "status": "booked", "created_at": _now_iso(),
    }
    if info["service"]:
        row["service"] = info["service"]
    if assigned:
        row["resource"] = assigned
    if brand_id:
        row["brand_id"] = brand_id
    await db.table("trials").insert(row).execute()
    # Post-insert re-check: if a concurrent booking pushed this slot over capacity
    # (or grabbed our resource), roll ours back so exactly the allowed number stand.
    try:
        rows = await _booked_rows(brand_id, info["date"], info["location"])
        survives = _survives_after_insert(cfg, info, full_id, assigned, rows)
    except Exception:
        survives = True  # never fail a real booking over a re-check glitch
    if not survives:
        try:
            await db.table("trials").update({"status": "cancelled"}).eq("id", full_id).execute()
        except Exception:
            pass
        raise TrialSlotUnavailable("that slot was just booked")
    return {
        "booking_id": booking_id, "service": info["service"], "resource": assigned or "",
        "location": info["location"], "time": info["time"], "end_time": info["end_time"],
    }


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


async def update_call_cost(call_id: str, estimated_cost: float) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"estimated_cost": estimated_cost}).eq("id", call_id).execute()
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

# Outcomes where the agent never actually connected/spoke with a person.
NON_CONNECTED_OUTCOMES = {"no_answer", "busy", "failed", "declined", "timeout"}


async def _fetch_call_rows_for_stats(db, brand_id: Optional[str]) -> list:
    """Fetch the lightweight columns stats needs. Tries to include estimated_cost,
    falling back if that column has not been migrated yet."""
    def _q(cols: str):
        q = db.table("call_logs").select(cols)
        return q.eq("brand_id", brand_id) if brand_id else q
    try:
        return (await _q("outcome, duration_seconds, timestamp, estimated_cost").execute()).data or []
    except Exception:
        return (await _q("outcome, duration_seconds, timestamp").execute()).data or []


async def get_stats(brand_id: Optional[str] = None) -> dict:
    db = await _adb()
    rows = await _fetch_call_rows_for_stats(db, brand_id)
    today_iso = datetime.now(IST).date().isoformat()

    total_calls    = len(rows)
    booked         = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations      = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    avg_dur        = sum(durations) / len(durations) if durations else 0
    booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)

    # Funnel: dialed → connected (answered) → booked.
    connected      = sum(1 for r in rows if (r.get("outcome") or "") not in NON_CONNECTED_OUTCOMES)
    answer_rate    = round((connected / total_calls * 100) if total_calls else 0, 1)
    callbacks_pending = sum(1 for r in rows if r.get("outcome") == "callback_requested")
    calls_today    = sum(1 for r in rows if (r.get("timestamp") or "")[:10] == today_iso)
    booked_today   = sum(1 for r in rows if r.get("outcome") == "booked" and (r.get("timestamp") or "")[:10] == today_iso)

    # Spend (USD), from the per-call estimated cost.
    total_cost       = sum(float(r.get("estimated_cost") or 0) for r in rows)
    cost_per_call    = (total_cost / total_calls) if total_calls else 0
    cost_per_booking = (total_cost / booked) if booked else 0

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

    # Trials: actual bookings, upcoming, and cancellations.
    trials_total = trials_upcoming = trials_cancelled = 0
    try:
        def _tq(cols: str):
            q = db.table("trials").select(cols)
            return q.eq("brand_id", brand_id) if brand_id else q
        trials = (await _tq("status, date").execute()).data or []
        trials_total = len(trials)
        trials_cancelled = sum(1 for t in trials if t.get("status") == "cancelled")
        trials_upcoming = sum(
            1 for t in trials if t.get("status") == "booked" and (t.get("date") or "") >= today_iso
        )
    except Exception:
        pass
    trial_cancel_rate = round((trials_cancelled / trials_total * 100) if trials_total else 0, 1)

    return {
        "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1), "booking_rate_percent": booking_rate,
        "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
        "connected": connected, "answer_rate_percent": answer_rate,
        "callbacks_pending": callbacks_pending,
        "calls_today": calls_today, "booked_today": booked_today,
        "total_cost": round(total_cost, 4), "cost_per_call": round(cost_per_call, 4),
        "cost_per_booking": round(cost_per_booking, 4),
        "trials_upcoming": trials_upcoming, "trials_cancelled": trials_cancelled,
        "trials_total": trials_total, "trial_cancel_rate_percent": trial_cancel_rate,
    }


async def get_brand_breakdown() -> list:
    """Per-brand performance for the dashboard comparison table."""
    db = await _adb()
    try:
        rows = (await db.table("call_logs").select("outcome, brand_id, estimated_cost").execute()).data or []
    except Exception:
        rows = (await db.table("call_logs").select("outcome, brand_id").execute()).data or []
    name_by_id = {b["id"]: b.get("name") for b in await get_all_brands()}
    agg: dict = {}
    for r in rows:
        bid = r.get("brand_id") or ""
        a = agg.setdefault(bid, {"calls": 0, "booked": 0, "cost": 0.0})
        a["calls"] += 1
        if r.get("outcome") == "booked":
            a["booked"] += 1
        a["cost"] += float(r.get("estimated_cost") or 0)
    out = []
    for bid, a in agg.items():
        out.append({
            "brand_id": bid,
            "name": name_by_id.get(bid) or ("Unassigned" if not bid else "Unknown brand"),
            "total_calls": a["calls"],
            "booked": a["booked"],
            "booking_rate_percent": round((a["booked"] / a["calls"] * 100) if a["calls"] else 0, 1),
            "total_cost": round(a["cost"], 4),
        })
    out.sort(key=lambda x: x["total_calls"], reverse=True)
    return out


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
    greeting: Optional[str] = None,
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
        "greeting": greeting,
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
