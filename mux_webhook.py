"""
MDS Video Platform — Mux webhook handler (M2).

When the admin app uploads via Mux Direct Upload, Mux fires lifecycle
events to this webhook. We update the videos row in Supabase and trigger
transcription once the asset is ready.

Events we handle:
  - video.upload.asset_created → asset is created from the upload, link
    asset_id to the upload_id-keyed videos row
  - video.asset.ready → encoding finished, set duration + playback_id +
    thumbnail, kick off AssemblyAI transcription

Other events (video.asset.errored, video.asset.deleted, etc.) are logged
but otherwise ignored for V1 — they can be wired into M5 admin polish.

Auth: Mux webhooks are signed with `Mux-Signature` HMAC-SHA256. We verify
when MUX_WEBHOOK_SECRET is set; fail-open if not (dev mode).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from videos import _supabase_get, _supabase_base, _supabase_key

log = logging.getLogger(__name__)


# ============================================================================
# Helpers
# ============================================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase_write(method: str, path: str, json: Optional[dict] = None,
                    params: Optional[dict] = None) -> Any:
    url = f"{_supabase_base()}/rest/v1/{path.lstrip('/')}"
    key = _supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    r = requests.request(method, url, headers=headers, json=json,
                         params=params or {}, timeout=15)
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def _patch_video(filter_params: dict, patch: dict) -> list:
    """PATCH videos rows matching filter_params with the given patch dict.
    Returns the rows that were updated (for logging)."""
    return _supabase_write(
        "PATCH",
        "videos",
        json=patch,
        params=filter_params,
    ) or []


def _thumbnail_url(playback_id: str) -> str:
    return f"https://image.mux.com/{playback_id}/thumbnail.jpg?time=5"


# ============================================================================
# Signature verification
# ============================================================================
def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify a Mux webhook signature. The header looks like:
        Mux-Signature: t=<timestamp>,v1=<hex>
    where v1 is HMAC-SHA256(secret, f"{timestamp}.{raw_body}").

    Fails open (returns True) when MUX_WEBHOOK_SECRET is not configured —
    we want to receive webhooks during initial setup before Andy's
    generated the secret in the Mux dashboard.
    """
    secret = os.getenv("MUX_WEBHOOK_SECRET", "")
    if not secret:
        return True
    if not signature_header:
        return False

    parts = {}
    for kv in signature_header.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()

    timestamp = parts.get("t")
    sig = parts.get("v1")
    if not timestamp or not sig:
        return False

    payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ============================================================================
# Event handlers
# ============================================================================
def _handle_upload_asset_created(data: dict) -> None:
    """Asset has been created from a direct upload. Link asset_id to the
    pre-existing videos row keyed by mux_upload_id.

    NOTE: for video.upload.asset_created events, `data` is the Direct
    Upload object (per Mux's webhook-spec.json), so:
        data.id        -> the upload_id
        data.asset_id  -> the newly-created asset_id
    Earlier versions of this handler had these inverted, which silently
    dropped every upload.asset_created event (upload_id resolved to None,
    early-return fired)."""
    upload_id = data.get("id")
    asset_id = data.get("asset_id")
    if not asset_id or not upload_id:
        log.warning("mux: upload.asset_created missing fields: %r", data)
        return

    # Look for an existing row that the admin app pre-created.
    rows = _supabase_get(
        "videos",
        params={
            "mux_upload_id": f"eq.{upload_id}",
            "select": "id,mux_asset_id,organization_id",
            "limit": "1",
        },
    )

    if rows:
        if rows[0].get("mux_asset_id") == asset_id:
            return  # already linked
        _patch_video(
            {"mux_upload_id": f"eq.{upload_id}"},
            {
                "mux_asset_id": asset_id,
                "updated_at": _now_iso(),
            },
        )
        log.info("mux: linked asset_id=%s to videos.mux_upload_id=%s",
                 asset_id, upload_id)
        return

    # Recovery path: no pre-existing row (admin app failed to insert, or
    # the upload was created out-of-band via Mux dashboard). Create the
    # row now scoped to the MDS org. M3 transcription will then trigger
    # on asset.ready.
    log.warning("mux: no videos row for upload_id=%s — recovery insert", upload_id)
    mds_org_id = "8f218f47-7832-48b3-b8de-847e83633661"
    _supabase_write(
        "POST",
        "videos",
        json={
            "organization_id": mds_org_id,
            "title": f"Untitled (recovered) — {asset_id[:12]}",
            "mux_upload_id": upload_id,
            "mux_asset_id": asset_id,
            "mux_status": "preparing",
            "visibility": "public",
            "source_type": "upload",
        },
    )


