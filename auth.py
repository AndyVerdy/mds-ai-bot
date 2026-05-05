"""
Authentication for the MDS Knowledge Base iOS app.

Flow:
  1. POST /api/auth/request-code  body={"email": "..."}
       -> generates a 6-digit code, stores it in-memory with a 10-min TTL,
          sends it via email_sender.send_login_code()
  2. POST /api/auth/verify        body={"email": "...", "code": "..."}
       -> if code matches, issues an opaque random token, persists it to the
          AuthSessions Airtable table with a 30-day TTL, returns the token
  3. Subsequent calls send `Authorization: Bearer <token>`. Verified by
     looking up the token row in AuthSessions and checking `expires_at`.
  4. POST /api/auth/logout        (auth required)
       -> deletes the session row.

Codes live in memory. They're disposable and a 10-minute TTL means a server
restart at worst forces the user to request a fresh code. Tokens persist in
Airtable so they survive deploys.

A small in-process cache fronts the Airtable lookup so we don't pay the round
trip on every request.
"""

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

AIRTABLE_BASE_ID = "appT9TVZWhv7io4CN"
AUTH_SESSIONS_TABLE = "AuthSessions"
MEMBERS_TABLE = "Members"

# Source MDS member directory — canonical membership status lives here, not
# in the local Members table (which only tracks WA-side activity).
SOURCE_BASE_ID = "appou5JVr0WIrioWS"
SOURCE_MEMBERS_TABLE = "tblfwOSROSHfuYUxv"
SOURCE_STATUS_FIELD = "AT Database Status"
SOURCE_EMAIL_FIELD = "Preferred Email"

# Only emails whose source-base AT Database Status is one of these are
# allowed to sign in. Other states (Removed, Pending Application,
# Declined, etc.) are blocked.
ALLOWED_MEMBERSHIP_STATUSES = {
    "Current Member",
    "New Member",
    "Pending Group Entrance",
}

CODE_TTL_SECONDS = 10 * 60          # 10 minutes
TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days
CACHE_TTL_SECONDS = 60               # in-process token cache TTL
MEMBER_LOOKUP_CACHE_TTL = 120        # cache email->is-member for 2 min

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ----- In-memory code store -------------------------------------------------

@dataclass
class _Code:
    code: str
    expires_at: float


_code_store: dict[str, _Code] = {}
_code_lock = threading.Lock()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(_normalize_email(email)))


def generate_code() -> str:
    """Six-digit numeric code, leading zeros preserved."""
    return f"{secrets.randbelow(1_000_000):06d}"


def store_code(email: str, code: str, ttl: int = CODE_TTL_SECONDS) -> None:
    e = _normalize_email(email)
    with _code_lock:
        _code_store[e] = _Code(code=code, expires_at=time.time() + ttl)


def consume_code(email: str, code: str) -> bool:
    """Check the code; on match, delete it and return True."""
    e = _normalize_email(email)
    code = (code or "").strip()

    # Apple-review bypass: when the request is for the configured reviewer
    # account and the entered code matches the configured fixed code, accept
    # without touching the in-memory store. Lets Apple's reviewer log in
    # without us knowing their inbox.
    reviewer = (os.getenv("REVIEWER_EMAIL") or "").strip().lower()
    fixed = (os.getenv("REVIEWER_FIXED_CODE") or "").strip()
    if reviewer and fixed and e == reviewer and code == fixed:
        return True

    now = time.time()
    with _code_lock:
        entry = _code_store.get(e)
        if not entry:
            return False
        if entry.expires_at < now:
            _code_store.pop(e, None)
            return False
        if entry.code != code:
            return False
        _code_store.pop(e, None)  # one-shot use
        return True


# ----- Member allowlist (Airtable Members.email) ----------------------------

@dataclass
class _MemberCacheEntry:
    is_member: bool
    cached_until: float


_member_cache: dict[str, _MemberCacheEntry] = {}
_member_cache_lock = threading.Lock()


