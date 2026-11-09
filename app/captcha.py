"""Cloudflare Turnstile captcha on the sign-up form.

On when RRC_TURNSTILE_SITE_KEY (public, shown to the browser) and RRC_TURNSTILE_SECRET_KEY (server only)
are both set. The widget gives the browser a single-use token, valid 5 minutes, that the server checks
with Cloudflare's Siteverify API.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN = 2048

log = logging.getLogger("uvicorn.error")


def _keys() -> tuple[str, str]:
    return (os.environ.get("RRC_TURNSTILE_SITE_KEY", "").strip(),
            os.environ.get("RRC_TURNSTILE_SECRET_KEY", "").strip())


def site_key() -> str | None:
    """Public key for the browser widget, or None when the captcha is off."""
    site, secret = _keys()
    return site if site and secret else None


def verify(token: str, remote_ip: str | None = None) -> bool:
    """True if Cloudflare confirms the token. Always True when the captcha is off."""
    site, secret = _keys()
    if not (site and secret):
        return True
    if not token or len(token) > MAX_TOKEN:
        return False
    fields = {"secret": secret, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    req = urllib.request.Request(SITEVERIFY_URL, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.error("Turnstile check failed: %s", exc)
        return False
    if not result.get("success"):
        log.info("Turnstile rejected a token: %s", result.get("error-codes"))
    return bool(result.get("success"))
