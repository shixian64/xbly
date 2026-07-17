"""Optional Cloudflare Turnstile verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerifier:
    def __init__(self, settings: Any):
        self.site_key = str(getattr(settings, "turnstile_site_key", "") or "").strip()
        secret_file = str(getattr(settings, "turnstile_secret_key_file", "") or "").strip()
        self.secret = Path(secret_file).read_text(encoding="utf-8").strip() if secret_file else ""
        if bool(self.site_key) != bool(self.secret):
            raise RuntimeError(
                "Turnstile site key and secret key must either both be configured or both be empty"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.site_key and self.secret)

    def verify(self, token: str, *, remote_ip: str = "") -> bool:
        if not self.enabled:
            return True
        value = str(token or "").strip()
        if not value:
            return False
        payload = {"secret": self.secret, "response": value}
        if remote_ip and remote_ip != "unknown":
            payload["remoteip"] = remote_ip
        try:
            response = httpx.post(VERIFY_URL, data=payload, timeout=8.0)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return False
        return bool(result.get("success"))
