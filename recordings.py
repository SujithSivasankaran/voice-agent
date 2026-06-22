"""Reconcile Vobiz trunk recordings into private S3 storage."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from db import get_recent_calls_without_recording, set_call_recording

logger = logging.getLogger("outbound-recordings")
_sync_lock = asyncio.Lock()


def _normalize_s3_endpoint(value: str) -> str:
    """Use Supabase's direct Storage hostname when given the API hostname."""
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        return endpoint
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if host.endswith(".supabase.co") and not host.endswith(".storage.supabase.co"):
        direct_host = host.removesuffix(".supabase.co") + ".storage.supabase.co"
        if parsed.port:
            direct_host += f":{parsed.port}"
        endpoint = urlunsplit((parsed.scheme, direct_host, parsed.path, "", ""))
    return endpoint


def _config() -> dict[str, str]:
    return {
        "auth_id": os.environ.get("VOBIZ_AUTH_ID", "").strip(),
        "auth_token": os.environ.get("VOBIZ_AUTH_TOKEN", "").strip(),
        "access_key": os.environ.get("S3_ACCESS_KEY_ID", "").strip(),
        "secret_key": os.environ.get("S3_SECRET_ACCESS_KEY", "").strip(),
        "endpoint": _normalize_s3_endpoint(os.environ.get("S3_ENDPOINT_URL", "")),
        "region": os.environ.get("S3_REGION", "").strip() or "us-east-1",
        "bucket": os.environ.get("S3_BUCKET", "").strip(),
    }


def recording_sync_status() -> dict[str, bool]:
    cfg = _config()
    return {
        "vobiz_api_configured": bool(cfg["auth_id"] and cfg["auth_token"]),
        "s3_configured": bool(cfg["access_key"] and cfg["secret_key"] and cfg["bucket"]),
    }


def _utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _phone_key(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _recording_time(recording: dict[str, Any]) -> Optional[datetime]:
    end_ms = recording.get("recording_end_ms")
    try:
        if end_ms:
            return datetime.fromtimestamp(float(end_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        pass
    return _utc(recording.get("add_time"))


def _match_call(
    recording: dict[str, Any],
    calls: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    phones = {
        key for key in (
            _phone_key(recording.get("from_number")),
            _phone_key(recording.get("to_number")),
        ) if key
    }
    recorded_at = _recording_time(recording)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for call in calls:
        if _phone_key(call.get("phone_number")) not in phones:
            continue
        started_at = _utc(call.get("timestamp"))
        if not started_at or not recorded_at:
            continue
        # call_logs.timestamp is written when the call is finalized, so compare
        # it directly with the carrier recording's completion/add timestamp.
        delta = abs((recorded_at - started_at).total_seconds())
        # Allows for carrier finalization delay while preventing old calls to the
        # same number from being paired with a new recording.
        if delta <= 30 * 60:
            ranked.append((delta, call))
    return min(ranked, key=lambda item: item[0])[1] if ranked else None


def _s3_client(cfg: dict[str, str]):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        endpoint_url=cfg["endpoint"] or None,
        region_name=cfg["region"],
        # Supabase and many S3-compatible endpoints require
        # endpoint/bucket/key rather than bucket.endpoint/key.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


async def sync_vobiz_recordings() -> dict[str, int]:
    """Copy newly completed Vobiz recordings to S3 and attach dashboard rows."""
    if _sync_lock.locked():
        return {"fetched": 0, "archived": 0, "failed": 0}
    async with _sync_lock:
        cfg = _config()
        if not recording_sync_status()["vobiz_api_configured"]:
            return {"fetched": 0, "archived": 0, "failed": 0}
        if not recording_sync_status()["s3_configured"]:
            return {"fetched": 0, "archived": 0, "failed": 0}

        url = f"https://api.vobiz.ai/api/v1/Account/{cfg['auth_id']}/Recording/"
        headers = {
            "X-Auth-ID": cfg["auth_id"],
            "X-Auth-Token": cfg["auth_token"],
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, params={"limit": 100, "offset": 0})
            response.raise_for_status()
            payload = response.json()
            recordings = payload.get("objects", []) if isinstance(payload, dict) else []
            calls = await get_recent_calls_without_recording(limit=200)
            archived = 0
            failed = 0
            assigned_call_ids: set[str] = set()

            for recording in recordings:
                recording_url = recording.get("recording_url") or recording.get("record_url")
                recording_id = str(recording.get("recording_id") or "").strip()
                if not recording_url or not recording_id:
                    continue
                available_calls = [c for c in calls if c.get("id") not in assigned_call_ids]
                call = _match_call(recording, available_calls)
                if not call:
                    continue

                audio = await client.get(str(recording_url), headers=headers)
                audio.raise_for_status()
                fmt = str(recording.get("recording_format") or "mp3").lower()
                extension = "wav" if fmt == "wav" else "mp3"
                content_type = "audio/wav" if extension == "wav" else "audio/mpeg"
                key = f"vobiz-recordings/{call['id']}/{recording_id}.{extension}"

                s3 = _s3_client(cfg)
                try:
                    await asyncio.to_thread(
                        s3.put_object,
                        Bucket=cfg["bucket"],
                        Key=key,
                        Body=audio.content,
                        ContentType=content_type,
                    )
                except Exception as exc:
                    failed += 1
                    response = getattr(exc, "response", {}) or {}
                    error = response.get("Error", {}) if isinstance(response, dict) else {}
                    logger.error(
                        "S3 recording upload failed (code=%s, message=%s, bucket=%s, endpoint=%s, region=%s)",
                        error.get("Code") or type(exc).__name__,
                        error.get("Message") or str(exc),
                        cfg["bucket"],
                        cfg["endpoint"],
                        cfg["region"],
                    )
                    continue
                if await set_call_recording(call["id"], f"s3://{cfg['bucket']}/{key}"):
                    assigned_call_ids.add(call["id"])
                    archived += 1
                    logger.info("Archived Vobiz recording for call %s", call["id"])

        return {"fetched": len(recordings), "archived": archived, "failed": failed}


async def presigned_recording_url(recording_ref: str, expires_seconds: int = 900) -> str:
    """Return a short-lived URL for a private S3 recording."""
    if not recording_ref.startswith("s3://"):
        return recording_ref
    cfg = _config()
    path = recording_ref[5:]
    bucket, separator, key = path.partition("/")
    if not separator or not bucket or not key:
        raise ValueError("Invalid S3 recording reference")
    if bucket != cfg["bucket"]:
        raise ValueError("Recording bucket does not match configured bucket")
    s3 = _s3_client(cfg)
    return await asyncio.to_thread(
        s3.generate_presigned_url,
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )
