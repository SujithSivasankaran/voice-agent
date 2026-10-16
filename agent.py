"""
LiveKit agent worker — Gemini Live voice AI entrypoint.

Configuration comes exclusively from VPS environment variables.
No .env file is read in production; load_dotenv(override=False) is a local-dev
convenience only and never overrides variables already set in the process environment.
"""

import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

# load_dotenv(override=False) is safe on the VPS: if no .env file exists it is
# a no-op; if one is present locally it fills in UNSET variables only.
from dotenv import load_dotenv
load_dotenv(override=False)

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api
from livekit.agents import Agent, AgentSession, RoomInputOptions, metrics
from livekit.plugins import noise_cancellation, silero

from business import DEFAULT_VOICE, INBOUND, campaign_or_default
from db import init_db, log_call, log_error
from prompts import OPENING_INSTRUCTION, build_greeting, build_inbound_prompt, build_prompt
from tools import CallTools
from observability import record_call_usage, setup_langfuse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


async def _wait_for_sip_participant(ctx: "agents.JobContext", timeout: float = 15.0):
    """Wait for the inbound caller's SIP participant to join the room and return
    it (or None on timeout). Used for incoming calls, where the caller is the one
    joining rather than us dialing out."""
    for p in ctx.room.remote_participants.values():
        return p
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    @ctx.room.on("participant_connected")
    def _on_join(p):
        if not fut.done():
            fut.set_result(p)

    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def _caller_number(participant) -> str:
    """Extract the caller's phone number from a SIP participant's attributes."""
    attrs = getattr(participant, "attributes", None) or {}
    num = (attrs.get("sip.phoneNumber") or attrs.get("sip.from")
           or attrs.get("sip.from_number") or attrs.get("sip.trunkPhoneNumber") or "")
    if not num:
        ident = getattr(participant, "identity", "") or ""
        if ident.startswith("sip_"):
            num = ident.replace("sip_", "")
    return num


def _is_sip_participant(participant) -> bool:
    """Identify SIP callers without relying on a particular SDK enum version."""
    identity = (getattr(participant, "identity", "") or "").lower()
    attributes = getattr(participant, "attributes", None) or {}
    return identity.startswith("sip_") or any(
        str(key).lower().startswith("sip.") for key in attributes
    )


def _sip_failure_outcome(exc: Exception) -> tuple[str, str]:
    detail = str(exc)
    text = detail.lower()
    if "busy" in text or "486" in text:
        return "busy", detail
    if any(value in text for value in ("declin", "reject", "603")):
        return "declined", detail
    if any(value in text for value in ("timeout", "timed out", "no answer", "408", "480")):
        return "no_answer", detail
    return "failed", detail


async def _save_early_call(
    phone: str,
    lead_name: str,
    outcome: str,
    reason: str,
    campaign: Optional[str] = None,
) -> None:
    """Persist calls that finish before CallTools exists."""
    for attempt in range(1, 4):
        try:
            await log_call(
                phone_number=phone or "unknown",
                lead_name=lead_name,
                outcome=outcome,
                reason=reason,
                duration_seconds=0,
                campaign=campaign,
            )
            return
        except Exception as exc:
            logger.warning("Early call logging attempt %d failed (%s): %s", attempt, outcome, exc)
            if attempt < 3:
                await asyncio.sleep(0.25 * attempt)
    logger.error("Early call logging exhausted all retries: %s", phone or "unknown")


