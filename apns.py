"""
Minimal APNs (Apple Push Notification service) client for the MDS Knowledge Base
iOS app.

Token-based auth (JWT signed with the .p8 key from developer.apple.com → Keys).
HTTP/2 to api.push.apple.com (or api.sandbox.push.apple.com for development).

Why this module exists rather than `apns2`/`pyapns_client`/etc.:
    - apns2 depends on the unmaintained `hyper` package. Dead.
    - pyapns_client is fine but adds a dependency for code we can write in
      ~120 lines. This whole flow is tiny: sign a JWT, POST to Apple, read
      the status. No state, no streaming.

Usage:
    from apns import APNsClient, APNsError

    client = APNsClient.from_env()
    client.send(
        device_token="abcd1234…",
        payload={"aps": {"alert": {"title": "…", "body": "…"}}},
    )

Required Render env vars:
    APNS_AUTH_KEY      Multi-line .p8 contents (the whole `-----BEGIN PRIVATE KEY-----` block)
    APNS_KEY_ID        10-char alphanumeric Key ID (from the dev portal screen)
    APNS_TEAM_ID       10-char Team ID
    APNS_BUNDLE_ID     com.mds.knowledgebase
    APNS_USE_SANDBOX   "1" / "true" to use api.sandbox.push.apple.com (default: production)
"""
from __future__ import annotations

import os
import time
import json
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt  # PyJWT


class APNsError(Exception):
    """Apple returned a non-200 status. Carries the Apple reason string."""

    def __init__(self, status: int, reason: str = "", token: str = ""):
        super().__init__(f"APNs {status} {reason} (token={token[:8]}…)")
        self.status = status
        self.reason = reason
        self.token = token


@dataclass
class APNsConfig:
    auth_key_pem: str   # contents of the .p8 (PEM)
    key_id: str         # 10-char Key ID
    team_id: str        # 10-char Team ID
    bundle_id: str      # com.mds.knowledgebase
    use_sandbox: bool   # True → api.sandbox.push.apple.com

    @property
    def host(self) -> str:
        return ("api.sandbox.push.apple.com" if self.use_sandbox
                else "api.push.apple.com")


class APNsClient:
    """Singleton-ish client. JWT is cached for 50 minutes (Apple allows 60)."""

    _JWT_TTL_S = 50 * 60

    def __init__(self, config: APNsConfig):
        self.config = config
        self._jwt: Optional[str] = None
        self._jwt_issued_at: float = 0.0
        # HTTP/2 client. http2=True requires the `h2` package (pulled in by httpx[http2]).
        self._client = httpx.Client(http2=True, timeout=10.0)

    @classmethod
    def from_env(cls) -> "APNsClient":
        auth_key = os.getenv("APNS_AUTH_KEY") or ""
        key_id = (os.getenv("APNS_KEY_ID") or "").strip()
        team_id = (os.getenv("APNS_TEAM_ID") or "").strip()
        bundle_id = (os.getenv("APNS_BUNDLE_ID") or "com.mds.knowledgebase").strip()
        use_sandbox = (os.getenv("APNS_USE_SANDBOX", "") or "").lower() in ("1", "true", "yes")
        if not auth_key or not key_id or not team_id:
            raise APNsError(500, "APNs env vars not configured")
        return cls(APNsConfig(
            auth_key_pem=auth_key,
            key_id=key_id,
            team_id=team_id,
            bundle_id=bundle_id,
            use_sandbox=use_sandbox,
        ))

    def _provider_token(self) -> str:
        """Refresh the JWT every ~50 min."""
        now = time.time()
        if self._jwt and (now - self._jwt_issued_at) < self._JWT_TTL_S:
            return self._jwt
        token = jwt.encode(
            payload={"iss": self.config.team_id, "iat": int(now)},
            key=self.config.auth_key_pem,
            algorithm="ES256",
            headers={"alg": "ES256", "kid": self.config.key_id},
        )
        # PyJWT returns a str on 2.x. Make sure.
        if isinstance(token, bytes):
            token = token.decode("ascii")
        self._jwt = token
        self._jwt_issued_at = now
        return token

    def send(
        self,
        device_token: str,
        payload: dict,
        *,
        push_type: str = "alert",
        priority: int = 10,
        topic: Optional[str] = None,
        collapse_id: Optional[str] = None,
        expiration: int = 0,
    ) -> dict:
        """Send a single push. Raises APNsError on non-2xx.

        Returns:
            {"apns_id": "<uuid>"} on success.

        Args:
            device_token: hex-encoded token from didRegisterForRemoteNotifications.
            payload: must contain a top-level "aps" object.
            push_type: "alert" | "background" | "liveactivity" | "voip" | …
            priority: 10 (immediate) or 5 (battery-conscious).
            topic: defaults to bundle_id; for Live Activity append ".push-type.liveactivity".
            collapse_id: dedupe key — multiple pushes with the same collapse_id
                show as a single notification on the device.
            expiration: unix ts after which Apple stops trying. 0 = once.
        """
        url = f"https://{self.config.host}/3/device/{device_token}"
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-push-type": push_type,
            "apns-topic": topic or self.config.bundle_id,
            "apns-priority": str(priority),
            "apns-expiration": str(expiration),
            "content-type": "application/json",
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]

        body = json.dumps(payload).encode("utf-8")
        resp = self._client.post(url, headers=headers, content=body)

        if resp.status_code == 200:
            return {"apns_id": resp.headers.get("apns-id", "")}

        # Apple sends a JSON body like {"reason": "BadDeviceToken"} on errors.
        reason = ""
        try:
            data = resp.json()
            reason = data.get("reason", "")
        except Exception:
            reason = (resp.text or "")[:200]

        raise APNsError(resp.status_code, reason, device_token)

    def close(self) -> None:
        self._client.close()


# Module-level singleton (lazy). Reused across requests so the JWT cache works.
_singleton: Optional[APNsClient] = None


def get_apns_client() -> APNsClient:
    global _singleton
    if _singleton is None:
        _singleton = APNsClient.from_env()
    return _singleton


def reset_apns_client() -> None:
    """Useful for tests + when env vars rotate."""
    global _singleton
    if _singleton is not None:
        _singleton.close()
        _singleton = None
