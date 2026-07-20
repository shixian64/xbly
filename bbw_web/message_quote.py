"""Bounded chat-message quotes shared by Web, TIM REST, and Android TUIKit."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping


QUOTE_NAMESPACE = "bbw_message"
QUOTE_VERSION = 1
NATIVE_QUOTE_KEY = "messageReply"

KIND_TO_NATIVE_MESSAGE_TYPE = {
    "text": 1,
    "custom": 2,
    "flash": 2,
    "image": 3,
    "audio": 4,
    "video": 5,
    "file": 6,
    "location": 7,
    "face": 8,
    "relay": 10,
}
NATIVE_MESSAGE_TYPE_TO_KIND = {
    value: key for key, value in KIND_TO_NATIVE_MESSAGE_TYPE.items() if key != "flash"
}


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


def _message_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    try:
        native_type = int(float(raw))
    except (TypeError, ValueError):
        native_type = 0
    if native_type:
        return NATIVE_MESSAGE_TYPE_TO_KIND.get(native_type, "custom")
    aliases = {
        "sound": "audio",
        "voice": "audio",
        "merger": "relay",
        "merge": "relay",
        "emoji": "face",
    }
    return aliases.get(raw, raw) or "text"


def _native_integer(value: Any) -> int:
    try:
        return max(0, int(float(str(value or "0").strip())))
    except (TypeError, ValueError):
        return 0


def _native_timestamp(value: Any) -> int:
    raw = str(value or "").strip()
    numeric = _native_integer(raw)
    if numeric:
        return numeric // 1000 if numeric >= 10_000_000_000 else numeric
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int(parsed.timestamp()))


def _native_abstract(quote: Mapping[str, str]) -> str:
    kind = quote.get("kind")
    if kind in {"image", "audio", "video", "location", "face"}:
        return ""
    text = str(quote.get("text") or "")
    if kind == "file" and text.startswith("[文件]"):
        return text[len("[文件]") :].lstrip()
    if kind == "relay" and text.startswith("[聊天记录]"):
        return text[len("[聊天记录]") :].lstrip()
    return text


def normalize_message_quote(value: Any) -> dict[str, str]:
    payload = _json_object(value)
    native_quote = payload.get(NATIVE_QUOTE_KEY)
    namespace = payload.get(QUOTE_NAMESPACE)
    if isinstance(native_quote, Mapping):
        if _bounded(native_quote.get("messageRootID"), 512):
            return {}
        if _native_integer(native_quote.get("version")) > QUOTE_VERSION:
            return {}
        payload = _json_object(native_quote)
    elif isinstance(namespace, Mapping):
        payload = _json_object(namespace.get("quote"))
    elif isinstance(payload.get("quote"), Mapping):
        payload = _json_object(payload.get("quote"))
    if not payload:
        return {}

    quote = {
        "message_id": _bounded(
            payload.get("message_id")
            or payload.get("messageId")
            or payload.get("messageID")
            or payload.get("id"),
            512,
        ),
        "message_random": _bounded(
            payload.get("message_random")
            or payload.get("messageRandom")
            or payload.get("msg_random"),
            80,
        ),
        "message_sequence": _bounded(
            payload.get("message_sequence")
            or payload.get("messageSequence")
            or payload.get("sequence")
            or payload.get("msg_sequence"),
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
            or payload.get("messageSender")
            or payload.get("author"),
            120,
        ),
        "text": _bounded(
            payload.get("text")
            or payload.get("preview")
            or payload.get("webAbstract")
            or payload.get("messageAbstract")
            or payload.get("content"),
            500,
        ),
        "kind": _message_kind(
            payload.get("kind")
            or payload.get("message_type")
            or payload.get("messageType")
            or payload.get("type"),
        )[:40],
        "sent_at": _bounded(
            payload.get("sent_at")
            or payload.get("sentAt")
            or payload.get("messageTime")
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
    native_quote = {
        "messageAbstract": _native_abstract(normalized),
        "messageID": normalized["message_id"],
        "messageSender": normalized["sender_name"] or normalized["sender_uid"],
        "messageSequence": _native_integer(normalized["message_sequence"]),
        "messageTime": _native_timestamp(normalized["sent_at"]),
        "messageType": KIND_TO_NATIVE_MESSAGE_TYPE.get(normalized["kind"], 2),
        "version": QUOTE_VERSION,
    }
    # TUIKit/Gson ignores unknown fields. Keep the minimum Web-only lookup data
    # beside the native fields so browser history can still jump across SDK and
    # archived message identifiers.
    if normalized["message_random"]:
        native_quote["messageRandom"] = normalized["message_random"]
    if normalized["sender_uid"]:
        native_quote["senderUid"] = normalized["sender_uid"]
    if normalized["kind"]:
        native_quote["kind"] = normalized["kind"]
    return json.dumps(
        {NATIVE_QUOTE_KEY: native_quote},
        ensure_ascii=False,
        separators=(",", ":"),
    )
