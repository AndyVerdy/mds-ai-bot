"""
MDS Video Platform — M3 transcripts.

Pipeline:
  1. submit_transcription(video_id) sends a job to AssemblyAI with the Mux
     HLS URL as audio_url. AssemblyAI stores our webhook URL and posts
     back when done.
  2. handle_webhook() is called by Flask when AssemblyAI POSTs the
     completion. It verifies the shared secret, fetches the full result
     via REST, chunks utterances into ~30s windows with 5s overlap, and
     inserts rows into transcript_segments.
  3. chapters.py is invoked at the end of step 2 to ask Claude (via
     AssemblyAI's LLM Gateway) for chapter boundaries; chapter_title is
     written back onto the matching segments.

All routes are gated by ENABLE_VIDEO_PLATFORM in web.py — same flag used
by the rest of the video platform. AssemblyAI calls require
ASSEMBLYAI_API_KEY in env. Webhook auth uses a shared secret in
ASSEMBLYAI_WEBHOOK_SECRET.

Verified against https://www.assemblyai.com/docs/llms-full.txt 2026-05-07
per Operating Rule 12 of the AssemblyAI agent prompt:
  - speech_models: ordered fallback list. Required.
  - speaker_labels: bool, GA.
  - webhook_url + webhook_auth_header_name + _value: GA.
  - Authorization header: raw API key, NO Bearer prefix.
  - auto_chapters: deprecated → use LLM Gateway (see chapters.py).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import requests


def _now_iso() -> str:
    """ISO 8601 UTC timestamp string Postgres accepts via PostgREST PATCH.
    PostgREST won't evaluate `now()` from JSON values."""
    return datetime.now(timezone.utc).isoformat()

from videos import _supabase_get, _supabase_base, _supabase_key

log = logging.getLogger(__name__)


# ============================================================================
# Config
# ============================================================================
ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"


def _api_key() -> str:
    key = os.getenv("ASSEMBLYAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ASSEMBLYAI_API_KEY not configured. Add it to Render env vars "
            "before submitting transcriptions."
        )
    return key


def _webhook_secret() -> str:
    """Shared secret AssemblyAI sends back on the webhook so we can confirm
    the request is real. Set in Render env (ASSEMBLYAI_WEBHOOK_SECRET)."""
    return os.getenv("ASSEMBLYAI_WEBHOOK_SECRET", "")


def _webhook_url() -> str:
    """The public URL AssemblyAI POSTs to on completion."""
    base = os.getenv("PUBLIC_BACKEND_URL", "https://mds-ai-bot.onrender.com").rstrip("/")
    return f"{base}/api/webhooks/assemblyai"


# ============================================================================
# Mux audio URL helpers
# ============================================================================
def _audio_url_unsigned(playback_id: str) -> str:
    """Mux audio-only MP4 rendition URL (unsigned form, public-policy
    playback ID). AssemblyAI rejects HLS .m3u8 manifests outright
    (`File type is audio/x-mpegurl ... does not contain audio`), so we
    use the static audio rendition instead. Each Mux asset must have
    mp4_support='audio-only' (or 'audio-only,capped-1080p') enabled —
    set on existing assets via PUT /assets/<id>/mp4-support and on new
    assets at creation time (M2 admin upload defaults it on)."""
    return f"https://stream.mux.com/{playback_id}/audio.m4a"


def _audio_url_for(video_row: dict) -> Optional[str]:
    """Returns the audio.m4a URL we should hand to AssemblyAI. Q16
    hardening: prefer the signed playback ID + JWT-signed URL (TTL ~30
    min — AAI fetches once at job start). Fall back to the public-policy
    bare URL for legacy assets that haven't been backfilled yet, or when
    MUX_SIGNING_KEY isn't configured (dev / pre-Q16)."""
    signed_pid = video_row.get("mux_signed_playback_id")
    public_pid = video_row.get("mux_playback_id")
    if signed_pid:
        # Local import to keep transcripts.py importable in environments
        # where mux_signer's PyJWT dep hasn't been installed yet (e.g.
        # narrow test slices).
        from mux_signer import sign_audio_url
        return sign_audio_url(signed_pid)
    if public_pid:
        return _audio_url_unsigned(public_pid)
    return None


# ============================================================================
# Supabase helpers (write-side via PostgREST)
# ============================================================================
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


def _update_video(video_id: str, patch: dict) -> None:
    """PATCH a single videos row by id."""
    _supabase_write(
        "PATCH",
        f"videos",
        json=patch,
        params={"id": f"eq.{video_id}"},
    )


def _insert_transcript_segments(rows: list[dict]) -> None:
    """Bulk insert into transcript_segments. PostgREST handles arrays."""
    if not rows:
        return
    _supabase_write("POST", "transcript_segments", json=rows)


def _delete_transcript_segments(video_id: str) -> None:
    """Remove all transcript_segments rows for a video. Used when
    re-transcribing — the old segments are stale and we replace them
    with the new run's output."""
    _supabase_write(
        "DELETE",
        "transcript_segments",
        params={"video_id": f"eq.{video_id}"},
    )


