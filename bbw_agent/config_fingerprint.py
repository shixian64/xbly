"""Secret-safe fingerprints for one immutable BYOK runtime snapshot."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def runner_configuration_fingerprint(
    runner_setting: Any,
    connection: Any,
) -> str:
    """Hash every field that can change autonomous model output or access.

    The encrypted credential is hashed as ciphertext and is never decrypted or
    copied into the fingerprint payload returned to callers.
    """

    encrypted_key = getattr(connection, "api_key_encrypted", None)
    encrypted_key_digest = hashlib.sha256(
        json.dumps(
            encrypted_key,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "connection": {
            "base_url": str(getattr(connection, "base_url", "") or ""),
            "enabled": bool(getattr(connection, "enabled", False)),
            "id": str(getattr(connection, "id", "") or ""),
            "key_digest": encrypted_key_digest,
            "last_test_status": str(
                getattr(connection, "last_test_status", "") or ""
            ),
            "model": str(getattr(connection, "model", "") or ""),
            "owner_user_id": str(
                getattr(connection, "owner_user_id", "") or ""
            ),
            "provider": str(getattr(connection, "provider", "") or ""),
        },
        "runner": {
            "active_connection_id": str(
                getattr(runner_setting, "active_connection_id", "") or ""
            ),
            "context_message_limit": int(
                getattr(runner_setting, "context_message_limit", 0) or 0
            ),
            "custom_instructions": str(
                getattr(runner_setting, "custom_instructions", "") or ""
            ),
            "max_output_tokens": int(
                getattr(runner_setting, "max_output_tokens", 0) or 0
            ),
            "owner_user_id": str(
                getattr(runner_setting, "owner_user_id", "") or ""
            ),
            "temperature_milli": int(
                getattr(runner_setting, "temperature_milli", 0) or 0
            ),
            "user_enabled": bool(
                getattr(runner_setting, "user_enabled", False)
            ),
            "version": int(getattr(runner_setting, "version", 0) or 0),
        },
        "schema": 1,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
