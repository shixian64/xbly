"""Multi-user web session store — isolated from bbw_protocol core.

Each browser gets a web_sid cookie/token → one BeibeiwuApp instance + optional heartbeat.
Protocol sessions are saved under sessions/{uid}.json (gitignored).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bbw_protocol.app import BeibeiwuApp
from bbw_protocol.adapters import NativeBundle
from bbw_protocol.device import build_device_profile
from bbw_protocol.heartbeat import Heartbeat
from bbw_protocol.session import Session

# bbw_web owns multi-user paths; protocol core stays single-session oriented
REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "sessions"
WEB_META_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class WebUser:
    """One authenticated (or pending) browser session."""

    web_sid: str
    app: BeibeiwuApp
    native: NativeBundle
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    heartbeat: Optional[Heartbeat] = None
    label: str = ""  # optional display label

    def touch(self) -> None:
        self.last_seen = time.time()

    def start_heartbeat(self, interval_sec: float = 55.0) -> Dict[str, Any]:
        if self.heartbeat and self.heartbeat.running:
            return self.heartbeat.status()
        self.heartbeat = Heartbeat(self.app, interval_sec=interval_sec)
        self.heartbeat.start()
        return self.heartbeat.status()

    def stop_heartbeat(self) -> None:
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None

    def persist(self) -> Optional[Path]:
        """Save protocol session file keyed by uid (if logged in)."""
        s = self.app.session
        if not s.logged_in:
            return None
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = SESSIONS_DIR / f"{s.uid}.json"
        s.path = str(path)
        return s.save(str(path))

    def public(self) -> Dict[str, Any]:
        who = self.app.whoami()
        return {
            "web_sid": self.web_sid,
            "label": self.label,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "heartbeat": self.heartbeat.status() if self.heartbeat else {"running": False},
            "user": who,
        }


class SessionStore:
    """Thread-safe multi-tenant map: web_sid → WebUser."""

    def __init__(
        self,
        *,
        ttl_sec: float = 86400.0 * 7,
        auto_heartbeat: bool = True,
        heartbeat_interval: float = 55.0,
    ):
        self._lock = threading.RLock()
        self._users: Dict[str, WebUser] = {}
        self.ttl_sec = ttl_sec
        self.auto_heartbeat = auto_heartbeat
        self.heartbeat_interval = heartbeat_interval
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        WEB_META_DIR.mkdir(parents=True, exist_ok=True)

    def _new_sid(self) -> str:
        return secrets.token_urlsafe(24)

    def create(self, label: str = "") -> WebUser:
        with self._lock:
            self.purge_expired()
            sid = self._new_sid()
            sess = Session()
            # provisional device; re-seeded on login with phone
            sess.apply_device(build_device_profile(seed=sid[:12]))
            app = BeibeiwuApp(sess)
            user = WebUser(
                web_sid=sid,
                app=app,
                native=NativeBundle(app),
                label=label or "",
            )
            self._users[sid] = user
            return user

    def get(self, web_sid: Optional[str]) -> Optional[WebUser]:
        if not web_sid:
            return None
        with self._lock:
            u = self._users.get(web_sid)
            if not u:
                return None
            if time.time() - u.last_seen > self.ttl_sec:
                self._drop(web_sid)
                return None
            u.touch()
            return u

    def require(self, web_sid: Optional[str]) -> WebUser:
        u = self.get(web_sid)
        if not u:
            raise KeyError("invalid or expired web session")
        return u

    def drop(self, web_sid: str) -> bool:
        with self._lock:
            return self._drop(web_sid)

    def _drop(self, web_sid: str) -> bool:
        u = self._users.pop(web_sid, None)
        if not u:
            return False
        u.stop_heartbeat()
        try:
            u.persist()
        except Exception:
            pass
        return True

    def purge_expired(self) -> int:
        now = time.time()
        dead = [k for k, v in self._users.items() if now - v.last_seen > self.ttl_sec]
        for k in dead:
            self._drop(k)
        return len(dead)

    def list_public(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.purge_expired()
            return [u.public() for u in self._users.values()]

    def login_password(
        self,
        web_sid: Optional[str],
        phone: str,
        password: str,
        *,
        label: str = "",
        start_hb: Optional[bool] = None,
    ) -> WebUser:
        """Login into existing web session or create one."""
        with self._lock:
            user = self.get(web_sid) if web_sid else None
            if not user:
                user = self.create(label=label or phone)
            elif label:
                user.label = label
            # stable device profile per phone
            user.app.session.apply_device(build_device_profile(seed=phone))
            r = user.app.auth.login_password(phone, password)
            if not r.ok and not user.app.session.logged_in:
                # keep web session for retry
                raise RuntimeError(
                    r.message or r.code or r.raw[:200] or "login failed"
                )
            user.persist()
            do_hb = self.auto_heartbeat if start_hb is None else start_hb
            if do_hb and user.app.session.logged_in:
                user.start_heartbeat(self.heartbeat_interval)
            user.touch()
            return user

    def login_onekey(
        self,
        web_sid: Optional[str],
        phone: str,
        *,
        label: str = "",
        start_hb: Optional[bool] = None,
    ) -> WebUser:
        with self._lock:
            user = self.get(web_sid) if web_sid else None
            if not user:
                user = self.create(label=label or phone)
            user.app.session.apply_device(build_device_profile(seed=phone))
            r = user.app.auth.login_onekey(phone)
            if not r.ok and not user.app.session.logged_in:
                raise RuntimeError(
                    r.message or r.code or r.raw[:200] or "onekey login failed"
                )
            user.persist()
            do_hb = self.auto_heartbeat if start_hb is None else start_hb
            if do_hb and user.app.session.logged_in:
                user.start_heartbeat(self.heartbeat_interval)
            user.touch()
            return user

    def restore_from_disk(self, uid: str, web_sid: Optional[str] = None) -> Optional[WebUser]:
        """Attach a previously saved protocol session (uid.json) to a web session."""
        path = SESSIONS_DIR / f"{uid}.json"
        if not path.exists():
            return None
        with self._lock:
            sess = Session.load(str(path))
            if not sess.logged_in:
                return None
            user = self.get(web_sid) if web_sid else None
            if not user:
                user = self.create(label=sess.nickname or uid)
            user.app = BeibeiwuApp(sess)
            user.native = NativeBundle(user.app)
            if self.auto_heartbeat:
                user.start_heartbeat(self.heartbeat_interval)
            user.touch()
            return user

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_web_sessions": len(self._users),
                "sessions_dir": str(SESSIONS_DIR),
                "ttl_sec": self.ttl_sec,
                "auto_heartbeat": self.auto_heartbeat,
            }
