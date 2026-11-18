"""Stand-alone prank caller CLI — NOT part of the Fitcamp dashboard.

Dials one or more numbers, each up to 3 times (a cooldown between), running a short
fun scripted call, then stops. Writes nothing to the Fitcamp database. There is also
a small admin-only web page for this at /prank; this CLI is the terminal equivalent.

Usage (run where the LiveKit env vars are available, e.g. the VPS):
    python prank_runner.py --phone +9173XXXXXXXX
    python prank_runner.py --phone +9173XXXXXXXX --phone +9198XXXXXXXX      # two at once
    python prank_runner.py --phone +9173XXXXXXXX,+9198XXXXXXXX              # same thing
    python prank_runner.py --phone +9173XXXXXXXX --attempts 3 --cooldown 90 --persona fridge

Personas: fridge | pizza | nap | time  (omit for a random one each attempt)
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

load_dotenv(override=False)

import prank  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description="Fun prank caller.")
    ap.add_argument("--phone", action="append", required=True,
                    help="Number to call (repeat the flag, or comma-separate, for several)")
    ap.add_argument("--attempts", type=int, default=3, help=f"Tries per number (max {prank.MAX_ATTEMPTS})")
    ap.add_argument("--cooldown", type=int, default=60, help="Seconds between a number's attempts")
    ap.add_argument("--persona", default="", help="fridge | pizza | nap | time (blank = random)")
    ap.add_argument("--max-lines", type=int, default=2,
                    help="Most calls ringing at once (keep below your Vobiz concurrency so inbound still works)")
    args = ap.parse_args()

    numbers = prank.collect_numbers(args.phone)
    if not numbers:
        raise SystemExit("No valid phone numbers given.")
    trunk_id = os.environ.get("OUTBOUND_TRUNK_ID", "")
    if not trunk_id:
        raise SystemExit("OUTBOUND_TRUNK_ID is not set — can't place outbound calls.")

    def emit(phone, msg):
        print(f"{phone}: {msg}")

    print(f"Prank: {len(numbers)} number(s), up to {prank.clamp_attempts(args.attempts)} each. Ctrl-C to stop.")
    try:
        lk = prank.livekit_from_env()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    try:
        await prank.run_prank(lk, numbers, trunk_id=trunk_id, persona=args.persona,
                              attempts=args.attempts, cooldown=args.cooldown,
                              max_lines=args.max_lines, emit=emit)
    finally:
        await lk.aclose()
    print("All done.")


if __name__ == "__main__":
    asyncio.run(main())