def _handle_asset_ready(data: dict) -> None:
    """Encoding is done. Update mux_status, duration, playback_id,
    thumbnail_url. Transcription is intentionally NOT triggered here —
    asset.ready fires before the audio-only MP4 rendition is generated,
    so AssemblyAI would fetch a 404 audio URL and immediately mark the
    job failed. Transcription submit lives in static_renditions.ready
    instead, which fires once audio.m4a is actually fetchable."""
    asset_id = data.get("id")
    upload_id = data.get("upload_id")  # Present when the asset came from a Direct Upload.
    if not asset_id:
        log.warning("mux: asset.ready missing id: %r", data)
        return

    duration_sec = int(data.get("duration") or 0) or None
    playback_ids = data.get("playback_ids") or []
    public_pid: Optional[str] = None
    for pid in playback_ids:
        if pid.get("policy") == "public":
            public_pid = pid.get("id")
            break
    if public_pid is None and playback_ids:
        public_pid = playback_ids[0].get("id")

    patch: dict[str, Any] = {
        "mux_status": "ready",
        "updated_at": _now_iso(),
    }
    if duration_sec:
        patch["duration_sec"] = duration_sec
    if public_pid:
        patch["mux_playback_id"] = public_pid
        patch["thumbnail_url"] = _thumbnail_url(public_pid)

    rows = _patch_video(
        {"mux_asset_id": f"eq.{asset_id}"},
        patch,
    )

    # Fallback: if no row matched by asset_id, the prior video.upload.asset_created
    # event probably wasn't delivered (Mux webhook subscription may not include
    # video.upload.* events). Recover by looking up the row via mux_upload_id and
    # writing mux_asset_id at the same time so subsequent events find the row.
    if not rows and upload_id:
        log.warning(
            "mux: asset.ready had no row by asset_id=%s — fallback by upload_id=%s",
            asset_id, upload_id,
        )
        rows = _patch_video(
            {"mux_upload_id": f"eq.{upload_id}"},
            {**patch, "mux_asset_id": asset_id},
        )

    if not rows:
        log.warning("mux: asset.ready for unknown asset_id=%s upload_id=%s — no row updated",
                    asset_id, upload_id)
        return


def _submit_transcription_safe(video_id: str) -> None:
    """Best-effort wrapper around transcripts.submit_transcription that
    swallows errors. Failures are logged but don't crash the webhook."""
    try:
        import transcripts
        transcripts.submit_transcription(video_id)
    except Exception:
        log.exception("mux: failed to auto-trigger transcription for video=%s",
                      video_id)


def _handle_asset_errored(data: dict) -> None:
    """Encoding failed. Mark the row errored so admin sees it."""
    asset_id = data.get("id")
    upload_id = data.get("upload_id")
    if not asset_id:
        return
    patch = {"mux_status": "errored", "updated_at": _now_iso()}
    rows = _patch_video({"mux_asset_id": f"eq.{asset_id}"}, patch)
    if not rows and upload_id:
        # Same fallback rationale as _handle_asset_ready.
        _patch_video(
            {"mux_upload_id": f"eq.{upload_id}"},
            {**patch, "mux_asset_id": asset_id},
        )


def _handle_static_renditions_ready(data: dict) -> None:
    """Audio-only MP4 rendition (audio.m4a at stream.mux.com/<pid>/audio.m4a)
    is now fetchable. THIS is the trigger for AssemblyAI — submitting on
    asset.ready was too early and led to instant `failed` because the
    audio URL 404'd.

    Idempotent: skips submission if the row already has
    transcription_status='ready' or 'processing'. Resubmits if it's null
    or 'failed' (e.g., a prior attempt failed)."""
    asset_id = data.get("id")
    upload_id = data.get("upload_id")
    if not asset_id:
        log.warning("mux: static_renditions.ready missing id: %r", data)
        return

    rows = _supabase_get(
        "videos",
        params={
            "mux_asset_id": f"eq.{asset_id}",
            "select": "id,transcription_status",
            "limit": "1",
        },
    )
    if not rows and upload_id:
        rows = _supabase_get(
            "videos",
            params={
                "mux_upload_id": f"eq.{upload_id}",
                "select": "id,transcription_status",
                "limit": "1",
            },
        )
    if not rows:
        log.warning(
            "mux: static_renditions.ready for unknown asset_id=%s upload_id=%s",
            asset_id, upload_id,
        )
        return

    row = rows[0]
    video_id = row["id"]
    status = row.get("transcription_status")
    if status in ("ready", "processing"):
        log.info("mux: static_renditions.ready for video=%s status=%s — skip",
                 video_id, status)
        return

    log.info("mux: static_renditions.ready for video=%s — submitting AssemblyAI",
             video_id)
    threading.Thread(
        target=_submit_transcription_safe,
        args=(video_id,),
        daemon=True,
    ).start()


# ============================================================================
# Top-level dispatch
# ============================================================================
def handle_webhook(payload: dict) -> tuple[dict, int]:
    """Route a Mux webhook to the right handler. Heavy work runs inline —
    Mux retries on non-2xx with exponential backoff, so any handler that
    might take >5s should defer to a background thread (asset.ready does)."""
    event_type = payload.get("type") or ""
    data = payload.get("data") or {}

    log.info("mux: received event=%s", event_type)
    try:
        if event_type == "video.upload.asset_created":
            _handle_upload_asset_created(data)
        elif event_type == "video.asset.ready":
            _handle_asset_ready(data)
        elif event_type == "video.asset.errored":
            _handle_asset_errored(data)
        elif event_type == "video.asset.static_renditions.ready":
            _handle_static_renditions_ready(data)
        else:
            # Many events we don't care about (asset.created at upload
            # creation time, asset.updated, master.ready, etc.). Acknowledge
            # them with 200 so Mux doesn't retry.
            pass
    except Exception:
        log.exception("mux: handler crashed for event=%s", event_type)
        return {"ok": False, "error": "handler crashed"}, 500

    return {"ok": True}, 200
