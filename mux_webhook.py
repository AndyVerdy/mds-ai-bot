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
    pre-existing videos row keyed by mux_upload_id."""
    asset_id = data.get("id")
    upload_id = data.get("upload_id")
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
    thumbnail_url, then trigger AssemblyAI."""
    asset_id = data.get("id")
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
    if not rows:
        log.warning("mux: asset.ready for unknown asset_id=%s — no row updated",
                    asset_id)
        return
    video_id = rows[0]["id"]

    # Trigger AssemblyAI in a background thread so the webhook returns
    # 200 fast. submit_transcription handles its own DB updates.
    threading.Thread(
        target=_submit_transcription_safe,
        args=(video_id,),
        daemon=True,
    ).start()


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
    if not asset_id:
        return
    _patch_video(
        {"mux_asset_id": f"eq.{asset_id}"},
        {"mux_status": "errored", "updated_at": _now_iso()},
    )


def _handle_static_renditions_ready(data: dict) -> None:
    """Audio-only MP4 rendition is ready. We don't strictly need to act
    on this since asset.ready already kicked off transcription, but we
    can log it for debugging."""
    asset_id = data.get("id")
    log.info("mux: static_renditions ready for asset_id=%s", asset_id)


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
