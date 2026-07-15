"""beibeiwu / 小贝乐园 protocol client.

Reimplements the Android app HTTP surface so most features can be used
without the APK UI (CTF / reverse-engineering toolkit).

Native capability adapters (IM / face / pay):
    from bbw_protocol.adapters import NativeBundle
"""

from .app import BeibeiwuApp
from .client import ProtocolClient
from .session import Session

__all__ = ["BeibeiwuApp", "ProtocolClient", "Session"]
__version__ = "0.2.0"
