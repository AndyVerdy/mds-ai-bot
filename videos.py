"""
MDS Video Platform — backend routes (M1).

These routes let the iOS Videos tab list and play videos. They're guarded by
the ENABLE_VIDEO_PLATFORM env var so the feature stays dormant until
explicitly turned on, even after this branch is merged to main.

M1 scope:
  GET /api/videos          → list ready, non-deleted, non-private videos for
                             the current user's org
  GET /api/videos/:id      → single video + HLS playback URL

Out of scope for M1 (lands later):
  - Access rules / visibility="rules" filtering   → M10
  - Private signed playback URLs                  → when private videos exist
  - Transcripts / chapters                        → M3
  - Speakers / categories                         → M6
  - View tracking POSTs                           → M5
  - Mux SDK calls (uploads, signed URLs)          → M2 / when needed

Data model: see CU doc 2531q-98637 page 03 (Data Model). Source of truth for
schema is the Postgres database itself, accessed here via Supabase service
role (bypasses RLS — backend always filters by organization_id explicitly).

Auth: routes use the existing OTP-based `require_auth` decorator from web.py
and pass it in via `register_video_routes`. `request.user_email` is set; we
look up the matching `users.id` + `organization_members.org_id` to scope the
query.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests
from flask import jsonify, request

from mux_signer import sign_video_url


# ============================================================================
# Feature flag
# ============================================================================
def is_enabled() -> bool:
    """Routes only register when this is true. Default: dormant."""
    return os.getenv("ENABLE_VIDEO_PLATFORM", "").strip().lower() in ("1", "true", "yes")


# ============================================================================
# Supabase client (lightweight — uses REST + service-role key, no SDK dep)
# ============================================================================
# We use Supabase's PostgREST endpoint directly via `requests` to avoid pulling
# in the full `supabase-py` package for what amounts to two SELECT queries in
# M1. If we add inserts/upserts later (e.g. M2 admin upload, M9 AT sync), it's
# still simple to keep using REST; we only need the SDK if we want auth flows
# or storage uploads server-side.

_SUPABASE_URL_DEFAULT = "https://nadtudwuwjhckotrngzn.supabase.co"


def _supabase_base() -> str:
    return (os.getenv("SUPABASE_URL") or _SUPABASE_URL_DEFAULT).rstrip("/")


def _supabase_key() -> str:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY not configured. Set it in env "
            "(Render env vars) before enabling the video platform."
        )
    return key


def _supabase_get(path: str, params: Optional[dict] = None) -> list[dict]:
    """GET against Supabase PostgREST. Returns the JSON body (always a list
    for table queries). Raises requests.HTTPError on non-2xx."""
    url = f"{_supabase_base()}/rest/v1/{path.lstrip('/')}"
    key = _supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, params=params or {}, timeout=10)
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else [body]


def _supabase_upsert(
    path: str,
    rows: list[dict],
    on_conflict: Optional[str] = None,
) -> list[dict]:
    """POST + Prefer: resolution=merge-duplicates against PostgREST. Used by
    upsert_progress to write the (user_id, video_id) composite-key row.
    Returns the upserted rows (representation header).
    """
    url = f"{_supabase_base()}/rest/v1/{path.lstrip('/')}"
    key = _supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    params: dict = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    r = requests.post(url, headers=headers, json=rows, params=params, timeout=10)
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else [body]


# ============================================================================
# User → org resolution (cached briefly)
# ============================================================================
# `request.user_email` (set by the OTP auth decorator in web.py) → users.id +
# the org_id they belong to. M1 has exactly one org (MDS) so the org lookup is
# trivial, but doing it through the join keeps the code right for when we add
# more orgs.

_user_org_cache: dict[str, tuple[str, str, float]] = {}
_USER_ORG_TTL_S = 60.0


def _resolve_user_org(email: str) -> Optional[tuple[str, str]]:
    """Return (user_id, org_id) for the given email, or None if not found."""
    now = time.time()
    cached = _user_org_cache.get(email)
    if cached and (now - cached[2]) < _USER_ORG_TTL_S:
        return cached[0], cached[1]

    users = _supabase_get(
        "users",
        params={"email": f"eq.{email.lower()}", "select": "id", "limit": "1"},
    )
    if not users:
        return None
    user_id = users[0]["id"]

    members = _supabase_get(
        "organization_members",
        params={
            "user_id": f"eq.{user_id}",
            "select": "org_id",
            "limit": "1",
        },
    )
    if not members:
        return None
    org_id = members[0]["org_id"]

    _user_org_cache[email] = (user_id, org_id, now)
    return user_id, org_id


# ============================================================================
# Mux URL helpers
# ============================================================================
def _hls_url(playback_id: Optional[str]) -> Optional[str]:
    """Construct the unsigned HLS URL for a public-policy Mux playback ID.

    Used as the fallback when a video has only a public playback ID
    (legacy / pre-Q16 assets). New uploads after Q16 land with both a
    public and a signed playback ID; the signed one is preferred for
    streaming via _streaming_url() below.
    """
    if not playback_id:
        return None
    return f"https://stream.mux.com/{playback_id}.m3u8"


def _streaming_url(
    signed_playback_id: Optional[str],
    public_playback_id: Optional[str],
) -> Optional[str]:
    """Pick the right streaming URL for a video. Q16 hardening: prefer the
    signed playback ID (JWT-signed URL, prevents leaked-URL access). Fall
    back to the public ID's bare URL for legacy assets that haven't been
    backfilled yet, or when MUX_SIGNING_KEY is unset (dev / pre-Q16)."""
    if signed_playback_id:
        return sign_video_url(signed_playback_id)
    return _hls_url(public_playback_id)


def _thumbnail_url(playback_id: Optional[str], time_sec: int = 5) -> Optional[str]:
    """Mux generates thumbnails at any timestamp. We default to 5s in to skip
    a typical black intro frame. M5 will let admin override this."""
    if not playback_id:
        return None
    return f"https://image.mux.com/{playback_id}/thumbnail.jpg?time={time_sec}"


# ============================================================================
# Watch-progress helpers (M11 — resume-from-last-position + Continue Watching)
# ============================================================================
# `user_video_progress` is the per-user, per-video position table. iOS pings
# every ~10s during continuous playback + on pause / seek / background. The
# read-back path enriches both list + detail responses.

# Once a user has watched within this fraction of the video's duration we
# mark `watched_to_end=TRUE`. Continue Watching filters those out so the
# rail doesn't fill with finished videos. Mirrors YouTube's ~95% threshold.
_WATCHED_END_THRESHOLD = 0.95


def _fetch_progress_map(user_id: str, video_ids: list[str]) -> dict[str, dict]:
    """Bulk-fetch `(video_id, {last_position_sec, watched_to_end, ...})` for
    the videos in `video_ids`, scoped to `user_id`. Empty when the user
    hasn't watched anything yet — list endpoint then renders no progress
    bars, which is correct.
    """
    if not video_ids:
        return {}
    # PostgREST `in.()` filter with comma-separated list. Quote nothing
    # because all our IDs are UUIDs (no commas, no special chars).
    in_list = ",".join(video_ids)
    rows = _supabase_get(
        "user_video_progress",
        params={
            "user_id": f"eq.{user_id}",
            "video_id": f"in.({in_list})",
            "select": "video_id,last_position_sec,duration_sec,watched_to_end,updated_at",
        },
    )
    return {r["video_id"]: r for r in rows}


def _fetch_progress_one(user_id: str, video_id: str) -> Optional[dict]:
    """Single-video progress row for the detail endpoint. Returns None when
    the user hasn't watched this video yet — caller seeds with zero."""
    rows = _supabase_get(
        "user_video_progress",
        params={
            "user_id": f"eq.{user_id}",
            "video_id": f"eq.{video_id}",
            "select": "video_id,last_position_sec,duration_sec,watched_to_end,updated_at",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _upsert_progress(
    user_id: str,
    video_id: str,
    position_sec: int,
    duration_sec: Optional[int],
) -> dict:
    """Idempotent upsert keyed on (user_id, video_id). Computes
    `watched_to_end` from the position/duration ratio.

    `updated_at` is sent as an explicit ISO timestamp because PostgREST's
    merge-duplicates path runs an UPDATE for existing rows, and column
    defaults (DEFAULT NOW()) only fire on INSERT — without sending it,
    the original insert time would stick and Continue Watching would sort
    wrong. iOS doesn't tell us the moment; we use the server clock which
    is fine since this is "last seen activity" not "user wall-clock time".
    """
    from datetime import datetime, timezone
    pos = max(0, int(position_sec))
    dur = int(duration_sec) if duration_sec and duration_sec > 0 else None
    watched_to_end = bool(dur and pos >= int(dur * _WATCHED_END_THRESHOLD))
    rows = _supabase_upsert(
        "user_video_progress",
        rows=[{
            "user_id": user_id,
            "video_id": video_id,
            "last_position_sec": pos,
            "duration_sec": dur,
            "watched_to_end": watched_to_end,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }],
        on_conflict="user_id,video_id",
    )
    return rows[0] if rows else {}


# ============================================================================
# Serializers
# ============================================================================
def _serialize_list_row(v: dict, progress: Optional[dict] = None) -> dict:
    """Compact shape for the videos list endpoint — just what the iOS grid
    needs to render each card. Detail endpoint adds more.

    `progress` is the (optional) row from `user_video_progress` for the
    caller. When present, surfaces `last_position_sec` + `watched_to_end`
    so the card's progress bar + "Continue watching" filter render from
    real data (replaces the V1 hash-based stub on iOS — see
    `VideoStubMetadata` retirement on the iOS side).
    """
    pid = v.get("mux_playback_id")
    last_pos = int(progress["last_position_sec"]) if progress and progress.get("last_position_sec") is not None else 0
    watched = bool(progress.get("watched_to_end")) if progress else False
    return {
        "id": v["id"],
        "title": v["title"],
        "duration_sec": v.get("duration_sec"),
        "thumbnail_url": v.get("thumbnail_url") or _thumbnail_url(pid),
        "recorded_at": v.get("recorded_at"),
        "uploaded_at": v.get("uploaded_at"),
        "last_position_sec": last_pos,
        "watched_to_end": watched,
    }


def _serialize_detail(v: dict, progress: Optional[dict] = None) -> dict:
    public_pid = v.get("mux_playback_id")
    signed_pid = v.get("mux_signed_playback_id")
    last_pos = int(progress["last_position_sec"]) if progress and progress.get("last_position_sec") is not None else 0
    watched = bool(progress.get("watched_to_end")) if progress else False
    return {
        "id": v["id"],
        "title": v["title"],
        "description": v.get("description"),
        "duration_sec": v.get("duration_sec"),
        # Thumbnails: image.mux.com serves public-policy IDs without
        # signing — keep using the public_pid here.
        "thumbnail_url": v.get("thumbnail_url") or _thumbnail_url(public_pid),
        # Streaming: prefer the signed playback ID + JWT-signed URL when
        # available; fall back to the bare public URL for legacy assets.
        "playback_url": _streaming_url(signed_pid, public_pid),
        # Keep `playback_id` returning the public ID — iOS uses it to
        # construct image.mux.com storyboard / thumbnail URLs which are
        # serve-without-signing on public-policy IDs. Adding the signed
        # one as a sibling field for any future client-side use.
        "playback_id": public_pid,
        "signed_playback_id": signed_pid,
        "mux_status": v.get("mux_status"),
        "visibility": v.get("visibility"),
        "recorded_at": v.get("recorded_at"),
        "uploaded_at": v.get("uploaded_at"),
        # M11 watch-progress fields. `VideoPresentationState.loadCurrent`
        # reads `last_position_sec` and queues a seek before play, so the
        # player resumes at the saved position instead of the head.
        "last_position_sec": last_pos,
        "watched_to_end": watched,
    }


# ============================================================================
# Route registration
# ============================================================================
def register_video_routes(app, require_auth):
    """Register video routes on the given Flask app, wrapped with the
    existing require_auth decorator. Called from web.py only when
    ENABLE_VIDEO_PLATFORM is set, so the feature is fully dormant otherwise.
    """

    @app.route("/api/videos", methods=["GET"])
    @require_auth
    def list_videos():
        email = getattr(request, "user_email", None)
        if not email:
            # Belt-and-suspenders — require_auth should have set this.
            return jsonify({"error": "Authentication required"}), 401

        resolved = _resolve_user_org(email)
        if not resolved:
            # Authenticated user has no users/membership row yet. M1: one
            # org, every member should be in it. Bail early with empty list
            # rather than 500ing.
            return jsonify({"videos": []})
        _user_id, org_id = resolved

        # Filter: org-scoped, ready to play, not deleted, not private.
        # Order: most recent recorded_at first, falling back to uploaded_at.
        rows = _supabase_get(
            "videos",
            params={
                "organization_id": f"eq.{org_id}",
                "mux_status": "eq.ready",
                "visibility": "in.(public,unlisted)",
                "deleted_at": "is.null",
                "select": (
                    "id,title,duration_sec,thumbnail_url,mux_playback_id,"
                    "recorded_at,uploaded_at"
                ),
                "order": "recorded_at.desc.nullslast,uploaded_at.desc",
                "limit": "200",
            },
        )

        # Bulk-fetch the caller's progress for the videos in this page so
        # cards can render real progress bars + "Continue watching" filter
        # from real data. One round-trip regardless of page size.
        progress_map = _fetch_progress_map(_user_id, [r["id"] for r in rows])

        return jsonify({
            "videos": [_serialize_list_row(r, progress_map.get(r["id"])) for r in rows]
        })

    @app.route("/api/videos/<video_id>/transcript", methods=["GET"])
    @require_auth
    def get_video_transcript(video_id: str):
        email = getattr(request, "user_email", None)
        if not email:
            return jsonify({"error": "Authentication required"}), 401

        resolved = _resolve_user_org(email)
        if not resolved:
            return jsonify({"error": "Not found"}), 404
        _user_id, org_id = resolved

        # Confirm the video belongs to this org and is visible to the user.
        # Same access logic as get_video() — drop private, must be ready.
        vrows = _supabase_get(
            "videos",
            params={
                "id": f"eq.{video_id}",
                "organization_id": f"eq.{org_id}",
                "deleted_at": "is.null",
                "select": "id,visibility,mux_status,transcription_status",
                "limit": "1",
            },
        )
        if not vrows:
            return jsonify({"error": "Not found"}), 404
        vrow = vrows[0]
        if vrow.get("visibility") == "private":
            return jsonify({"error": "Not found"}), 404
        if vrow.get("transcription_status") != "ready":
            return jsonify({
                "error": "Transcript not ready",
                "transcription_status": vrow.get("transcription_status"),
            }), 409

        # Pull all segments for this video, ordered by time.
        segments = _supabase_get(
            "transcript_segments",
            params={
                "video_id": f"eq.{video_id}",
                "organization_id": f"eq.{org_id}",
                "select": "id,text,start_ms,end_ms,speaker_label,chapter_title",
                "order": "start_ms.asc",
                "limit": "10000",
            },
        )

        # Derive chapter list from segments (first occurrence wins).
        chapters: list[dict] = []
        seen: set[str] = set()
        for s in segments:
            ct = s.get("chapter_title")
            if ct and ct not in seen:
                chapters.append({"title": ct, "start_ms": s["start_ms"]})
                seen.add(ct)

        return jsonify({
            "video_id": video_id,
            "chapters": chapters,
            "segments": segments,
        })

    @app.route("/api/videos/<video_id>", methods=["GET"])
    @require_auth
    def get_video(video_id: str):
        email = getattr(request, "user_email", None)
        if not email:
            return jsonify({"error": "Authentication required"}), 401

        resolved = _resolve_user_org(email)
        if not resolved:
            return jsonify({"error": "Not found"}), 404
        _user_id, org_id = resolved

        rows = _supabase_get(
            "videos",
            params={
                "id": f"eq.{video_id}",
                "organization_id": f"eq.{org_id}",
                "deleted_at": "is.null",
                "select": (
                    "id,title,description,duration_sec,thumbnail_url,"
                    "mux_playback_id,mux_signed_playback_id,"
                    "mux_status,visibility,recorded_at,uploaded_at"
                ),
                "limit": "1",
            },
        )
        if not rows:
            return jsonify({"error": "Not found"}), 404

        v = rows[0]
        # M1: silently drop private — admin-only viewing comes with admin UI in M5.
        # M10 will replace this with the rules engine.
        if v.get("visibility") == "private":
            return jsonify({"error": "Not found"}), 404
        if v.get("mux_status") != "ready":
            return jsonify({"error": "Video not ready"}), 409

        progress = _fetch_progress_one(_user_id, video_id)
        return jsonify(_serialize_detail(v, progress))

    @app.route("/api/videos/<video_id>/progress", methods=["POST"])
    @require_auth
    def post_video_progress(video_id: str):
        """Record the caller's last-watched position for `video_id`.

        Body: `{position_sec: int, duration_sec?: int}`. iOS calls this on
        a periodic timer (~10s) plus on pause / seek / backgrounding. The
        upsert is keyed on (user_id, video_id) so repeated calls overwrite.

        Returns the full row shape so the client can refresh its in-memory
        model without an extra GET.
        """
        email = getattr(request, "user_email", None)
        if not email:
            return jsonify({"error": "Authentication required"}), 401

        resolved = _resolve_user_org(email)
        if not resolved:
            return jsonify({"error": "Not found"}), 404
        user_id, org_id = resolved

        # Confirm the video is in this org + readable. Same access logic as
        # get_video — protect against a forged video_id from leaking write
        # access to another org's row (the (user_id, video_id) PK on the
        # progress table doesn't enforce org membership).
        vrows = _supabase_get(
            "videos",
            params={
                "id": f"eq.{video_id}",
                "organization_id": f"eq.{org_id}",
                "deleted_at": "is.null",
                "select": "id,visibility,duration_sec",
                "limit": "1",
            },
        )
        if not vrows:
            return jsonify({"error": "Not found"}), 404
        vrow = vrows[0]
        if vrow.get("visibility") == "private":
            return jsonify({"error": "Not found"}), 404

        body = request.get_json(silent=True) or {}
        try:
            position_sec = int(body.get("position_sec", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "position_sec must be an integer"}), 400
        # iOS sends duration when it knows; fall back to the stored duration
        # so `watched_to_end` can still be computed for old clients.
        duration_sec = body.get("duration_sec")
        if duration_sec is None:
            duration_sec = vrow.get("duration_sec")
        try:
            duration_sec_int = int(duration_sec) if duration_sec is not None else None
        except (TypeError, ValueError):
            duration_sec_int = vrow.get("duration_sec")

        try:
            row = _upsert_progress(user_id, video_id, position_sec, duration_sec_int)
        except Exception as e:
            import traceback
            print(f"[post_video_progress] {type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

        return jsonify({
            "video_id": video_id,
            "last_position_sec": int(row.get("last_position_sec") or position_sec),
            "duration_sec": row.get("duration_sec"),
            "watched_to_end": bool(row.get("watched_to_end")),
            "updated_at": row.get("updated_at"),
        })
