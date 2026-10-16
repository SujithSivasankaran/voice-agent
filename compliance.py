"""
Telephony compliance guards.

An autodialer that places calls on a customer's behalf must not dial numbers that
have opted out, and must respect local calling-hour rules. This module centralises
those checks so every dial path (single call, batch, campaign) enforces them
identically. Two independent guards:

1. Do-Not-Call suppression — `is_suppressed()` consults the dnc_list table
   (persisted; see migrations/0002). A listed number is never dialed.

2. Calling window — `calling_window_status()` checks the current time in the
   deploy's configured timezone (APP_TIMEZONE, via db.now_local) against an allowed
   local-hour window and optional blocked weekdays. Defaults are permissive
   (00:00–23:59, no blocked days) so existing deploys are unaffected until an
   operator tightens them.

Env vars
--------
    CALLING_WINDOW_START   local start hour, "HH:MM" (default "00:00")
    CALLING_WINDOW_END     local end hour,   "HH:MM" (default "23:59")
    CALLING_BLOCKED_DAYS   comma list of weekday names/numbers to block, e.g.
                           "sun" or "sat,sun" or "0,6" (Mon=0 … Sun=6). Default none.
"""

import logging
import os
from datetime import time as _time

logger = logging.getLogger("compliance")

_WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "0": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "1": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "2": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "3": 3,
    "fri": 4, "friday": 4, "4": 4,
    "sat": 5, "saturday": 5, "5": 5,
    "sun": 6, "sunday": 6, "6": 6,
}


def _parse_hhmm(value: str, fallback: _time) -> _time:
    try:
        hh, mm = value.strip().split(":")
        return _time(hour=max(0, min(23, int(hh))), minute=max(0, min(59, int(mm))))
    except Exception:
        return fallback


def _blocked_weekdays() -> set:
    raw = os.environ.get("CALLING_BLOCKED_DAYS", "")
    days: set = set()
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in _WEEKDAY_ALIASES:
            days.add(_WEEKDAY_ALIASES[token])
    return days


def calling_window_status(when=None) -> tuple[bool, str]:
    """Return (allowed, reason). `when` defaults to now in the deploy's local zone.

    reason is "" when allowed, else a short human explanation for logs/UI.
    """
    from db import now_local  # local import avoids a circular import at module load

    when = when or now_local()
    start = _parse_hhmm(os.environ.get("CALLING_WINDOW_START", "00:00"), _time(0, 0))
    end = _parse_hhmm(os.environ.get("CALLING_WINDOW_END", "23:59"), _time(23, 59))

    if when.weekday() in _blocked_weekdays():
        return False, f"calls are blocked on {when.strftime('%A')} by CALLING_BLOCKED_DAYS"

    current = when.time()
    if start <= end:
        allowed = start <= current <= end
    else:
        # Window wraps past midnight (e.g. 20:00–06:00).
        allowed = current >= start or current <= end
    if not allowed:
        return False, (
            f"outside calling window {start.strftime('%H:%M')}–{end.strftime('%H:%M')} "
            f"(local time {current.strftime('%H:%M')})"
        )
    return True, ""


async def is_suppressed(phone: str) -> tuple[bool, str]:
    """Return (suppressed, reason). True if the number is on the DNC list. Fails OPEN (returns not-suppressed) on a lookup error so a
    transient DB glitch does not silently halt all calling — the error is logged."""
    from db import is_dnc
    try:
        entry = await is_dnc(phone)
    except Exception as exc:
        logger.warning("DNC lookup failed for %s (allowing call): %s", phone, exc)
        return False, ""
    if entry:
        return True, f"number is on the Do-Not-Call list ({entry.get('reason') or 'suppressed'})"
    return False, ""


async def check_dialable(phone: str) -> tuple[bool, str]:
    """Combined pre-dial gate: DNC first, then the calling window.
    Returns (ok_to_dial, reason). reason is "" when ok."""
    suppressed, why = await is_suppressed(phone)
    if suppressed:
        return False, why
    allowed, why = calling_window_status()
    if not allowed:
        return False, why
    return True, ""
