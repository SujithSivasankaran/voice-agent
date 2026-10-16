import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import RunContext, llm

from business import MISSED_CLASS, MISSED_REASONS, INBOUND
from db import (
    TrialSlotUnavailable, is_trial_slot_free, next_free_trial_slots, book_trial,
    log_call, update_call_transcript, update_call_cost,
    record_missed_class, link_missed_class_call,
    add_contact_memory, add_dnc,
)

logger = logging.getLogger("call-tools")


class CallTools(llm.ToolContext):
    """The function tools the agent can use on a call."""

    def __init__(
        self, ctx: agents.JobContext, phone_number: Optional[str] = None,
        lead_name: Optional[str] = None, campaign: str = "", missed_date: str = "",
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        # trial | missed_class | inbound — decides the tool set and is stored on the call log.
        self.campaign = campaign
        self.missed_date = missed_date
        self._call_start_time = time.time()
        self._sip_domain = os.environ.get("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self._call_logged = False
        # id of the call_logs row, so the transcript can be attached after the
        # call fully ends (the most complete history is available only then).
        self._call_id: Optional[str] = None
        self._booking_confirmed = False
        # missed_classes rows saved during this call, linked to the call log once it exists.
        self._missed_rows: list[str] = []
        # Set by the entrypoint after the AgentSession is built, so end_call can
        # wait for the goodbye to finish playing before hanging up.
        self.session = None
        self._hangup_task: Optional[asyncio.Task] = None
        super().__init__(tools=[])

    def tool_list(self) -> list:
        """The fixed tool set for this call's campaign."""
        common = [self.end_call, self.transfer_to_human, self.mark_do_not_call]
        if self.campaign == MISSED_CLASS:
            return [self.record_missed_class_reason] + common
        booking = [self.check_availability, self.book_trial, self.send_sms_confirmation]
        if self.campaign == INBOUND:
            return booking + [self.remember_details] + common
        return booking + common

    # ── Trial booking ────────────────────────────────────────────────────────

    @llm.function_tool
    async def check_availability(self, date: str, time: str, location: str) -> str:
        """
        Check whether a trial slot is free before booking.
        date: YYYY-MM-DD | time: HH:MM (24-hour) | location: ADAYAR or ECR
        """
        try:
            if await is_trial_slot_free(date, time, location):
                return f"available: {date} at {time} at {location.upper()}"
            alternatives = await next_free_trial_slots(date, time, location)
            choices = ", ".join(alternatives) or "none in the next two weeks — ask for another branch"
            return f"unavailable: suggest one of these instead: {choices}"
        except ValueError as exc:
            return f"invalid request: {exc}. Ask the caller to choose again."
        except Exception as exc:
            logger.error("Availability check failed: %s", exc)
            return "Unable to check availability right now. Do not claim the slot is available."

    @llm.function_tool
    async def book_trial(self, name: str, date: str, time: str, location: str, phone: str = "") -> str:
        """
        Book the free one-hour trial after the caller has confirmed every detail.
        name: caller's name | date: YYYY-MM-DD | time: HH:MM (24-hour) | location: ADAYAR or ECR
        phone: only if the caller's number is unknown or they want a different one
        """
        try:
            result = await book_trial(name, phone or self.phone_number or "", date, time, location)
            self._booking_confirmed = True
            return (f"BOOKING CONFIRMED. ID: {result['booking_id']}. "
                    f"Trial on {result['date']} at {result['time']} at {result['location']}.")
        except TrialSlotUnavailable:
            alternatives = await next_free_trial_slots(date, time, location)
            choices = ", ".join(alternatives) or "none in the next two weeks"
            return f"NOT BOOKED: that slot was just taken. Ask the caller to choose from: {choices}."
        except ValueError as exc:
            return f"NOT BOOKED: {exc}. Ask the caller to correct the details."
        except Exception as exc:
            logger.error("Booking failed: %s", exc)
            return "NOT BOOKED: technical issue saving the booking. Do not tell the caller it is confirmed."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send an SMS confirmation after a successful booking. Skips silently if SMS is not set up.
        phone: caller's phone | message: text to send
        """
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        from_num = os.environ.get("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: not configured."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception:
            return "SMS delivery failed, but the booking is confirmed."

    # ── Missed class ─────────────────────────────────────────────────────────

    @llm.function_tool
    async def record_missed_class_reason(self, category: str, reason: str, coming_back: str = "") -> str:
        """
        Save why the member missed class. Call exactly once, before saying goodbye.
        category: sick_or_injured | work | travel | family | schedule | lost_motivation | cost | other | not_given
        reason: their reason in a few words, in English
        coming_back: when they said they'd be back at class; blank if they didn't say
        """
        if not self.phone_number:
            return "No phone number on this call — cannot save. Continue the conversation."
        if self._missed_rows:
            return "Already saved for this call. Do not call this again."
        category = (category or "").strip().lower()
        if category not in MISSED_REASONS:
            category = "other"
        try:
            row_id = await record_missed_class(
                self.phone_number, self.lead_name, category, reason, coming_back, self.missed_date,
            )
            self._missed_rows.append(row_id)
            return "Saved. Now encourage them briefly, thank them and end the call."
        except Exception as exc:
            logger.error("Could not save missed-class reason: %s", exc)
            return "Could not save right now. Continue the conversation normally."

    # ── Every call ───────────────────────────────────────────────────────────

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Save a message or useful detail for the team, e.g. "Wants a call back about changing to evening batch".
        insight: the detail to save
        """
        if not self.phone_number:
            return "Cannot save — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            return f"Saved: {insight}"
        except Exception:
            return "Could not save the detail."

    @llm.function_tool
    async def mark_do_not_call(self, reason: str = "caller_opt_out") -> str:
        """
        Add this caller's number to the Do-Not-Call list so they are never called again.
        Call this immediately whenever the caller asks not to be contacted, says
        "remove me", "stop calling", "do not call", or opts out in any way.
        reason: short note, e.g. "asked to stop calling"
        """
        if not self.phone_number:
            return "No phone number on this call — cannot record the opt-out."
        try:
            await add_dnc(self.phone_number, reason=reason or "caller_opt_out", source="agent")
            return "Recorded. This number will not be called again. Apologise briefly and close the call."
        except Exception as exc:
            logger.warning("Failed to record opt-out for %s: %s", self.phone_number, exc)
            return "Could not record the opt-out right now, but assure the caller it will be handled."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Connect the caller with the Harry's Fitcamp team.
        Use when the caller asks for the team, is upset, or has a complex membership or billing issue.
        reason: why you're transferring
        """
        destination = os.environ.get("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "I can't connect the team right now. Please offer to take a message."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "I couldn't connect the team right now. Please apologize and offer to take a message."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination, play_dialtone=False,
                )
            )
            return "Connecting you with our team now. Please hold."
        except Exception:
            return "I couldn't connect the team right now. Please apologize and offer to take a message."

    @llm.function_tool
    async def end_call(self, context: RunContext, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS say a short goodbye to the caller first, then call this.
        outcome: 'booked' | 'completed' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
        reason: brief description
        """
        try:
            await self._log_call(outcome, reason)
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        # Let any goodbye spoken before this tool call finish playing.
        # (SpeechHandle.wait_for_playout() raises inside the owning tool; the
        # RunContext variant waits only for the speech that preceded the call.)
        try:
            await asyncio.wait_for(context.wait_for_playout(), timeout=15)
        except Exception:
            pass
        if self._goodbye_spoken():
            await self._drain_and_hang_up()
            return "Call ended."
        # Some models call end_call without saying goodbye. A reply generated inside
        # this tool would queue behind it, so speak the goodbye and hang up from a
        # background task once the tool has returned.
        if self._hangup_task is None:
            self._hangup_task = asyncio.create_task(self._say_goodbye_then_hang_up(outcome))
        return "Call ending. Do not say anything else."

    # ── Call bookkeeping (not tools) ─────────────────────────────────────────

    async def _log_call(self, outcome: str, reason: str) -> None:
        self._call_id = await log_call(
            phone_number=self.phone_number or "unknown",
            lead_name=self.lead_name, outcome=outcome, reason=reason,
            duration_seconds=int(time.time() - self._call_start_time),
            recording_url=self.recording_url, campaign=self.campaign or None,
        )
        self._call_logged = True
        for row_id in self._missed_rows:
            try:
                await link_missed_class_call(row_id, self._call_id)
            except Exception as exc:
                logger.warning("Could not link missed-class row %s: %s", row_id, exc)

    def _goodbye_spoken(self) -> bool:
        """True when the agent has spoken since the caller's last message."""
        try:
            items = self.session.history.items if self.session else []
        except Exception:
            return True
        for item in reversed(items):
            role = getattr(item, "role", None)
            if role == "assistant":
                return True
            if role == "user":
                return False
        return False

    async def _wait_until_quiet(self, timeout: float) -> None:
        """Wait until the agent is neither thinking nor speaking."""
        deadline = time.time() + timeout
        quiet_since = None
        while time.time() < deadline:
            state = getattr(self.session, "agent_state", None) if self.session else None
            if state in ("thinking", "speaking"):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since >= 0.6:
                return
            await asyncio.sleep(0.2)

    async def _say_goodbye_then_hang_up(self, outcome: str) -> None:
        try:
            # Let the tool turn settle; the model may still speak its own goodbye.
            await self._wait_until_quiet(timeout=8)
            if self.session is not None and not self._goodbye_spoken():
                closing = {
                    "callback_requested": "Acknowledge that they're busy and say you'll call back at a better time.",
                    "booked": "Confirm the booking briefly.",
                    "not_interested": "Respect their decision without pushing.",
                    "wrong_number": "Apologise for the mix-up.",
                }.get(outcome, "")
                handle = self.session.generate_reply(
                    instructions=(
                        "The call is ending now. Say one short, warm goodbye to the caller in the "
                        f"language of the conversation and thank them for their time. {closing} "
                        "Do not ask any questions."
                    ),
                    tool_choice="none",
                )
                await asyncio.wait_for(handle.wait_for_playout(), timeout=15)
        except Exception as exc:
            logger.warning("Could not speak goodbye before hanging up: %s", exc)
        await self._drain_and_hang_up()

    async def _drain_and_hang_up(self) -> None:
        try:
            await asyncio.sleep(float(os.environ.get("END_CALL_DRAIN_SECONDS", "2.0")))
        except Exception:
            pass
        # Force the SIP leg to hang up. Disconnecting only the local agent can
        # leave the caller connected to an otherwise empty LiveKit room.
        for participant in list(self.ctx.room.remote_participants.values()):
            identity = getattr(participant, "identity", "") or ""
            attributes = getattr(participant, "attributes", None) or {}
            is_sip = identity.lower().startswith("sip_") or any(
                str(key).lower().startswith("sip.") for key in attributes
            )
            if not is_sip:
                continue
            try:
                await self.ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(room=self.ctx.room.name, identity=identity)
                )
            except Exception as exc:
                logger.warning("Failed to remove SIP participant %s: %s", identity, exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass

    async def ensure_call_logged(self, outcome: str = "completed", reason: str = "call disconnected") -> None:
        """Save one fallback call-log row when the model did not call end_call."""
        if self._call_logged:
            return
        if self._booking_confirmed:
            outcome = "booked"
        for attempt in range(1, 4):
            try:
                await self._log_call(outcome, reason)
                logger.info("Fallback call log saved (%s): %s", outcome, self.phone_number)
                return
            except Exception as exc:
                logger.warning("Fallback call logging attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(0.25 * attempt)
        logger.error("Fallback call logging exhausted all retries: %s", self.phone_number)

    async def attach_transcript(self, transcript: Optional[str]) -> None:
        """Attach the conversation transcript to the already-logged call row. Pure
        persistence of text the model already produced — no extra model calls."""
        if not transcript or not self._call_id:
            return
        try:
            await update_call_transcript(self._call_id, transcript)
            logger.info("Transcript attached to call %s (%d chars)", self._call_id, len(transcript))
        except Exception as exc:
            logger.warning("Could not save transcript for call %s: %s", self._call_id, exc)

    async def attach_cost(self, gemini_cost) -> None:
        """Store this call's estimated spend (Gemini model + telephony) in USD.

        Telephony = call minutes × TELEPHONY_COST_PER_MIN_USD (default ~$0.006 ≈
        ₹0.50/min; 0 counts Gemini only). LiveKit is treated as free."""
        if not self._call_id:
            return
        try:
            total = float(gemini_cost or 0.0)
            try:
                rate = float(os.environ.get("TELEPHONY_COST_PER_MIN_USD", "0.006") or 0)
            except ValueError:
                rate = 0.0
            if rate > 0:
                total += max(0.0, time.time() - self._call_start_time) / 60.0 * rate
            await update_call_cost(self._call_id, round(total, 6))
        except Exception as exc:
            logger.warning("Could not save cost for call %s: %s", self._call_id, exc)
