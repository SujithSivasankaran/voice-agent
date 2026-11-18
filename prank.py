"""Core logic for the standalone prank caller.

No FastAPI and no database — just the LiveKit dispatch and the bounded retry. Used by
both prank_runner.py (CLI) and the small admin-only /prank page in server.py. Prank
calls take agent.py's separate _run_prank path, which writes nothing to the Fitcamp
database.

Bounded by design: at most 3 attempts PER NUMBER (hard-capped here), each call
time-capped (agent side), the same person can't be targeted twice in one run, and no
more than `max_lines` calls ring at once.
"""

import asyncio
import json
import os
import time
import uuid
from typing import Callable, Optional

from livekit import api

HARD_CAP_ATTEMPTS = 3            # a prank can never place more than this many calls per number
PER_ATTEMPT_TIMEOUT = 180       # safety net: longest we wait for one call's room to finish
PERSONAS = ("fridge", "pizza", "nap", "time")


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
            if p and key and key not in seen:
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
            await asyncio.sleep(3)
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
                    attempts: int = HARD_CAP_ATTEMPTS, cooldown: int = 60, max_lines: int = 2,
                    emit: Optional[Callable[[str, str], None]] = None) -> None:
    """Prank every number concurrently, each up to `attempts` times (capped at 3)."""
    emit = emit or (lambda phone, msg: None)
    attempts = max(1, min(int(attempts), HARD_CAP_ATTEMPTS))
    cooldown = max(0, int(cooldown))
    line = asyncio.Semaphore(max(1, int(max_lines)))
    await asyncio.gather(*[
        _prank_one_number(lk, phone, trunk_id, attempts, cooldown, persona, line, emit)
        for phone in numbers
    ])