def _extract_transcript(session) -> str:
    """Build a readable transcript from the AgentSession chat history.

    This only formats text the model already produced during the call (Gemini
    Live emits user/assistant transcriptions into the chat context), so it adds
    no extra model or API cost. Returns "" if no usable history is found.
    """
    if session is None:
        return ""
    try:
        history = session.history
    except Exception:
        return ""

    items = getattr(history, "items", None)
    if items is None and hasattr(history, "to_dict"):
        try:
            items = (history.to_dict() or {}).get("items", [])
        except Exception:
            items = []

    lines: list[str] = []
    for item in (items or []):
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
        else:
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
        if role not in ("user", "assistant"):
            continue

        # Normalise content to plain text. Chat items may expose text_content,
        # a bare string, or a list mixing strings and non-text parts.
        text = ""
        if not isinstance(item, dict) and getattr(item, "text_content", None):
            text = item.text_content
        elif isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(part for part in content if isinstance(part, str))
        text = " ".join((text or "").split())
        if not text:
            continue

        speaker = "Agent" if role == "assistant" else "Caller"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


# ── Import Google plugin paths ────────────────────────────────────────────────

_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None
_gemini_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
    try:
        # Gemini TTS (Gemini API via GOOGLE_API_KEY) shares Gemini Live's prebuilt
        # voices, so session.say() can speak in the SAME voice as the live model.
        _gemini_tts = _gp.beta.GeminiTTS
        logger.info("Loaded google.beta.GeminiTTS")
    except AttributeError:
        logger.info("google.beta.GeminiTTS unavailable (needs livekit-plugins-google>=1.5)")
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
_deepgram_tts = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
    try:
        _deepgram_tts = _dg.TTS
    except AttributeError:
        pass
except ImportError:
    pass


# ── Session factory ───────────────────────────────────────────────────────────