# ============================================================================
# AssemblyAI submit
# ============================================================================
def submit_transcription(video_id: str) -> dict:
    """Look up the video row, build the audio URL from its Mux playback_id,
    and submit a transcription job to AssemblyAI. Returns the AssemblyAI
    response (includes id + status). Updates the videos row with
    assemblyai_transcript_id and transcription_status='processing'.

    Idempotency: if videos.assemblyai_transcript_id already exists,
    refuses to re-submit unless force=True is passed (caller's choice).
    """
    rows = _supabase_get(
        "videos",
        params={
            "id": f"eq.{video_id}",
            "select": "id,mux_playback_id,mux_signed_playback_id,mux_status,"
                       "transcription_status,assemblyai_transcript_id,"
                       "organization_id",
            "limit": "1",
        },
    )
    if not rows:
        raise ValueError(f"video {video_id} not found")
    v = rows[0]

    if v["mux_status"] != "ready":
        raise ValueError(f"video {video_id} mux_status={v['mux_status']!r}, "
                          f"not ready to transcribe")
    if not (v.get("mux_signed_playback_id") or v.get("mux_playback_id")):
        raise ValueError(f"video {video_id} missing both signed and public "
                          f"playback IDs")

    audio_url = _audio_url_for(v)
    if not audio_url:
        raise ValueError(f"video {video_id}: failed to build audio URL")

    payload: dict[str, Any] = {
        "audio_url": audio_url,
        "speech_models": ["universal-3-pro", "universal-2"],
        "speaker_labels": True,
        "language_code": "en",
        "punctuate": True,
        "format_text": True,
        "webhook_url": _webhook_url(),
    }

    secret = _webhook_secret()
    if secret:
        payload["webhook_auth_header_name"] = "X-MDS-Webhook-Secret"
        payload["webhook_auth_header_value"] = secret

    headers = {
        "Authorization": _api_key(),  # raw key, NO Bearer prefix.
        "Content-Type": "application/json",
    }

    r = requests.post(f"{ASSEMBLYAI_BASE}/transcript",
                      headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    body = r.json()

    transcript_id = body.get("id")
    if not transcript_id:
        raise RuntimeError(f"AssemblyAI submit returned no id: {body!r}")

    _update_video(video_id, {
        "assemblyai_transcript_id": transcript_id,
        "transcription_status": "processing",
        "language": "en",
        "updated_at": _now_iso(),
    })

    log.info("transcripts: submitted video=%s transcript_id=%s", video_id, transcript_id)
    return body


def fetch_transcript(transcript_id: str) -> dict:
    """Fetch the full transcript from AssemblyAI by id."""
    headers = {"Authorization": _api_key()}
    r = requests.get(f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}",
                     headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


# ============================================================================
# Chunking — utterances → ~30s windows with 5s overlap
# ============================================================================
DEFAULT_WINDOW_MS = 30_000
DEFAULT_OVERLAP_MS = 5_000


def chunk_utterances(utterances: list[dict],
                     window_ms: int = DEFAULT_WINDOW_MS,
                     overlap_ms: int = DEFAULT_OVERLAP_MS) -> list[dict]:
    """Convert AssemblyAI's utterance list into our segment format.

    AssemblyAI's `utterances` array (when speaker_labels=true) gives one
    entry per speaker turn:
        {speaker, text, start, end, words: [{text, start, end, ...}, ...]}
    where start/end are in milliseconds.

    We want ~30s segments with 5s overlap. Within a single utterance,
    we slice based on word timestamps. Across utterances, we don't merge
    (each segment carries one speaker_label so search results can show
    "X said ...").

    Returns a list of dicts ready to insert into transcript_segments
    (without organization_id / video_id which the caller adds).
    """
    segments: list[dict] = []

    for utt in utterances:
        words: list[dict] = utt.get("words") or []
        if not words:
            # Single-word utterance fallback
            segments.append({
                "speaker_label": utt.get("speaker"),
                "text": (utt.get("text") or "").strip(),
                "start_ms": int(utt.get("start") or 0),
                "end_ms": int(utt.get("end") or 0),
            })
            continue

        # Walk the words, snapshotting every time the window since the
        # current segment-start exceeds window_ms. Then step back by
        # overlap_ms for the next segment.
        i = 0
        while i < len(words):
            seg_start_ms = int(words[i]["start"])
            seg_end_target = seg_start_ms + window_ms
            j = i
            while j < len(words) and int(words[j]["end"]) < seg_end_target:
                j += 1
            # j is one past the last word that fits.
            seg_words = words[i:j] if j > i else words[i:i+1]
            seg_text = " ".join((w.get("text") or "") for w in seg_words).strip()
            seg_end_ms = int(seg_words[-1]["end"])
            segments.append({
                "speaker_label": utt.get("speaker"),
                "text": seg_text,
                "start_ms": seg_start_ms,
                "end_ms": seg_end_ms,
            })

            if j >= len(words):
                break

            # Step back by overlap_ms to begin the next segment.
            target_next_start = seg_end_ms - overlap_ms
            k = j
            while k > i and int(words[k - 1]["start"]) > target_next_start:
                k -= 1
            i = max(k, i + 1)  # ensure forward progress

    # Drop empty segments defensively
    return [s for s in segments if s.get("text")]


# ============================================================================
# Webhook handler
# ============================================================================
def handle_webhook(payload: dict, secret_header_value: str) -> tuple[dict, int]:
    """Process an AssemblyAI completion webhook. Verifies the shared secret,
    fetches the full transcript, persists segments, and triggers chapters.

    Returns (json_body, status_code) so the Flask route can pass through.

    Idempotency: if the matching video already has segments, we no-op
    rather than duplicating. AssemblyAI retries up to 10 times per the
    docs; we want each retry to be safe.
    """
    expected = _webhook_secret()
    if expected and secret_header_value != expected:
        return {"error": "Invalid secret"}, 401

    transcript_id = payload.get("transcript_id")
    status = payload.get("status")
    if not transcript_id:
        return {"error": "Missing transcript_id"}, 400

    # Find the matching video.
    matches = _supabase_get(
        "videos",
        params={
            "assemblyai_transcript_id": f"eq.{transcript_id}",
            "select": "id,organization_id,title,transcription_status",
            "limit": "1",
        },
    )
    if not matches:
        log.warning("transcripts: webhook for unknown transcript_id=%s", transcript_id)
        return {"ok": True, "note": "unknown transcript_id"}, 200

    video = matches[0]
    video_id = video["id"]
    org_id = video["organization_id"]

    # Status check.
    if status == "error":
        full = fetch_transcript(transcript_id)
        err = full.get("error") or "AssemblyAI returned error"
        log.error("transcripts: error for video=%s err=%s", video_id, err)
        _update_video(video_id, {
            "transcription_status": "failed",
            "updated_at": _now_iso(),
        })
        return {"ok": True}, 200

    # Idempotency: AssemblyAI may re-deliver the same webhook (their docs
    # say up to 10 retries on non-2xx responses). True duplicates surface as
    # status='ready' for THIS transcript with segments already in place. In
    # that case, no-op.
    #
    # If status is 'processing' (we just submitted), even if segments exist,
    # they're from a *previous* transcription run — _process_completed_transcript
    # will delete them and insert fresh ones.
    if video.get("transcription_status") == "ready":
        existing = _supabase_get(
            "transcript_segments",
            params={"video_id": f"eq.{video_id}", "select": "id", "limit": "1"},
        )
        if existing:
            log.info(
                "transcripts: duplicate webhook for already-ready video=%s — skipping",
                video_id,
            )
            return {"ok": True, "note": "already processed"}, 200

    # Process async so we return 2xx in <10s. AssemblyAI's docs are strict
    # about that or they retry up to 10 times.
    threading.Thread(
        target=_process_completed_transcript,
        args=(video_id, org_id, transcript_id, video.get("title") or ""),
        daemon=True,
    ).start()
    return {"ok": True, "queued": True}, 200


def _process_completed_transcript(video_id: str, org_id: str,
                                   transcript_id: str, video_title: str) -> None:
    """Background worker — fetches transcript, chunks, inserts, runs chapters.
    Errors here only get logged; webhook already responded 2xx."""
    try:
        full = fetch_transcript(transcript_id)
        utterances = full.get("utterances") or []
        if not utterances:
            log.warning("transcripts: no utterances for video=%s", video_id)
            _update_video(video_id, {
                "transcription_status": "failed",
                "updated_at": _now_iso(),
            })
            return

        segments = chunk_utterances(utterances)
        rows = [
            {
                "organization_id": org_id,
                "video_id": video_id,
                "speaker_label": s["speaker_label"],
                "text": s["text"],
                "start_ms": s["start_ms"],
                "end_ms": s["end_ms"],
            }
            for s in segments
        ]
        # Re-transcription support: clear any stale segments from a prior
        # run before inserting the new ones. First-time transcriptions
        # have nothing to delete; PostgREST's DELETE on an empty match is
        # a no-op.
        _delete_transcript_segments(video_id)
        _insert_transcript_segments(rows)
        log.info("transcripts: inserted %d segments for video=%s",
                 len(rows), video_id)

        # Update Mux duration on the video too if AssemblyAI returned it
        # and we don't have one yet. (Useful for the iOS card label.)
        duration_sec = full.get("audio_duration")
        patch = {
            "transcription_status": "ready",
            "updated_at": _now_iso(),
        }
        if duration_sec:
            patch["duration_sec"] = int(duration_sec)
        _update_video(video_id, patch)

        # Now produce chapters via LLM Gateway. Failures here don't fail
        # the transcript itself — chapters are nice-to-have.
        try:
            import chapters
            chapters.generate_and_apply_chapters(
                video_id=video_id,
                video_title=video_title,
                transcript_text=full.get("text") or "",
                utterances=utterances,
            )
        except Exception:
            log.exception("transcripts: chapters generation failed for video=%s", video_id)

    except Exception:
        log.exception("transcripts: processing failed for video=%s", video_id)
        try:
            _update_video(video_id, {
                "transcription_status": "failed",
                "updated_at": _now_iso(),
            })
        except Exception:
            pass
