"""Optional Langfuse/OpenTelemetry tracing for LiveKit voice calls."""

import base64
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("outbound-observability")


def langfuse_status() -> dict[str, Any]:
    """Return secret-safe configuration diagnostics for the health endpoint."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    host = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    ).strip().rstrip("/")
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
        dependencies_installed = True
    except ImportError:
        dependencies_installed = False
    return {
        "configured": bool(public_key and secret_key),
        "public_key_set": bool(public_key),
        "secret_key_set": bool(secret_key),
        "base_url": host,
        "dependencies_installed": dependencies_installed,
    }


def setup_langfuse(metadata: dict[str, Any]) -> Optional[Any]:
    """Configure LiveKit OTEL export when Langfuse credentials are present."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    host = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    ).strip().rstrip("/")
    if not public_key or not secret_key:
        logger.warning(
            "Langfuse disabled (public_key_set=%s, secret_key_set=%s, base_url=%s)",
            bool(public_key),
            bool(secret_key),
            host,
        )
        return None

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from livekit.agents.telemetry import set_tracer_provider

        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        set_tracer_provider(provider, metadata=metadata)

        # Emit and flush a tiny root span immediately. This makes configuration
        # failures visible during the call instead of waiting for shutdown.
        tracer = provider.get_tracer("outbound-ai.langfuse-check")
        with tracer.start_as_current_span("langfuse-export-check") as span:
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.observation.metadata.room", metadata.get("call.room", ""))
        flushed = provider.force_flush(timeout_millis=10_000)
        logger.info(
            "Langfuse tracing enabled (endpoint=%s, initial_flush=%s)",
            f"{host}/api/public/otel/v1/traces",
            flushed,
        )
        return provider
    except Exception as exc:
        # Observability must never prevent a call from running.
        logger.warning("Langfuse setup failed; continuing without tracing: %s", exc)
        return None


def record_call_usage(
    provider: Any,
    summary: Any,
    *,
    model: str,
    metadata: dict[str, Any],
) -> Optional[float]:
    """Attach a per-call Gemini usage/cost summary without double-counting generations."""
    if provider is None or summary is None:
        return None

    input_text = int(getattr(summary, "llm_input_text_tokens", 0) or 0)
    input_audio = int(getattr(summary, "llm_input_audio_tokens", 0) or 0)
    output_text = int(getattr(summary, "llm_output_text_tokens", 0) or 0)
    output_audio = int(getattr(summary, "llm_output_audio_tokens", 0) or 0)
    total_input = int(getattr(summary, "llm_prompt_tokens", 0) or 0)
    total_output = int(getattr(summary, "llm_completion_tokens", 0) or 0)

    # Gemini 3.1 Flash Live paid-tier rates, USD per 1M tokens.
    costs = {
        "input_text": input_text * 0.75 / 1_000_000,
        "input_audio": input_audio * 3.00 / 1_000_000,
        "output_text": output_text * 4.50 / 1_000_000,
        "output_audio": output_audio * 12.00 / 1_000_000,
    }
    total_cost = sum(costs.values())
    usage = {
        "input_text": input_text,
        "input_audio": input_audio,
        "output_text": output_text,
        "output_audio": output_audio,
        "total_input": total_input,
        "total_output": total_output,
    }
    logger.info("Gemini usage breakdown: %s", json.dumps(usage, sort_keys=True))

    try:
        tracer = provider.get_tracer("outbound-ai.call-cost")
        with tracer.start_as_current_span("gemini-call-usage-summary") as span:
            # Keep this a normal span. LiveKit's generation spans already contain
            # provider usage; marking this as another generation would double cost.
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.observation.metadata.model", model)
            span.set_attribute("langfuse.observation.metadata.usage", json.dumps(usage))
            span.set_attribute("langfuse.observation.metadata.cost_breakdown_usd", json.dumps(costs))
            span.set_attribute("langfuse.observation.metadata.estimated_cost_usd", total_cost)
            for key, value in metadata.items():
                if value is not None:
                    span.set_attribute(f"langfuse.observation.metadata.{key}", value)
        return total_cost
    except Exception as exc:
        logger.warning("Could not record per-call usage summary: %s", exc)
        return None
