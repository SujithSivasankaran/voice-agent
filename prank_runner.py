"""Stand-alone prank caller — NOT part of the Fitcamp dashboard.

Dials one or more numbers, each up to 3 times (a cooldown between each), running a
short fun scripted call, then stops. It writes nothing to the call logs or any
Fitcamp data: prank jobs take agent.py's separate _run_prank path, which never
touches the database.

Bounded by design: at most 3 attempts PER NUMBER (hard-capped in code), each call
time-capped, and the person can always just hang up. The same number can't be
targeted twice in one run, and no more than --max-lines calls run at once so a real
incoming call still has a free line.

Usage (run where the LiveKit env vars are available, e.g. the VPS):
    python prank_runner.py --phone +9173XXXXXXXX
    python prank_runner.py --phone +9173XXXXXXXX --phone +9198XXXXXXXX      # two at once
    python prank_runner.py --phone +9173XXXXXXXX,+9198XXXXXXXX              # same thing
    python prank_runner.py --phone +9173XXXXXXXX --attempts 3 --cooldown 90 --persona fridge

Personas: fridge | pizza | nap | time  (omit for a random one each attempt)
"""

import argparse
import asyncio
import json
import os
import time
import uuid

from dotenv import load_dotenv

load_dotenv(override=False)

from livekit import api  # noqa: E402

# Hard safety cap: a prank can never place more than this many calls PER NUMBER.
HARD_CAP_ATTEMPTS = 3
# Longest we wait for one attempt's room to finish before moving on (safety net).
PER_ATTEMPT_TIMEOUT = 180


def _digits(phone: str) -> str:
    d = "".join(c for c in (phone or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d   # compare on the last 10 digits


async def _room_present(lk, name: str) -> bool:
    rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
    return any(r.name == name for r in rooms)


async def _one_attempt(lk, phone: str, trunk_id: str, persona: str, idx: int, total: int, line) -> None:
    async with line:   # hold a "phone line" for the duration of this call
        room = f"prank-{phone.replace('+', '')}-{uuid.uuid4().hex[:6]}"
        meta = json.dumps({"prank": True, "phone_number": phone, "trunk_id": trunk_id, "persona": persona or ""})
        await lk.room.create_room(api.CreateRoomRequest(name=room, metadata=meta))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room, metadata=meta)
        )
        print(f"{phone} [{idx}/{total}] calling … (room {room})")
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
        print(f"{phone} [{idx}/{total}] attempt finished")


async def _prank_one_number(lk, phone: str, trunk_id: str, attempts: int, cooldown: int, persona: str, line) -> None:
    for i in range(1, attempts + 1):
        await _one_attempt(lk, phone, trunk_id, persona, i, attempts, line)
        if i < attempts:
            print(f"{phone} cooldown {cooldown}s …")
            await asyncio.sleep(cooldown)
    print(f"{phone} done — stopped after {attempts} attempt(s).")


def _collect_numbers(values) -> list:
    """Flatten repeated/comma-separated --phone values, dropping duplicate people."""
    out, seen = [], set()
    for v in values or []:
        for piece in str(v).split(","):
            p = piece.strip()
            key = _digits(p)
            if p and key and key not in seen:
                seen.add(key)
                out.append(p)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="Fun prank caller (max 3 attempts per number).")
    ap.add_argument("--phone", action="append", required=True,
                    help="Number to call (repeat the flag, or comma-separate, for several)")
    ap.add_argument("--attempts", type=int, default=HARD_CAP_ATTEMPTS, help="Tries per number (max 3)")
    ap.add_argument("--cooldown", type=int, default=60, help="Seconds between a number's attempts")
    ap.add_argument("--persona", default="", help="fridge | pizza | nap | time (blank = random)")
    ap.add_argument("--max-lines", type=int, default=2,
                    help="Most calls ringing at once (keep below your Vobiz concurrency so inbound still works)")
    args = ap.parse_args()

    numbers = _collect_numbers(args.phone)
    if not numbers:
        raise SystemExit("No valid phone numbers given.")
    attempts = max(1, min(args.attempts, HARD_CAP_ATTEMPTS))
    cooldown = max(0, args.cooldown)
    max_lines = max(1, args.max_lines)
    trunk_id = os.environ.get("OUTBOUND_TRUNK_ID", "")

    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set — run where the LiveKit env vars are available.")
    if not trunk_id:
        raise SystemExit("OUTBOUND_TRUNK_ID is not set — can't place outbound calls.")

    print(f"Prank: {len(numbers)} number(s) — up to {attempts} attempt(s) each, {cooldown}s cooldown, "
          f"{max_lines} line(s) at once. Ctrl-C to stop.")
    line = asyncio.Semaphore(max_lines)   # never use more lines than this at once
    lk = api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        await asyncio.gather(*[
            _prank_one_number(lk, phone, trunk_id, attempts, cooldown, args.persona, line)
            for phone in numbers
        ])
    finally:
        await lk.aclose()
    print("All done.")


if __name__ == "__main__":
    asyncio.run(main())
