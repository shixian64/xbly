"""Canonical private account fields used only for the signed-in user's DTO."""

from __future__ import annotations

from typing import Mapping


_FALSE_VALUES = frozenset(
    {
        "",
        "0",
        "false",
        "no",
        "none",
        "null",
        "off",
        "否",
        "未实名",
        "未认证",
    }
)


def _first_value(
    account_data: Mapping[str, object],
    profile: Mapping[str, object],
    key: str,
) -> object | None:
    for source in (account_data, profile):
        value = source.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _text_value(
    account_data: Mapping[str, object],
    profile: Mapping[str, object],
    key: str,
    *,
    default: str,
) -> str:
    value = _first_value(account_data, profile, key)
    return str(value).strip() if value is not None else default


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value or "").strip().casefold()
    if normalized in _FALSE_VALUES:
        return False
    try:
        return int(normalized) != 0
    except ValueError:
        return bool(normalized)


def canonical_self_account_display(
    profile: Mapping[str, object] | None,
    account_data: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return private display fields for the authenticated account only.

    Runtime membership and verification values are stored on the external
    account.  Older imports may still carry them in ``User.profile``, so the
    latter remains a compatibility fallback.
    """

    safe_profile = dict(profile or {})
    safe_account_data = dict(account_data or {})
    account_verify_time = safe_account_data.get("rp_verify_time")
    profile_verify_time = safe_profile.get("rp_verify_time")
    verified_value = next(
        (
            value
            for value in (account_verify_time, profile_verify_time)
            if _enabled(value)
        ),
        None,
    )
    verify_time = (
        str(verified_value).strip()
        if verified_value is not None
        else _text_value(
            safe_account_data,
            safe_profile,
            "rp_verify_time",
            default="0",
        )
    )
    return {
        "is_realname": (
            _enabled(safe_account_data.get("is_realname"))
            or _enabled(safe_profile.get("is_realname"))
            or verified_value is not None
        ),
        "money": _text_value(
            safe_account_data, safe_profile, "money", default="0"
        ),
        "vip": _text_value(safe_account_data, safe_profile, "vip", default="0"),
        "svip": _text_value(
            safe_account_data, safe_profile, "svip", default="0"
        ),
        "user_role": _text_value(
            safe_account_data, safe_profile, "user_role", default=""
        ),
        "rp_verify_time": verify_time,
        "logged_in": True,
    }
