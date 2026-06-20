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
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, get_agent_profile
from prompts import build_prompt
from tools import AppointmentTools

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
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
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
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=600,
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

        return AgentSession(llm=RealtimeClass(**realtime_kwargs))

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
    business_name    = meta.get("business_name", "our company")
    service_type     = meta.get("service_type", "our service")
    custom_prompt    = meta.get("system_prompt") or meta.get("custom_prompt")
    agent_profile_id = meta.get("agent_profile_id")
    trunk_id         = meta.get("trunk_id") or os.environ.get("OUTBOUND_TRUNK_ID", "")

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

    # ── Build system prompt ───────────────────────────────────────────────────
    system_prompt = build_prompt(lead_name, business_name, service_type, custom_prompt)

    # ── Connect to LiveKit room ───────────────────────────────────────────────
    await ctx.connect()

    # ── Build tool set ────────────────────────────────────────────────────────
    enabled_tools  = meta.get("enabled_tools") or await get_enabled_tools()
    tools_instance = AppointmentTools(ctx, phone_number, lead_name)
    tool_list      = tools_instance.build_tool_list(enabled_tools)

    # ── Build and start session ───────────────────────────────────────────────
    session = _build_session(tool_list, system_prompt, model=profile_model, voice=profile_voice)

    agent = Agent(instructions=system_prompt, tools=tool_list)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    # ── Dial out if no SIP participant in the room yet ────────────────────────
    sip_already_present = any(
        "sip_" in p.identity for p in ctx.room.remote_participants.values()
    )

    if phone_number and not sip_already_present:
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
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

    # ── Speak opening line ────────────────────────────────────────────────────
    await session.generate_reply(
        instructions=f"Start immediately: 'Hi, am I speaking with {lead_name}?'"
    )

    # ── Wait for the room to close (SIP participant hangs up) ─────────────────
    # We listen for the room's "disconnected" event rather than calling
    # session.wait_for_close() which does not exist in livekit-agents 1.x.
    disconnect = asyncio.Event()

    @ctx.room.on("disconnected")
    def _on_room_disconnected():
        disconnect.set()

    try:
        await asyncio.wait_for(disconnect.wait(), timeout=7200)  # 2-hour hard cap
    except asyncio.TimeoutError:
        await _log("warning", f"Call hit 2-hour timeout: {phone_number}")
    finally:
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
