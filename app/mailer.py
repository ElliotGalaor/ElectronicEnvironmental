"""Sending e-mails (sign-up confirmation links).

Two back ends, chosen from the environment:
- Brevo transactional API (RRC_BREVO_API_KEY + RRC_MAIL_FROM): HTTPS only, so it works on hosts that
  block outgoing SMTP, such as Render's free plan. The sender address must be verified in Brevo.
- Server log (RRC_MAIL_LOG=1), for development: the message is written to the log instead of being sent.
Without either, no e-mail can be sent and sign-up confirmation is off.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SENDER_NAME = "RoHS / REACH Checker"

log = logging.getLogger("uvicorn.error")


class MailError(Exception):
    """The e-mail could not be sent."""


def _brevo_settings() -> tuple[str, str]:
    return os.environ.get("RRC_BREVO_API_KEY", "").strip(), os.environ.get("RRC_MAIL_FROM", "").strip()


def _log_mode() -> bool:
    return os.environ.get("RRC_MAIL_LOG", "").lower() in ("1", "true", "yes", "on")


def available() -> bool:
    key, sender = _brevo_settings()
    return bool(key and sender) or _log_mode()


def send(to: str, subject: str, html: str) -> None:
    key, sender = _brevo_settings()
    if key and sender:
        _send_brevo(key, sender, to, subject, html)
    elif _log_mode():
        log.info("E-mail to %s (RRC_MAIL_LOG, not sent)\nSubject: %s\n%s", to, subject, html)
    else:
        raise MailError("No e-mail service configured")


def _send_brevo(key: str, sender: str, to: str, subject: str, html: str) -> None:
    body = {"sender": {"name": SENDER_NAME, "email": sender}, "to": [{"email": to}],
            "subject": subject, "htmlContent": html}
    req = urllib.request.Request(
        BREVO_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"api-key": key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        log.error("Brevo refused the e-mail (HTTP %s): %s", exc.code, detail)
        raise MailError(f"Brevo HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        log.error("Brevo unreachable: %s", exc)
        raise MailError("Brevo unreachable") from exc
