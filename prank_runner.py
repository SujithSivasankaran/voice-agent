"""Stand-alone prank caller — NOT part of the Fitcamp dashboard.

Dials one number up to 3 times (a cooldown between each), each time running a short
fun scripted call via the existing LiveKit agent, then stops. It writes nothing to
the call logs or any Fitcamp data: prank calls go down agent.py's separate _run_prank
path, which never touches the database.

The person can always just hang up, each call is time-capped, and the number of
attempts is hard-capped at 3 in code — so it stops on its own and can't be turned
into a flood.

Usage (run on the VPS or anywhere with the LiveKit env vars):
    python prank_runner.py --phone +9173XXXXXXXX
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

# Hard safety cap: a prank can never place more than this many calls, whatever is passed.
HARD_CAP_ATTEMPTS = 3
# Longest we wait for one attempt's room to finish before moving on (safety net).
PER_ATTEMPT_TIMEOUT = 180


async def _room_present(lk, name: str) -> bool:
    rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
    return any(r.name == name for r in rooms)


async def _one_attempt(lk, phone: str, trunk_id: str, persona: str, idx: int, total: int) -> None:
    room = f"prank-{phone.replace('+', '')}-{uuid.uuid4().hex[:6]}"
    meta = json.dumps({"prank": True, "phone_number": phone, "trunk_id": trunk_id, "persona": persona or ""})
    await lk.room.create_room(api.CreateRoomRequest(name=room, metadata=meta))
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room, metadata=meta)
    )
    print(f"[{idx}/{total}] calling {phone} … (room {room})")

    # Wait for the room to appear (agent joined + dialing) then disappear (call over).
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
    print(f"[{idx}/{total}] attempt finished")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Fun prank caller (max 3 attempts).")
    ap.add_argument("--phone", required=True, help="Number to call, e.g. +9173XXXXXXXX")
    ap.add_argument("--attempts", type=int, default=HARD_CAP_ATTEMPTS, help="How many times to try (max 3)")
    ap.add_argument("--cooldown", type=int, default=60, help="Seconds to wait between attempts")
    ap.add_argument("--persona", default="", help="fridge | pizza | nap | time (blank = random)")
    args = ap.parse_args()

    attempts = max(1, min(args.attempts, HARD_CAP_ATTEMPTS))
    cooldown = max(0, args.cooldown)
    phone = args.phone.strip()
    trunk_id = os.environ.get("OUTBOUND_TRUNK_ID", "")

    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set — run where the LiveKit env vars are available.")
    if not trunk_id:
        raise SystemExit("OUTBOUND_TRUNK_ID is not set — can't place outbound calls.")

    print(f"Prank: {phone} — up to {attempts} attempt(s), {cooldown}s cooldown. Ctrl-C to stop early.")
    lk = api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        for i in range(1, attempts + 1):
            await _one_attempt(lk, phone, trunk_id, args.persona, i, attempts)
            if i < attempts:
                print(f"cooldown {cooldown}s …")
                await asyncio.sleep(cooldown)
    finally:
        await lk.aclose()
    print(f"Done — stopped after {attempts} attempt(s).")


if __name__ == "__main__":
    asyncio.run(main())