def _admin_emails() -> set[str]:
    """Comma-separated list of emails that bypass the Members allowlist.
    Set ADMIN_EMAILS on Render. Example: 'andy@mds.co,bob@example.com'."""
    raw = os.getenv("ADMIN_EMAILS", "") or ""
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def is_member_email(email: str) -> bool:
    """Check whether `email` is allowed to sign in.

    Allowed when ANY of:
      a. email == REVIEWER_EMAIL env var (Apple App Store review bypass).
      b. email is in the ADMIN_EMAILS env-var allowlist.
      c. email exists in the source MDS member directory base
         (appou5JVr0WIrioWS) AND the AT Database Status field is one of:
         Current Member, New Member, Pending Group Entrance.

    Other source-base statuses (Removed, Pending Application, Declined,
    Staff, Dead Lead, etc.) are blocked.

    Cached locally for 2 minutes per email to avoid hammering Airtable.
    Falls OPEN (returns True) on AT errors so transient failures don't lock
    everyone out.
    """
    e = _normalize_email(email)
    if not e:
        return False

    # Apple App Store reviewer bypass — needs to log in without us knowing
    # their actual email. The fixed-code path lives in consume_code().
    reviewer = (os.getenv("REVIEWER_EMAIL") or "").strip().lower()
    if reviewer and e == reviewer:
        return True

    if e in _admin_emails():
        return True

    # Cache hit?
    with _member_cache_lock:
        cached = _member_cache.get(e)
        if cached and cached.cached_until > time.time():
            return cached.is_member

    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        # If we can't reach Airtable, fall open — better to let users try
        # than lock everyone out on a config glitch.
        return True

    # Query the source MDS member directory by Preferred Email + return
    # the membership status field.
    formula = "LOWER({" + SOURCE_EMAIL_FIELD + "})='" + e.replace("'", r"\'") + "'"
    url = f"https://api.airtable.com/v0/{SOURCE_BASE_ID}/{SOURCE_MEMBERS_TABLE}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {pat}"},
            params={
                "filterByFormula": formula,
                "maxRecords": 1,
                "fields[]": [SOURCE_STATUS_FIELD],
            },
            timeout=15,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if not records:
            is_member = False
        else:
            status = records[0].get("fields", {}).get(SOURCE_STATUS_FIELD, "")
            is_member = status in ALLOWED_MEMBERSHIP_STATUSES
    except Exception:
        # Fall open on AT errors.
        is_member = True

    with _member_cache_lock:
        _member_cache[e] = _MemberCacheEntry(
            is_member=is_member,
            cached_until=time.time() + MEMBER_LOOKUP_CACHE_TTL,
        )
    return is_member


# ----- Token persistence (Airtable AuthSessions) ----------------------------

def _airtable_url() -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AUTH_SESSIONS_TABLE}"


def _airtable_headers() -> dict:
    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        raise RuntimeError("AIRTABLE_PAT not configured on the server")
    return {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}


def issue_token(email: str) -> dict:
    """Create a new session row and return the dict of token + email + expiry."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=TOKEN_TTL_SECONDS)
    payload = {
        "fields": {
            "token": token,
            "email": _normalize_email(email),
            "created_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "last_used_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
    }
    resp = requests.post(_airtable_url(), headers=_airtable_headers(),
                         json=payload, timeout=15)
    resp.raise_for_status()
    return {
        "token": token,
        "email": _normalize_email(email),
        "expires_at": expires.isoformat(),
    }


# ----- Token validation cache ----------------------------------------------

@dataclass
class _Cached:
    email: str
    expires_at: float        # absolute epoch seconds when this token expires
    cached_until: float      # absolute epoch seconds for cache freshness


_token_cache: dict[str, _Cached] = {}
_token_cache_lock = threading.Lock()


def _cache_get(token: str) -> _Cached | None:
    with _token_cache_lock:
        entry = _token_cache.get(token)
        if not entry:
            return None
        now = time.time()
        if entry.cached_until < now or entry.expires_at < now:
            _token_cache.pop(token, None)
            return None
        return entry


def _cache_put(token: str, email: str, expires_at_iso: str) -> None:
    try:
        # Airtable returns ISO with trailing Z.
        expires_at = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        expires_at = time.time() + TOKEN_TTL_SECONDS
    with _token_cache_lock:
        _token_cache[token] = _Cached(
            email=email,
            expires_at=expires_at,
            cached_until=time.time() + CACHE_TTL_SECONDS,
        )


def _cache_evict(token: str) -> None:
    with _token_cache_lock:
        _token_cache.pop(token, None)


def verify_token(token: str) -> str | None:
    """Return the email tied to this token, or None if missing/expired."""
    if not token:
        return None

    cached = _cache_get(token)
    if cached:
        return cached.email

    # Cache miss — query Airtable.
    try:
        formula = "{token}='" + token.replace("'", r"\'") + "'"
        resp = requests.get(
            _airtable_url(),
            headers={"Authorization": _airtable_headers()["Authorization"]},
            params={"filterByFormula": formula, "maxRecords": 1},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return None

    records = resp.json().get("records", [])
    if not records:
        return None
    f = records[0].get("fields", {})
    email = f.get("email")
    expires_at = f.get("expires_at")
    if not email or not expires_at:
        return None

    # Check expiry.
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except Exception:
        return None
    if exp_dt < datetime.now(timezone.utc):
        return None

    _cache_put(token, email, expires_at)
    return email


def revoke_token(token: str) -> bool:
    """Delete the session row matching the given token. Returns True on success."""
    if not token:
        return False
    _cache_evict(token)
    try:
        formula = "{token}='" + token.replace("'", r"\'") + "'"
        resp = requests.get(
            _airtable_url(),
            headers={"Authorization": _airtable_headers()["Authorization"]},
            params={"filterByFormula": formula, "maxRecords": 1},
            timeout=15,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if not records:
            return True  # already gone
        rec_id = records[0]["id"]
        delete_resp = requests.delete(
            f"{_airtable_url()}/{rec_id}",
            headers={"Authorization": _airtable_headers()["Authorization"]},
            timeout=15,
        )
        return delete_resp.ok
    except Exception:
        return False
