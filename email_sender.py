"""
Email sending for the auth flow.

If RESEND_API_KEY is set in the environment, sends real emails via Resend.
Otherwise falls back to logging the message to stdout — useful for local
testing and as a safety net during initial deployment.

Resend signup: https://resend.com — free tier 3k/mo. Set:
  RESEND_API_KEY=re_xxx
  EMAIL_FROM="MDS Knowledge Base <onboarding@resend.dev>"   (or your own domain)
"""

import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

DEFAULT_FROM = "MDS Knowledge Base <onboarding@resend.dev>"


def _build_subject(code: str) -> str:
    return f"Your MDS Knowledge Base login code: {code}"


def _build_html(code: str, email: str) -> str:
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
      <h2 style="color: #09090b; margin-bottom: 8px;">MDS Knowledge Base</h2>
      <p style="color: #52525b; margin-top: 0;">Your login code:</p>
      <div style="font-size: 32px; font-weight: 700; letter-spacing: 6px; color: #09090b; padding: 16px 24px; background: #f4f4f5; border-radius: 8px; text-align: center; margin: 16px 0;">{code}</div>
      <p style="color: #71717a; font-size: 13px;">This code expires in 10 minutes. If you didn't request it, you can ignore this email.</p>
      <p style="color: #a1a1aa; font-size: 12px; margin-top: 24px;">Sent to {email}.</p>
    </div>
    """


def _build_text(code: str, email: str) -> str:
    return (
        f"Your MDS Knowledge Base login code is: {code}\n\n"
        f"This code expires in 10 minutes.\n"
        f"Sent to {email}."
    )


def send_login_code(email: str, code: str) -> bool:
    """Send the 6-digit login code to the user. Returns True on success.

    If no RESEND_API_KEY is configured, logs the code to stdout and still
    returns True — the system stays usable in dev / pre-Resend setup.
    """
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM", DEFAULT_FROM)

    if not api_key:
        # No key configured — log the code so it's still recoverable from
        # Render logs during initial setup, but signal failure to the caller
        # so the API surfaces a 502 instead of pretending the email went out.
        logger.error(
            "RESEND_API_KEY not set — login code NOT sent to %s",
            email,
        )
        print(
            f"[AUTH-DEV] Login code for {email}: {code} "
            "(NOT SENT — no RESEND_API_KEY)",
            flush=True,
        )
        return False

    payload = {
        "from": sender,
        "to": [email],
        "subject": _build_subject(code),
        "html": _build_html(code, email),
        "text": _build_text(code, email),
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mds-ai-bot/1.0 (+https://mds-ai-bot.onrender.com)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 200 and resp.status < 300:
                logger.info("Sent login code to %s via Resend", email)
                return True
            logger.error("Resend returned %s: %s", resp.status, body[:300])
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.error("Resend HTTPError %s: %s", e.code, body[:300])
        return False
    except Exception as e:  # pragma: no cover
        logger.error("Resend send failed: %s", e)
        return False
