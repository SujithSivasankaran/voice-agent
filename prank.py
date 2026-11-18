"""Core logic for the standalone prank caller.

No FastAPI and no database — just the LiveKit dispatch and the bounded retry. Used by
both prank_runner.py (CLI) and the small admin-only /prank page in server.py. Prank
calls take agent.py's separate _run_prank path, which writes nothing to the Fitcamp
database.

Bounded by design so it stays a prank, not a way to trap someone's line: the number
of attempts is configurable but capped at MAX_ATTEMPTS, calls are spaced by at least
MIN_COOLDOWN seconds, each call is time-capped (agent side), the person can hang up,
the same number can't be targeted twice in one run, and a job can be stopped at any
time (cancel the task running run_prank).
"""

import asyncio
import json
import os
import time
import uuid
from typing import Callable, Optional

from livekit import api

MAX_ATTEMPTS = 100               # safety ceiling: a prank can never place more than this many calls per number
MIN_COOLDOWN = 0               # safety floor: calls to one number are always spaced by at least this (seconds)
PER_ATTEMPT_TIMEOUT = 180       # safety net: longest we wait for one call's room to finish
PERSONAS = ("fridge", "pizza", "nap", "time")


def clamp_attempts(n) -> int:
    try:
        return max(1, min(int(n), MAX_ATTEMPTS))
    except (TypeError, ValueError):
        return 1


def clamp_cooldown(n) -> int:
    try:
        return max(MIN_COOLDOWN, min(int(n), 600))
    except (TypeError, ValueError):
        return MIN_COOLDOWN


def _digits(phone: str) -> str:
    d = "".join(c for c in (phone or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d   # compare people on the last 10 digits


def collect_numbers(values) -> list:
    """Flatten repeated / comma-separated / newline-separated numbers, dropping
    duplicate people (so the per-number cap can't be doubled)."""
    out, seen = [], set()
    for v in (values if isinstance(values, (list, tuple)) else [values]):
        for piece in str(v or "").replace("\n", ",").split(","):
            p = piece.strip()
            key = _digits(p)
            if p and key:
                seen.add(key)
                out.append(p)
    return out


def livekit_from_env():
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} is not set.")
    return api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )


async def _room_present(lk, name: str) -> bool:
    rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
    return any(r.name == name for r in rooms)


async def _one_attempt(lk, phone, trunk_id, persona, idx, total, line, emit) -> None:
    async with line:   # hold a "phone line" for the duration of this call
        room = f"prank-{phone.replace('+', '')}-{uuid.uuid4().hex[:6]}"
        meta = json.dumps({"prank": True, "phone_number": phone, "trunk_id": trunk_id, "persona": persona or ""})
        await lk.room.create_room(api.CreateRoomRequest(name=room, metadata=meta))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room, metadata=meta)
        )
        emit(phone, f"calling (attempt {idx} of {total})")
        deadline = time.time() + PER_ATTEMPT_TIMEOUT
        seen = False
        while time.time() < deadline:
            present = await _room_present(lk, room)
            if present:
                seen = True
            elif seen:
                break
            await asyncio.sleep(0)
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception:
            pass
        emit(phone, f"attempt {idx} finished")


async def _prank_one_number(lk, phone, trunk_id, attempts, cooldown, persona, line, emit) -> None:
    for i in range(1, attempts + 1):
        await _one_attempt(lk, phone, trunk_id, persona, i, attempts, line, emit)
        if i < attempts:
            emit(phone, f"waiting {cooldown}s before the next try")
            await asyncio.sleep(cooldown)
    emit(phone, f"done — stopped after {attempts} attempt(s)")


async def run_prank(lk, numbers: list, *, trunk_id: str, persona: str = "",
                    attempts: int = 3, cooldown: int = 60, max_lines: int = 2,
                    emit: Optional[Callable[[str, str], None]] = None) -> None:
    """Prank every number concurrently, each up to `attempts` times (capped at
    MAX_ATTEMPTS). Cancel the awaiting task to stop early."""
    emit = emit or (lambda phone, msg: None)
    attempts = clamp_attempts(attempts)
    cooldown = clamp_cooldown(cooldown)
    line = asyncio.Semaphore(max(1, int(3)))
    await asyncio.gather(*[
        _prank_one_number(lk, phone, trunk_id, attempts, cooldown, persona, line, emit)
        for phone in numbers
    ])
