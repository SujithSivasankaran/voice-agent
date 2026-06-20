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

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions, metrics
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, get_agent_profile, get_active_campaigns, get_default_prompt
from prompts import build_prompt, build_inbound_prompt
from tools import AppointmentTools
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


# ── Import Google plugin paths ────────────────────────────────────────────────

_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

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

    model / voice override the VPS env vars for this specific call only —
    used by agent profiles without mutating os.environ.

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
                    silence_duration_ms=int(os.environ.get("VAD_SILENCE_MS", "200")),
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

        # Attach a TTS so session.say() can speak a fixed line (e.g. the inbound
        # greeting) — the realtime model itself ignores say(). The TTS is only
        # used when we explicitly call say(); normal conversation stays on Gemini.
        session_kwargs: dict = {"llm": RealtimeClass(**realtime_kwargs)}
        if _deepgram_tts is not None:
            try:
                session_kwargs["tts"] = _deepgram_tts(
                    model=os.environ.get("DEEPGRAM_TTS_MODEL", "aura-asteria-en")
                )
            except Exception as exc:
                logger.warning("Could not attach greeting TTS (%s)", exc)
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

    phone_number     = meta.get("phone_number", "")
    lead_name        = meta.get("lead_name", "there")
    # Organisation identity is fixed and must not be overridden by call metadata.
    business_name    = "Harry's Fitcamp"
    service_type     = meta.get("service_type", "our service")
    custom_prompt    = meta.get("system_prompt") or meta.get("custom_prompt")
    agent_profile_id = meta.get("agent_profile_id")
    trunk_id         = meta.get("trunk_id") or os.environ.get("OUTBOUND_TRUNK_ID", "")

    trace_metadata = {
        "langfuse.session.id": ctx.room.name,
        "langfuse.trace.name": "harrys-fitcamp-voice-call",
        "call.direction": "outbound" if phone_number else "inbound",
        "call.room": ctx.room.name,
        "campaign.id": str(meta.get("campaign_id") or ""),
        "campaign.name": str(meta.get("campaign_name") or ""),
    }
    trace_provider = setup_langfuse(trace_metadata)

    await _log("info", f"Call starting — phone={phone_number} lead={lead_name}")

    # ── Load agent profile (no os.environ writes) ─────────────────────────────
    profile_model: Optional[str] = None
    profile_voice: Optional[str] = None

    if agent_profile_id:
        try:
            profile = await get_agent_profile(agent_profile_id)
            if profile:
                profile_model = profile.get("model") or None
                profile_voice = profile.get("voice") or None
                if profile.get("system_prompt") and not custom_prompt:
                    custom_prompt = profile["system_prompt"]
                if profile.get("enabled_tools"):
                    import json as _j
                    try:
                        pt = _j.loads(profile["enabled_tools"])
                        if pt:
                            meta["enabled_tools"] = pt
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Could not load agent profile %s: %s", agent_profile_id, exc)

    # ── Connect to LiveKit room ───────────────────────────────────────────────
    await ctx.connect()

    # A SIP participant leaving does not necessarily disconnect the local room,
    # so listen for both events from the moment we connect.
    call_ended = asyncio.Event()

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        if _is_sip_participant(participant):
            logger.info("SIP participant disconnected: %s", getattr(participant, "identity", "unknown"))
            call_ended.set()

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*_args):
        call_ended.set()

    # ── Inbound call? (no outbound number means the caller dialled us) ─────────
    # Inbound calls are routed here by a LiveKit dispatch rule; the caller's SIP
    # participant joins the room instead of us dialing out.
    is_inbound = not phone_number
    if is_inbound:
        caller = await _wait_for_sip_participant(ctx, timeout=15)
        if caller is None:
            await _log("warning", "Inbound job had no SIP participant; closing without starting AI")
            await ctx.room.disconnect()
            return
        caller_num = _caller_number(caller)
        if caller_num:
            phone_number = caller_num
        if getattr(caller, "name", ""):
            lead_name = caller.name
        await _log("info", f"📞 Incoming call from {phone_number or 'unknown caller'}")

    # ── Build system prompt ───────────────────────────────────────────────────
    # Incoming calls use the inbound (reception) prompt + summaries of every active
    # campaign (so callers can ask about anything running). Outgoing calls use the
    # campaign script passed in via metadata, else the default outreach prompt.
    if is_inbound:
        active_summaries = ""
        try:
            actives = await get_active_campaigns()
            active_summaries = "\n".join(
                f"• {a.get('name')}: {a.get('summary')}"
                for a in actives if a.get("summary")
            )
        except Exception as exc:
            logger.warning("Could not load active campaigns for inbound: %s", exc)
        system_prompt = build_inbound_prompt(lead_name, business_name, service_type, custom_prompt, active_summaries)
    else:
        # Campaign/custom prompt from metadata wins; otherwise the editable default
        # base (Supabase), falling back to the built-in DEFAULT_SYSTEM_PROMPT.
        base = custom_prompt
        if not base:
            try:
                base = await get_default_prompt()
            except Exception:
                base = None
        system_prompt = build_prompt(lead_name, business_name, service_type, base)

    # ── Dial out before starting Gemini ───────────────────────────────────────
    sip_already_present = any(
        _is_sip_participant(p) for p in ctx.room.remote_participants.values()
    )

    if phone_number and not sip_already_present and not is_inbound:
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
            await ctx.room.disconnect()
            return
        else:
            logger.info("Dialling %s via trunk %s …", phone_number, trunk_id)
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
                await ctx.room.disconnect()
                return
    else:
        logger.info("SIP participant already present — skipping dial-out")

    # Avoid Gemini charges for ringing, busy, and no-answer time. The realtime
    # model connection is created only after wait_until_answered succeeds.
    if call_ended.is_set():
        await _log("info", f"Call ended before AI session started — phone={phone_number}")
        await ctx.room.disconnect()
        return

    enabled_tools = meta.get("enabled_tools") or await get_enabled_tools()
    tools_instance = AppointmentTools(ctx, phone_number, lead_name)
    tool_list = tools_instance.build_tool_list(enabled_tools)
    session = _build_session(tool_list, system_prompt, model=profile_model, voice=profile_voice)
    tools_instance.session = session
    agent = Agent(instructions=system_prompt, tools=tool_list)
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(event):
        metrics.log_metrics(event.metrics)
        usage_collector.collect(event.metrics)

    try:
        await session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )

        if is_inbound:
            greeting = "Hi, this is Tina from Harry's Fitcamp. How can I help you?"
            try:
                await session.say(greeting, allow_interruptions=True)
            except Exception as exc:
                logger.warning("Inbound greeting via say() failed (%s) — agent will greet reactively", exc)

        # SIP hang-up is the normal stop signal. This post-answer limit is only
        # a final guard against abandoned jobs.
        max_call_seconds = max(60, int(os.environ.get("MAX_CALL_DURATION_SECONDS", "600")))
        try:
            await asyncio.wait_for(call_ended.wait(), timeout=max_call_seconds)
        except asyncio.TimeoutError:
            await _log("warning", f"Call hit {max_call_seconds}s safety timeout: {phone_number}")
    finally:
        # Explicitly closing AgentSession closes the Gemini realtime connection;
        # disconnecting the room alone is not a reliable billing boundary.
        try:
            await session.aclose()
        except Exception as exc:
            logger.warning("Agent session cleanup failed: %s", exc)
        usage_summary = usage_collector.get_summary()
        model_name = profile_model or os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
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
