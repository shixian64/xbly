"""Bounded chat-message quote snapshots shared by Web and TIM REST paths."""

from __future__ import annotations

import json
from typing import Any, Mapping


QUOTE_NAMESPACE = "bbw_message"
QUOTE_VERSION = 1


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raw = str(value or "").strip()
    if not raw or not raw.startswith(("{", "[")):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def normalize_message_quote(value: Any) -> dict[str, str]:
    payload = _json_object(value)
    namespace = payload.get(QUOTE_NAMESPACE)
    if isinstance(namespace, Mapping):
        payload = _json_object(namespace.get("quote"))
    elif isinstance(payload.get("quote"), Mapping):
        payload = _json_object(payload.get("quote"))
    if not payload:
        return {}

    quote = {
        "message_id": _bounded(
            payload.get("message_id")
            or payload.get("messageId")
            or payload.get("id"),
            512,
        ),
        "message_random": _bounded(
            payload.get("message_random")
            or payload.get("messageRandom")
            or payload.get("msg_random"),
            80,
        ),
        "sender_uid": _bounded(
            payload.get("sender_uid")
            or payload.get("senderUid")
            or payload.get("from"),
            128,
        ),
        "sender_name": _bounded(
            payload.get("sender_name")
            or payload.get("senderName")
            or payload.get("author"),
            120,
        ),
        "text": _bounded(
            payload.get("text")
            or payload.get("preview")
            or payload.get("content"),
            500,
        ),
        "kind": _bounded(
            payload.get("kind")
            or payload.get("message_type")
            or payload.get("type"),
            40,
        )
        or "text",
        "sent_at": _bounded(
            payload.get("sent_at")
            or payload.get("sentAt")
            or payload.get("timestamp")
            or payload.get("time"),
            80,
        ),
    }
    if not any(
        quote[key]
        for key in ("message_id", "message_random", "sender_uid", "text")
    ):
        return {}
    return quote


def extract_message_quote(cloud_custom_data: Any) -> dict[str, str]:
    return normalize_message_quote(cloud_custom_data)


def encode_message_quote(quote: Any) -> str:
    normalized = normalize_message_quote(quote)
    if not normalized:
        return ""
    return json.dumps(
        {
            QUOTE_NAMESPACE: {
                "version": QUOTE_VERSION,
                "quote": normalized,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
