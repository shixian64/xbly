"""Legacy Tencent TIM REST implementation of the message transport edge."""

from __future__ import annotations

from bbw_protocol.adapters.tim_rest import TimRestClient

from .contracts import MessageTransport


def create_legacy_tim_rest_transport() -> MessageTransport:
    """Create the existing server-side TIM client behind a neutral contract."""

    return TimRestClient()