def _build_session(
    tools: list,
    system_prompt: str,
    model: Optional[str] = None,
    voice: Optional[str] = None,
) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    model / voice override the VPS env vars for this specific call only.

    SILENCE-PREVENTION (all 3 required for Gemini Live):
    1. SessionResumptionConfig(transparent=True) — auto-reconnect on timeout
    2. ContextWindowCompressionConfig          — sliding window, prevents freeze
    3. RealtimeInputConfig(END_SENSITIVITY_LOW) — less aggressive VAD, 2s silence
    """
    gemini_model = model or os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = voice or os.environ.get("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.environ.get("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            from google.genai import types as _gt
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=int(os.environ.get("VAD_SILENCE_MS", "300")),
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied")
        except Exception as _cfg_err:
            logger.warning("Silence-prevention config failed (non-fatal): %s", _cfg_err)
            _realtime_input_cfg = None
            _session_resumption_cfg = None
            _ctx_compression_cfg = None

        realtime_kwargs: dict = dict(
            model=gemini_model, voice=gemini_voice, instructions=system_prompt,
        )
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        # TTS ATTACHED to the session for session.say() — used only for the INBOUND
        # greeting (outbound has no scripted opener). Deepgram (aura) streams first
        # audio in ~200ms but in a different voice; Gemini TTS matches the live voice
        # but is slower to start. Default to Deepgram for speed; set GREETING_TTS=gemini
        # to greet inbound callers in the matching Gemini voice instead. Either way the
        # other is used as a fallback if the preferred one is unavailable.
        session_kwargs: dict = {"llm": RealtimeClass(**realtime_kwargs)}
        prefer_gemini = os.environ.get("GREETING_TTS", "deepgram").strip().lower() == "gemini"

        def _gemini_greeting_tts():
            return _gemini_tts(
                model=os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
                voice_name=gemini_voice,
            )

        def _deepgram_greeting_tts():
            return _deepgram_tts(model=os.environ.get("DEEPGRAM_TTS_MODEL", "aura-asteria-en"))

        order = []
        if prefer_gemini:
            if _gemini_tts is not None:   order.append((f"Gemini TTS (voice={gemini_voice})", _gemini_greeting_tts))
            if _deepgram_tts is not None: order.append(("Deepgram (fast) fallback", _deepgram_greeting_tts))
        else:
            if _deepgram_tts is not None: order.append(("Deepgram (fast first-audio)", _deepgram_greeting_tts))
            if _gemini_tts is not None:   order.append((f"Gemini TTS (voice={gemini_voice}) fallback", _gemini_greeting_tts))
        for label, factory in order:
            try:
                session_kwargs["tts"] = factory()
                logger.info("Greeting TTS: %s", label)
                break
            except Exception as exc:
                logger.warning("Greeting TTS %s failed (%s)", label, exc)
        return AgentSession(**session_kwargs)

    if _google_llm is None:
        raise RuntimeError(
            "No Google AI backend found. "
            "Run: pip install 'livekit-plugins-google>=1.0'"
        )

    logger.info("SESSION: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    vad = silero.VAD.load()
    return AgentSession(stt=stt, llm=_google_llm(model=gemini_model), tts=tts, vad=vad)


# ── Main entrypoint ───────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext):
    """
    Per-call handler. Reads lead metadata from room/job, dials via SIP,
    then runs the Gemini Live agent for the full call lifecycle.
    """
    logger.info("Job received — room: %s", ctx.room.name)

    # ── Parse metadata ────────────────────────────────────────────────────────
    meta: dict = {}
    try:
        if ctx.job.metadata:
            meta.update(json.loads(ctx.job.metadata))
    except Exception:
        pass
    try:
        if ctx.room.metadata:
            meta.update(json.loads(ctx.room.metadata))
    except Exception:
        logger.warning("Could not parse room metadata as JSON")

    phone_number = meta.get("phone_number", "")
    lead_name    = meta.get("lead_name", "there")
    missed_date  = meta.get("missed_date", "")
    trunk_id     = meta.get("trunk_id") or os.environ.get("OUTBOUND_TRUNK_ID", "")
    # No outbound number means the caller dialled us (routed by a LiveKit dispatch rule).
    is_inbound   = not phone_number
    campaign     = INBOUND if is_inbound else campaign_or_default(meta.get("campaign"))

    trace_metadata = {
        "langfuse.session.id": ctx.room.name,
        "langfuse.trace.name": "voice-call",
        "call.direction": "inbound" if is_inbound else "outbound",
        "call.room": ctx.room.name,
        "campaign.id": str(meta.get("campaign_id") or ""),
        "campaign.name": campaign,
    }
    trace_provider = setup_langfuse(trace_metadata)

    await _log("info", f"Call starting — phone={phone_number} lead={lead_name} campaign={campaign}")

    # ── Connect to LiveKit room ───────────────────────────────────────────────
    try:
        await ctx.connect()
    except Exception as exc:
        await _save_early_call(phone_number, lead_name, "failed", f"room connection failed: {exc}", campaign)
        logger.exception("Could not connect to LiveKit room")
        return

    # A SIP participant leaving does not necessarily disconnect the local room,
    # so listen for both events from the moment we connect.
    call_ended = asyncio.Event()
    fallback_outcome = "completed"
    fallback_reason = "session ended"

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        nonlocal fallback_outcome, fallback_reason
        if _is_sip_participant(participant):
            logger.info("SIP participant disconnected: %s", getattr(participant, "identity", "unknown"))
            fallback_outcome = "caller_hangup"
            fallback_reason = "caller disconnected after answer"
            call_ended.set()

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*_args):
        call_ended.set()

    # ── Inbound: wait for the caller's SIP participant to join ─────────────────
    if is_inbound:
        caller = await _wait_for_sip_participant(ctx, timeout=15)
        if caller is None:
            await _log("warning", "Inbound job had no SIP participant; closing without starting AI")
            await _save_early_call("unknown", lead_name, "no_answer", "inbound job had no SIP participant", campaign)
            await ctx.room.disconnect()
            return
        caller_num = _caller_number(caller)
        if caller_num:
            phone_number = caller_num
        if getattr(caller, "name", ""):
            lead_name = caller.name
        await _log("info", f"📞 Incoming call from {phone_number or 'unknown caller'}")

    # ── System prompt: one fixed script per campaign ──────────────────────────
    if is_inbound:
        system_prompt = build_inbound_prompt(caller_phone=phone_number)
    else:
        system_prompt = build_prompt(campaign, lead_name, caller_phone=phone_number, missed_date=missed_date)

    # ── Build the agent session (before dialing, so its model connection can be
    #    warmed during the ring) ────────────────────────────────────────────────
    # Tara's voice is fixed in business.py (it overrides GEMINI_TTS_VOICE, as the brand voice did).
    session_voice = DEFAULT_VOICE
    try:
        tools_instance = CallTools(ctx, phone_number, lead_name, campaign=campaign, missed_date=missed_date)
        tool_list = tools_instance.tool_list()
        session = _build_session(tool_list, system_prompt, voice=session_voice)
    except Exception as exc:
        await _save_early_call(phone_number, lead_name, "failed", f"agent setup failed: {exc}", campaign)
        logger.exception("Could not build agent session")
        await ctx.room.disconnect()
        return
    tools_instance.session = session
    agent = Agent(instructions=system_prompt, tools=tool_list)
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(event):
        metrics.log_metrics(event.metrics)
        usage_collector.collect(event.metrics)

    def _start_session():
        return session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )

    # For outbound this holds the connection warm-up running during the ring; None
    # for inbound / already-present SIP (started inline once the call is up).
    warm_task: Optional[asyncio.Task] = None

    async def _cleanup_started_session():
        """Tear down a session that may have been warmed/started, on an early exit."""
        if warm_task is not None:
            warm_task.cancel()
            try:
                await warm_task
            except BaseException:
                pass
        try:
            await session.aclose()
        except Exception:
            pass

    # ── Dial out, warming the model connection during the ring ─────────────────
    sip_already_present = any(
        _is_sip_participant(p) for p in ctx.room.remote_participants.values()
    )

    if phone_number and not sip_already_present and not is_inbound:
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
            await _save_early_call(phone_number, lead_name, "failed", "outbound trunk is not configured", campaign)
            await _cleanup_started_session()
            await ctx.room.disconnect()
            return
        # Establish the Gemini connection + system-prompt prefill NOW, concurrently
        # with the ring, so the model is warm the instant the callee answers and the
        # opener is near-instant — instead of paying the whole cold start in-band
        # (which was the slow first response, every turn after it fast). Trade-off:
        # busy/no-answer calls incur the model's setup cost; accepted for outbound
        # responsiveness. The warm-up is torn down on any early exit below.
        warm_task = asyncio.create_task(_start_session())
        logger.info("Dialling %s via trunk %s … (warming model during ring)", phone_number, trunk_id)
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    participant_name=lead_name,
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered by %s", phone_number)
        except Exception as exc:
            await _log("error", f"SIP dial failed: {exc}", str(exc))
            outcome, reason = _sip_failure_outcome(exc)
            await _save_early_call(phone_number, lead_name, outcome, f"SIP dial failed: {reason}", campaign)
            await _cleanup_started_session()
            await ctx.room.disconnect()
            return
    else:
        logger.info("SIP participant already present — skipping dial-out")

    # If the callee hung up as/just after they answered, stop before proceeding.
    if call_ended.is_set():
        await _log("info", f"Call ended before AI session started — phone={phone_number}")
        await _save_early_call(phone_number, lead_name, "caller_hangup", "call ended before AI session started", campaign)
        await _cleanup_started_session()
        await ctx.room.disconnect()
        return

    try:
        # Ensure the session is live. Outbound warmed it during the ring (await the
        # task — usually already done); inbound / already-present SIP start it now.
        if warm_task is not None:
            await warm_task
        else:
            await _start_session()

        # Open the call the moment it connects. The two directions warm up differently:
        if is_inbound:
            # INBOUND: play a scripted opener instantly via the session's attached TTS
            # (Deepgram ~200 ms first audio) so the caller hears us the instant they're
            # connected. The inbound prompt tells the model the greeting was already
            # spoken, so it never greets twice. allow_interruptions=False lets the short
            # opener play cleanly over just-answered line noise / the caller's "hello".
            try:
                await session.say(build_greeting(), allow_interruptions=False)
            except Exception as exc:
                logger.warning("Greeting via say() failed (%s) — agent will greet reactively", exc)
        else:
            # OUTBOUND: don't wait for the callee to speak. The model connection was
            # already warmed during the ring (above), so this opening turn is generated
            # near-instantly instead of paying the cold start in-band after the callee's
            # first reply (which was the slow first response, every turn after it fast).
            # Proactively generating the opener also makes the agent open the call itself
            # per its script. Not awaited, so the opener plays while we wait on the call;
            # generate_reply() returns a handle synchronously (not a coroutine), so
            # calling it without await is correct.
            try:
                session.generate_reply(instructions=OPENING_INSTRUCTION)
            except Exception as exc:
                logger.warning("Proactive opening via generate_reply failed (%s) — agent will open reactively", exc)

        # SIP hang-up is the normal stop signal. This post-answer limit is only
        # a final guard against abandoned jobs.
        max_call_seconds = max(60, int(os.environ.get("MAX_CALL_DURATION_SECONDS", "600")))
        try:
            await asyncio.wait_for(call_ended.wait(), timeout=max_call_seconds)
        except asyncio.TimeoutError:
            fallback_outcome = "timeout"
            fallback_reason = f"call reached {max_call_seconds}s safety timeout"
            await _log("warning", f"Call hit {max_call_seconds}s safety timeout: {phone_number}")
    except Exception as exc:
        fallback_outcome = "failed"
        fallback_reason = f"agent session failed: {exc}"
        logger.exception("Agent session failed")
    finally:
        # Capture the conversation transcript while the session (and its chat
        # history) is still alive — before aclose() tears the connection down.
        transcript = _extract_transcript(session)
        # Explicitly closing AgentSession closes the Gemini realtime connection;
        # disconnecting the room alone is not a reliable billing boundary.
        try:
            await session.aclose()
        except Exception as exc:
            logger.warning("Agent session cleanup failed: %s", exc)
        await tools_instance.ensure_call_logged(fallback_outcome, fallback_reason)
        # Attach the transcript to whichever call_logs row was just written
        # (by end_call during the call, or ensure_call_logged above).
        await tools_instance.attach_transcript(transcript)
        usage_summary = usage_collector.get_summary()
        model_name = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
        estimated_cost = record_call_usage(
            trace_provider,
            usage_summary,
            model=model_name,
            metadata={
                "direction": trace_metadata["call.direction"],
                "room": ctx.room.name,
                "campaign_id": trace_metadata["campaign.id"],
            },
        )
        if estimated_cost is not None:
            logger.info("Estimated Gemini cost for %s: $%.6f", ctx.room.name, estimated_cost)
        # Persist all-in spend (Gemini + telephony) on the call row. Called even
        # when the Gemini estimate is unavailable, so telephony is still counted.
        await tools_instance.attach_cost(estimated_cost)
        # Flush while the job process and exporter worker are still alive.
        # A LiveKit shutdown callback runs too late: OTEL's atexit handler may
        # already have stopped the BatchSpanProcessor by then.
        if trace_provider is not None:
            flushed = trace_provider.force_flush(timeout_millis=10_000)
            logger.info("Langfuse call flush completed: %s", flushed)
        try:
            await ctx.room.disconnect()
        except Exception:
            pass
        await _log("info", f"Call ended — phone={phone_number}")


# ── Prewarm: load VAD model before first call ─────────────────────────────────

def prewarm(proc: agents.JobProcess) -> None:
    try:
        proc.userdata["vad"] = silero.VAD.load()
        logger.info("VAD prewarmed")
    except Exception as exc:
        logger.warning("VAD prewarm failed: %s", exc)


# ── Worker entry ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="outbound-caller",
        )
    )
