"""Periodic UpdateOnline0 for a single BeibeiwuApp (protocol core).

Web multi-user layer starts/stops one Heartbeat per web session in bbw_web.
"""

from __future__ import annotations

import random
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

if TYPE_CHECKING:
    from .app import BeibeiwuApp


class Heartbeat:
    def __init__(
        self,
        app: "BeibeiwuApp",
        *,
        interval_sec: float = 55.0,
        jitter_sec: float = 8.0,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.app = app
        self.interval_sec = interval_sec
        self.jitter_sec = jitter_sec
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._first = True
        self.last_ok: Optional[bool] = None
        self.last_at: float = 0.0
        self.ticks: int = 0
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def once(
        self,
        first: Optional[bool] = None,
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> Dict[str, Any]:
        use_first = self._first if first is None else first
        try:
            call_options: Dict[str, Any] = {}
            if timeout is not None:
                call_options["timeout"] = timeout
            if deadline is not None:
                call_options["deadline"] = deadline
            r = self.app.misc.update_online(first=use_first, **call_options)
            self._first = False
            self.last_ok = bool(r.ok)
            self.last_at = time.time()
            self.ticks += 1
            self.last_error = "" if r.ok else (r.message or r.code or (r.raw or "")[:120])
            out = {
                "ok": r.ok,
                "code": r.code,
                "message": r.message,
                "ticks": self.ticks,
                "at": self.last_at,
            }
            if self.on_tick:
                try:
                    self.on_tick(out)
                except Exception:
                    pass
            return out
        except Exception as e:
            self.last_ok = False
            self.last_error = str(e)
            self.last_at = time.time()
            return {"ok": False, "error": str(e), "ticks": self.ticks, "at": self.last_at}

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()

        def loop() -> None:
            if self._stop.wait(1.5):
                return
            while not self._stop.is_set():
                self.once()
                delay = self.interval_sec
                if self.jitter_sec > 0:
                    delay += random.uniform(-self.jitter_sec, self.jitter_sec)
                delay = max(15.0, delay)
                if self._stop.wait(delay):
                    break

        self._thread = threading.Thread(
            target=loop, name=f"bbw-hb-{self.app.session.uid}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "interval_sec": self.interval_sec,
            "ticks": self.ticks,
            "last_ok": self.last_ok,
            "last_at": self.last_at,
            "last_error": self.last_error,
            "uid": getattr(self.app.session, "uid", ""),
        }
