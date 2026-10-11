"""Send email through the Brevo API (HTTPS, free plan of 300 emails a day).

HTTPS instead of SMTP because many free hosts block SMTP ports.
Without BREVO_API_KEY (local development) the message is printed to the server log instead of sent.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("mailer")
BREVO_URL = "https://api.brevo.com/v3/smtp/email"

# Tests replace this list to capture messages
outbox: list[dict] | None = None


def is_configured() -> bool:
    """Is sending email enabled? (or are tests capturing messages)"""
    if outbox is not None:
        return True
    return bool(os.environ.get("BREVO_API_KEY", "").strip() and os.environ.get("MAIL_FROM", "").strip())


def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Return True if it was actually sent."""
    message = {"to": to, "subject": subject, "html": html, "text": text}
    if outbox is not None:
        outbox.append(message)
        return True

    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip()
    if not (api_key and sender):
        # Not configured: print the link to the log so an admin can finish manually
        print(f"[البريد غير مُعدّ] إلى: {to} | {subject}\n{text}", flush=True)
        return False

    payload = {
        "sender": {"name": os.environ.get("MAIL_FROM_NAME", "مفتاح الخزينة"), "email": sender},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    request = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if isinstance(exc, urllib.error.HTTPError) else str(exc)
        log.error("فشل إرسال البريد إلى %s: %s", to, detail)
        print(f"[فشل إرسال البريد] إلى: {to} | {detail}", flush=True)
        return False
