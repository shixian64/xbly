"""Small environment/Docker-secret loader for protocol credentials.

Protocol adapters are also imported by local reverse-engineering tools and
unit tests, so development has explicit, non-production placeholder values.
Every other environment is fail-closed: deployed processes must provide the
credential directly or through the conventional ``*_FILE`` variable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class ProtocolSecretError(RuntimeError):
    """Raised when a protocol credential is missing or misconfigured."""


_DEVELOPMENT_ENVIRONMENTS = {"dev", "development", "local", "test", "testing"}


def _development_defaults_allowed() -> bool:
    raw = os.getenv("BBW_ENV")
    if raw is None:
        # Preserve command-line and unit-test usability.  Docker deployment
        # explicitly sets BBW_ENV=production and therefore never takes this
        # branch.
        return True
    return raw.strip().lower() in _DEVELOPMENT_ENVIRONMENTS


def read_protocol_secret(
    name: str,
    *,
    development_default: Optional[str] = None,
) -> str:
    """Read ``name`` or ``name_FILE`` and reject ambiguous/empty settings.

    ``*_FILE`` follows the Docker secret convention: the file is UTF-8 text
    and trailing line endings are ignored.  Placeholder defaults are accepted
    only when ``BBW_ENV`` is absent or explicitly development/test/local.
    """

    file_name = f"{name}_FILE"
    direct_is_set = name in os.environ
    file_is_set = file_name in os.environ
    if direct_is_set and file_is_set:
        raise ProtocolSecretError(f"configure only one of {name} and {file_name}")

    if direct_is_set:
        if not _development_defaults_allowed():
            raise ProtocolSecretError(
                f"{name} is not allowed in production; use {file_name}"
            )
        value = os.environ[name].strip()
        if not value:
            raise ProtocolSecretError(f"{name} must not be empty")
        return value

    if file_is_set:
        path_value = os.environ[file_name].strip()
        if not path_value:
            raise ProtocolSecretError(f"{file_name} must not be empty")
        path = Path(path_value)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProtocolSecretError(f"cannot read {file_name}: {path}") from exc
        if not value:
            raise ProtocolSecretError(f"secret file configured by {file_name} is empty")
        return value

    if _development_defaults_allowed() and development_default:
        return development_default

    environment = os.getenv("BBW_ENV", "production") or "<empty>"
    raise ProtocolSecretError(
        f"missing {name} or {file_name} for BBW_ENV={environment!r}"
    )
