"""mux_signer.py — Mux signed playback URL generation (Q16 hardening).

Background: Mux assets can have multiple playback IDs, each with its own
policy (public or signed). After Q16, new uploads request BOTH policies —
the public ID is used for image.mux.com thumbnail URLs (cheap, fine),
the signed ID for HLS streaming + audio.m4a transcription input
(JWT-signed, prevents leaked-URL access). Existing videos get backfilled
with a signed playback ID via the Mux API.

This module signs the JWT. The private RSA key + key ID come from env
vars Andy sets in Render:

    MUX_SIGNING_KEY_ID  — short alphanumeric key ID issued by Mux
    MUX_SIGNING_KEY     — RSA private key in PEM format

When env vars are missing, the helpers gracefully return the unsigned URL
so the platform keeps working in pre-Q16 mode (e.g. fresh dev clone before
keys are wired). The first call without keys logs a one-shot warning so
the operator knows why streams might 401.

Mux JWT shape:
    header  : { "alg": "RS256", "typ": "JWT", "kid": <signing key id> }
    payload : { "sub": <playback id>, "aud": "v" | "t" | "s" | "g",
                "exp": <unix seconds> }

For both the .m3u8 manifest and the audio.m4a rendition the audience is
"v" — audio.m4a lives under the video asset and Mux treats it as a
video sub-asset, not a separate audience.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import jwt  # PyJWT — already in requirements.txt (pyjwt[crypto]>=2.8.0)

log = logging.getLogger(__name__)

# Mux JWT audience claims
AUD_VIDEO = "v"
AUD_THUMBNAIL = "t"
AUD_STORYBOARD = "s"
AUD_GIF = "g"

# Default TTLs — long enough that a typical viewing session won't expire
# mid-watch, short enough that a leaked URL doesn't just replace the
# unsigned-leak risk we're fixing.
DEFAULT_VIDEO_TTL_SEC = 24 * 3600   # 24 hours — covers a long pause/resume
DEFAULT_AUDIO_TTL_SEC = 30 * 60     # 30 minutes — AAI fetches once at job start

_MISSING_KEY_WARNED = False


def _get_signing_key() -> Optional[tuple[str, str]]:
    """Returns (key_id, pem) if both env vars are set; None if either is
    missing. Logs once on first miss so we don't spam logs."""
    global _MISSING_KEY_WARNED
    key_id = os.getenv("MUX_SIGNING_KEY_ID", "").strip()
    key_pem = os.getenv("MUX_SIGNING_KEY", "").strip()
    if not key_id or not key_pem:
        if not _MISSING_KEY_WARNED:
            log.warning(
                "mux_signer: MUX_SIGNING_KEY_ID or MUX_SIGNING_KEY not set — "
                "falling back to unsigned URLs. Set both in Render env (and "
                "Manual Deploy) to complete Q16 (signed-URL hardening)."
            )
            _MISSING_KEY_WARNED = True
        return None
    return key_id, key_pem


def _sign_token(playback_id: str, audience: str, ttl_sec: int) -> Optional[str]:
    """Build + sign a JWT for the given playback ID + audience. Returns
    None if the signing key isn't configured (caller falls back to bare
    URL)."""
    keypair = _get_signing_key()
    if keypair is None:
        return None
    key_id, key_pem = keypair
    now = int(time.time())
    payload = {
        "sub": playback_id,
        "aud": audience,
        "exp": now + ttl_sec,
    }
    headers = {"kid": key_id}
    try:
        return jwt.encode(payload, key_pem, algorithm="RS256", headers=headers)
    except Exception as e:
        log.error("mux_signer: failed to sign token for %s: %s", playback_id, e)
        return None


def sign_video_url(
    playback_id: Optional[str],
    ttl_sec: int = DEFAULT_VIDEO_TTL_SEC,
) -> Optional[str]:
    """Returns a signed HLS URL for the given Mux playback ID. Returns
    None if playback_id is missing. Returns the unsigned bare URL if the
    signing key isn't configured (pre-Q16 fallback — Mux will 401 if this
    is a signed-only ID, which is the warning the operator gets).
    """
    if not playback_id:
        return None
    token = _sign_token(playback_id, AUD_VIDEO, ttl_sec)
    base = f"https://stream.mux.com/{playback_id}.m3u8"
    if token is None:
        return base
    return f"{base}?token={token}"


def sign_audio_url(
    playback_id: Optional[str],
    ttl_sec: int = DEFAULT_AUDIO_TTL_SEC,
) -> Optional[str]:
    """Returns a signed audio.m4a URL — used for AAI transcription submit.
    Audience is "v" (audio.m4a is part of the video asset). Short TTL
    default (30 min) — AAI fetches once at job start, then we don't care
    about the URL anymore."""
    if not playback_id:
        return None
    token = _sign_token(playback_id, AUD_VIDEO, ttl_sec)
    base = f"https://stream.mux.com/{playback_id}/audio.m4a"
    if token is None:
        return base
    return f"{base}?token={token}"
