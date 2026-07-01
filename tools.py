import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    TrialSlotUnavailable, check_trial_slot, get_next_available_trial_slots,
    insert_trial, normalize_trial_location, resolve_service, find_booking_slot,
    log_call, update_call_transcript, update_call_cost, log_error,
    get_calls_by_phone, get_appointments_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
    get_active_campaigns,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(
        self, ctx: agents.JobContext, phone_number: Optional[str] = None,
        lead_name: Optional[str] = None, booking_config: Optional[dict] = None,
        brand_id: Optional[str] = None,
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        # Brand context: which business this call belongs to, and that brand's
        # booking rules (locations / slot times / duration). Empty → unconstrained defaults.
        self.booking_config = booking_config or {}
        self._brand_id = brand_id
        self._call_start_time = time.time()
        self._sip_domain = os.environ.get("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self._call_logged = False
        # id of the call_logs row, so the transcript can be attached after the
        # call fully ends (the most complete history is available only then).
        self._call_id: Optional[str] = None
        self._booking_confirmed = False
        # Set by the entrypoint after the AgentSession is built, so end_call can
        # wait for the goodbye to finish playing before hanging up.
        self.session = None
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list, *, inbound: bool = False) -> list:
        """Return tools filtered by the enabled list.

        An empty list enables only tools used by the standard call flows.
        Optional integrations must be explicitly enabled by name so their
        schemas do not inflate every model turn.
        """
        default_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation,
            # self.lookup_contact,  # disabled for now — re-add to re-enable
            self.remember_details, self.lookup_campaign,
        ]
        optional_methods = [self.book_calcom, self.cancel_calcom]
        if not enabled:
            return default_methods
        all_methods = default_methods + optional_methods
        name_map = {m.__name__: m for m in all_methods}
        # Availability, atomic booking, and clean call termination are core
        # behavior and must remain available regardless of profile filtering.
        required = {"check_availability", "book_appointment", "end_call", "lookup_campaign"}
        selected = set(enabled) | required
        if inbound:
            selected.add("lookup_campaign")
        return [method for method in all_methods if method.__name__ in selected]

    @llm.function_tool
    async def lookup_campaign(self, query: str) -> str:
        """Find details of an active campaign only when a caller asks about it.
        query: campaign name, offer, or keywords mentioned by the caller
        """
        try:
            # Scope to this call's brand so one brand never surfaces another's campaigns.
            campaigns = await get_active_campaigns(self._brand_id)
            if not campaigns:
                return "No active campaigns were found. Offer to record a callback request."

            query_words = {
                word for word in "".join(
                    char.lower() if char.isalnum() else " " for char in query
                ).split() if len(word) > 2
            }

            def _score(campaign: dict) -> int:
                name = str(campaign.get("name") or "").lower()
                summary = str(campaign.get("summary") or "").lower()
                searchable = f"{name} {summary}"
                score = sum(2 if word in name else 1 for word in query_words if word in searchable)
                if query.strip().lower() in name:
                    score += 5
                return score

            ranked = sorted(campaigns, key=_score, reverse=True)
            best = ranked[0]
            if _score(best) <= 0:
                names = ", ".join(str(c.get("name")) for c in campaigns[:8])
                return f"No confident match. Active campaign names: {names}. Ask which one they mean."

            name = str(best.get("name") or "Campaign")
            summary = " ".join(str(best.get("summary") or "").split())
            if not summary:
                return f"{name} is active, but detailed information is unavailable. Offer a callback."
            # Keep retrieved context bounded; never inject a full campaign script.
            return f"{name}: {summary[:700]}"
        except Exception as exc:
            logger.warning("Campaign lookup failed: %s", exc)
            return "Campaign lookup is temporarily unavailable. Offer to record a callback request."

    def _slot_label(self, location: str, service: str = "", resource: str = "") -> str:
        """A human suffix for tool replies naming the service / resource / location
        that actually applied (resolving single-branch and single-service auto-fill)."""
        parts = []
        try:
            svc, _dur = resolve_service(service, self.booking_config)
        except Exception:
            svc = (service or "").strip()
        if svc:
            parts.append(svc)
        if resource and str(resource).strip():
            parts.append(f"with {str(resource).strip()}")
        try:
            loc = normalize_trial_location(location, self.booking_config)
        except Exception:
            loc = (location or "").strip().upper()
        if loc:
            parts.append(f"at {loc}")
        return (" — " + ", ".join(parts)) if parts else ""

    @llm.function_tool
    async def check_availability(
        self, date: str, time: str, location: str = "", service: str = "", resource: str = "",
        end_time: str = "",
    ) -> str:
        """
        Check whether a slot is free before booking. Call after collecting the details.
        service: only when the business offers multiple services (use the named service).
        location: only when the business has more than one location.
        resource: only when the business lets the caller pick a person/court/room.
        end_time: only for open-hours businesses when the caller wants a longer block
          (e.g. 06:00 to 07:30). Pass the caller's requested end (HH:MM); the block length
          must be a whole multiple of the slot interval. Omit for a single standard slot.
        Omit any field that doesn't apply. date: YYYY-MM-DD | time: HH:MM (24-hour)
        """
        try:
            ok, assigned, _info = await find_booking_slot(
                date, time, location, self._brand_id, self.booking_config, service, resource,
                end_time or None,
            )
            where = self._slot_label(location, service, resource or (assigned or ""))
            span = f" to {end_time}" if end_time else ""
            if ok:
                free_with = f" ({assigned} is free)" if (assigned and not resource) else ""
                return f"available: {date} at {time}{span}{where}{free_with}"
            alternatives = await get_next_available_trial_slots(
                date, time, location, brand_id=self._brand_id, config=self.booking_config,
                service=service, resource=resource, end_time=end_time or None,
            )
            choices = ", ".join(alternatives) or "no preset slots — ask the caller for another time"
            return f"unavailable{where}: suggest one of these next available times: {choices}"
        except ValueError as exc:
            return f"invalid booking request: {exc}. Ask the caller to correct it."
        except Exception as exc:
            logger.error("Availability check failed: %s", exc)
            return "Unable to check availability right now. Do not claim the slot is available."

    @llm.function_tool
    async def book_appointment(
        self, name: str, phone: str, date: str, time: str,
        location: str = "", service: str = "", resource: str = "", end_time: str = "",
    ) -> str:
        """
        Atomically book the slot after the caller confirms every detail.
        service: only when the business offers multiple services.
        location: only when the business has more than one location.
        resource: only when the caller may pick a specific person/court/room.
        end_time: only for open-hours businesses booking a longer block — pass the
          caller's confirmed end (HH:MM); the block length must be a multiple of the slot
          interval. Omit for a single standard slot. (See check_availability.)
        Omit any field that doesn't apply. date: YYYY-MM-DD | time: HH:MM (24-hour)
        """
        try:
            result = await insert_trial(
                name, phone, date, time, location, self._brand_id, self.booking_config,
                service, resource, end_time or None,
            )
            self._booking_confirmed = True
            where = self._slot_label(result.get("location", ""), result.get("service", ""), result.get("resource", ""))
            span = f" to {end_time}" if end_time else ""
            return f"BOOKING CONFIRMED. ID: {result['booking_id']}. Booked {date} at {time}{span}{where}."
        except TrialSlotUnavailable:
            alternatives = await get_next_available_trial_slots(
                date, time, location, brand_id=self._brand_id, config=self.booking_config,
                service=service, resource=resource, end_time=end_time or None,
            )
            choices = ", ".join(alternatives) or "no preset slots — ask the caller for another time"
            return f"NOT BOOKED: that slot was just taken. Ask the caller to choose from: {choices}."
        except ValueError as exc:
            return f"NOT BOOKED: {exc}. Ask the caller to correct the booking details."
        except Exception as exc:
            logger.error("Booking failed: %s", exc)
            return "NOT BOOKED: technical issue saving the booking. Do not tell the caller it is confirmed."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS call this before the call ends.
        outcome: 'booked' | 'completed' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
        reason: brief description
        """
        duration = int(time.time() - self._call_start_time)
        try:
            self._call_id = await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name, outcome=outcome, reason=reason,
                duration_seconds=duration, recording_url=self.recording_url,
                brand_id=self._brand_id,
            )
            self._call_logged = True
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        # Let the spoken goodbye finish before hanging up so the call doesn't
        # cut off mid-sentence. Wait for the current speech to play out if the
        # session API supports it, then a short safety buffer.
        try:
            speech = getattr(self.session, "current_speech", None) if self.session else None
            if speech is not None and hasattr(speech, "wait_for_playout"):
                await speech.wait_for_playout()
        except Exception:
            pass
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
                    api.RoomParticipantIdentity(
                        room=self.ctx.room.name,
                        identity=identity,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to remove SIP participant %s: %s", identity, exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    async def ensure_call_logged(
        self,
        outcome: str = "completed",
        reason: str = "call disconnected",
    ) -> None:
        """Save one fallback dashboard row when the model did not call end_call."""
        if self._call_logged:
            return
        if self._booking_confirmed:
            outcome = "booked"
        for attempt in range(1, 4):
            try:
                self._call_id = await log_call(
                    phone_number=self.phone_number or "unknown",
                    lead_name=self.lead_name,
                    outcome=outcome,
                    reason=reason,
                    duration_seconds=int(time.time() - self._call_start_time),
                    recording_url=self.recording_url,
                    brand_id=self._brand_id,
                )
                self._call_logged = True
                logger.info("Fallback call log saved (%s): %s", outcome, self.phone_number)
                return
            except Exception as exc:
                logger.warning("Fallback call logging attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(0.25 * attempt)
        logger.error("Fallback call logging exhausted all retries: %s", self.phone_number)

    async def attach_transcript(self, transcript: Optional[str]) -> None:
        """Attach the conversation transcript to the already-logged call row.

        Called once the call has fully ended, since the most complete history is
        only available then. This is pure persistence of text the model already
        produced during the call — it makes no additional model/API calls."""
        if not transcript or not self._call_id:
            return
        try:
            await update_call_transcript(self._call_id, transcript)
            logger.info("Transcript attached to call %s (%d chars)", self._call_id, len(transcript))
        except Exception as exc:
            logger.warning("Could not save transcript for call %s: %s", self._call_id, exc)

    async def attach_cost(self, gemini_cost) -> None:
        """Store this call's all-in estimated spend (Gemini model + telephony) in
        USD on the call_logs row, so the dashboard can report total spend and cost
        per booking.

        Telephony = call minutes × TELEPHONY_COST_PER_MIN_USD. Set that env var to
        your Vobiz per-minute rate converted to USD (default ~$0.006 ≈ ₹0.50/min);
        set it to 0 to count Gemini only. LiveKit is treated as free."""
        if not self._call_id:
            return
        try:
            total = float(gemini_cost or 0.0)
            try:
                rate = float(os.environ.get("TELEPHONY_COST_PER_MIN_USD", "0.006") or 0)
            except ValueError:
                rate = 0.0
            if rate > 0:
                minutes = max(0.0, time.time() - self._call_start_time) / 60.0
                total += minutes * rate
            await update_call_cost(self._call_id, round(total, 6))
        except Exception as exc:
            logger.warning("Could not save cost for call %s: %s", self._call_id, exc)

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Connect the caller with the brand's team via SIP REFER.
        Use when the caller requests the team, is angry, or has a complex issue.
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
        except Exception as exc:
            return "I couldn't connect the team right now. Please apologize and offer to take a message."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send SMS confirmation after a successful booking. Skips silently if Twilio not configured.
        phone: lead's phone | message: text to send
        """
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        from_num = os.environ.get("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception as exc:
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Call at the START of every call before engaging.
        phone: the lead's phone number with country code
        Returns call history, appointments, and remembered details.
        """
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:10]:
                    lines.append(f"  • {m['insight']}")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  • {ts} — {c.get('outcome','?')}: {c.get('reason','')}")
            if appointments:
                lines.append(f"\nAPPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(f"  • {a.get('date')} {a.get('time')} — {a.get('service')} [{a.get('status')}]")
            return "\n".join(lines)
        except Exception as exc:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        Examples: "Prefers morning calls", "Has 2 kids, interested in family plan", "Callback in 2 weeks"
        insight: the detail to remember
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            if not api_key:
                return
            import google.genai as genai  # installed via google-genai package
            client = genai.Client(api_key=api_key)
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = (
                "Compress these notes about a sales contact into 3-5 concise bullets. "
                f"Keep all key facts.\n\n{bullet_list}"
            )
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash-lite"),
                    contents=prompt,
                ),
            )
            if response.text and response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """
        Book in Cal.com calendar after book_appointment succeeds.
        name: full name | email: lead's email | date: YYYY-MM-DD | start_time: HH:MM | notes: optional
        """
        api_key = os.environ.get("CALCOM_API_KEY", "")
        event_type_id = os.environ.get("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.environ.get("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone,
                          "responses": {"name": name, "email": email, "notes": notes},
                          "metadata": {"source": "T-800"}, "language": "en"},
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            uid = data.get("uid", "")
            return f"Cal.com booked. UID: {uid}"
        except Exception as exc:
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """
        Cancel a Cal.com booking by UID.
        booking_uid: from book_calcom | reason: optional
        """
        api_key = os.environ.get("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {},
                )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
