"""Multi-user web session store — isolated from ``bbw_protocol`` core.

Browser session ids are intentionally kept in HttpOnly cookies by the BFF.  Protocol
sessions stay in memory by default; opt-in persistence writes a reduced session file
which never contains the login password or ``raw_user`` response.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bbw_protocol.app import BeibeiwuApp
from bbw_protocol.adapters import NativeBundle
from bbw_protocol.device import build_device_profile
from bbw_protocol.heartbeat import Heartbeat
from bbw_protocol.session import Session

# bbw_web owns multi-user paths; protocol core stays single-session oriented
REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "sessions"
WEB_META_DIR = Path(__file__).resolve().parent / "data"


def _session_path(uid: Any) -> Optional[Path]:
    """Return a path confined to ``sessions/`` for a simple server uid."""
    value = str(uid or "").strip()
    if (
        not value
        or value == "0"
        or len(value) > 64
        or any(not (ch.isalnum() or ch in "_-") for ch in value)
    ):
        return None
    return SESSIONS_DIR / f"{value}.json"


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
    persist_sessions: bool = False
    pending_until: Optional[float] = field(default=None, repr=False)
    profile_cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]] = field(
        default_factory=dict,
        repr=False,
    )
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def touch(self) -> None:
        self.last_seen = time.time()

    def mark_pending(self, ttl_seconds: float) -> None:
        self.pending_until = time.time() + max(1.0, float(ttl_seconds))
        if self.persist_sessions:
            try:
                self.delete_persisted()
            except OSError:
                pass

    def clear_pending(self) -> None:
        self.pending_until = None

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
        """Persist a reduced protocol session, when explicitly enabled.

        ``Session.save`` serializes every dataclass field, including ``password`` and
        ``raw_user``.  Web persistence must not do that, so it owns a small sanitized
        writer here instead.
        """
        s = self.app.session
        if not self.persist_sessions or not s.logged_in:
            return None
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = _session_path(s.uid)
        if path is None:
            return None
        data = asdict(s)
        data.pop("path", None)
        data.pop("password", None)
        data.pop("raw_user", None)
        # Write atomically so two request threads cannot leave truncated JSON.
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        s.path = str(path)
        return path

    def delete_persisted(self, uid: Optional[str] = None) -> bool:
        """Delete this user's optional persisted session file."""
        path = _session_path(uid or self.app.session.uid)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def public(self) -> Dict[str, Any]:
        who = self.app.whoami()
        raw_phone = str(who.get("phone") or "")
        phone = raw_phone
        if len(phone) >= 7:
            phone = f"{phone[:3]}****{phone[-4:]}"
        label = str(self.label or "")
        if label == raw_phone or (label.isdigit() and len(label) >= 7):
            label = f"{label[:3]}****{label[-4:]}"
        return {
            "label": label,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "heartbeat": self.heartbeat.status() if self.heartbeat else {"running": False},
            "user": {
                "logged_in": bool(who.get("logged_in")),
                "uid": str(who.get("uid") or ""),
                "nickname": str(who.get("nickname") or ""),
                "avatar": str(who.get("portrait") or who.get("avatar") or ""),
                "portrait": str(who.get("portrait") or who.get("avatar") or ""),
                "user_role": str(who.get("user_role") or ""),
                "rp_verify_time": str(who.get("rp_verify_time") or "0"),
                "is_realname": bool(who.get("is_realname")),
                "vip": str(who.get("vip") or "0"),
                "svip": str(who.get("svip") or "0"),
                "money": str(who.get("money") or "0"),
                "phone": phone,
            },
        }


class SessionStore:
    """Thread-safe multi-tenant map: web_sid → WebUser."""

    def __init__(
        self,
        *,
        ttl_sec: float = 86400.0 * 7,
        auto_heartbeat: bool = True,
        heartbeat_interval: float = 55.0,
        persist_sessions: bool = False,
        allow_weak_onekey: bool = False,
        pending_expire_callback: Optional[Callable[[WebUser], None]] = None,
    ):
        self._lock = threading.RLock()
        self.users: Dict[str, WebUser] = {}
        self.ttl_sec = ttl_sec
        self.auto_heartbeat = auto_heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.persist_sessions = bool(persist_sessions)
        self.allow_weak_onekey = bool(allow_weak_onekey)
        self.pending_expire_callback = pending_expire_callback
        if self.persist_sessions:
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
                persist_sessions=self.persist_sessions,
            )
            self.users[sid] = user
            return user

    def rotate_sid(self, user: WebUser) -> WebUser:
        """Rotate the browser credential after authentication.

        The old map key is removed before the new key is published, preventing login
        fixation while preserving the already initialized app/session object.
        """
        with self._lock:
            old_sid = user.web_sid
            if self.users.get(old_sid) is user:
                self.users.pop(old_sid, None)
            sid = self._new_sid()
            while sid in self.users:
                sid = self._new_sid()
            user.web_sid = sid
            user.touch()
            self.users[sid] = user
            return user

    def get(self, web_sid: Optional[str]) -> Optional[WebUser]:
        if not web_sid:
            return None
        with self._lock:
            u = self.users.get(web_sid)
            if not u:
                return None
            now = time.time()
            if (
                now - u.last_seen > self.ttl_sec
                or (u.pending_until is not None and now >= u.pending_until)
            ):
                self._drop(web_sid)
                return None
            u.touch()
            return u

    def require(self, web_sid: Optional[str]) -> WebUser:
        u = self.get(web_sid)
        if not u:
            raise KeyError("invalid or expired web session")
        return u

    def drop(
        self,
        web_sid: str,
        *,
        delete_persisted: bool = False,
        persisted_uid: Optional[str] = None,
    ) -> bool:
        with self._lock:
            return self._drop(
                web_sid,
                delete_persisted=delete_persisted,
                persisted_uid=persisted_uid,
            )

    def _drop(
        self,
        web_sid: str,
        *,
        delete_persisted: bool = False,
        persisted_uid: Optional[str] = None,
    ) -> bool:
        u = self.users.pop(web_sid, None)
        if not u:
            return False
        was_pending = u.pending_until is not None
        u.stop_heartbeat()
        if delete_persisted:
            try:
                u.delete_persisted(persisted_uid)
            except OSError:
                pass
        if was_pending and self.pending_expire_callback is not None:
            threading.Thread(
                target=self.pending_expire_callback,
                args=(u,),
                name="bbw-pending-login-cleanup",
                daemon=True,
            ).start()
        return True

    def purge_expired(self) -> int:
        now = time.time()
        dead = [
            k
            for k, v in self.users.items()
            if now - v.last_seen > self.ttl_sec
            or (v.pending_until is not None and now >= v.pending_until)
        ]
        for k in dead:
            self._drop(k)
        return len(dead)

    def list_public(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.purge_expired()
            return [u.public() for u in self.users.values()]

    def close(self) -> None:
        """Stop all background work without serializing credentials on shutdown."""
        with self._lock:
            for sid in list(self.users):
                self._drop(sid)

    def login_password(
        self,
        web_sid: Optional[str],
        phone: str,
        password: str,
        *,
        label: str = "",
        start_hb: Optional[bool] = None,
    ) -> WebUser:
        """Log in, then rotate the browser SID before returning it."""
        user = self.get(web_sid) if web_sid else None
        created = user is None
        if not user:
            user = self.create(label=label or phone)
        elif label:
            user.label = label
        with user.lock:
            # stable device profile per phone
            user.app.session.apply_device(build_device_profile(seed=phone))
            r = user.app.auth.login_password(phone, password)
            if not r.ok or not user.app.session.logged_in:
                if created:
                    self.drop(user.web_sid)
                raise RuntimeError(
                    r.message or r.code or r.raw[:200] or "login failed"
                )
            # The protocol helper keeps the submitted password for CLI refresh.  The
            # Web process never needs to retain it after the request completes.
            user.app.session.password = ""
            self.rotate_sid(user)
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
        if not self.allow_weak_onekey:
            raise PermissionError("weak one-key login is disabled")
        user = self.get(web_sid) if web_sid else None
        created = user is None
        if not user:
            user = self.create(label=label or phone)
        elif label:
            user.label = label
        with user.lock:
            user.app.session.apply_device(build_device_profile(seed=phone))
            r = user.app.auth.login_onekey(phone)
            if not r.ok or not user.app.session.logged_in:
                if created:
                    self.drop(user.web_sid)
                raise RuntimeError(
                    r.message or r.code or r.raw[:200] or "onekey login failed"
                )
            user.app.session.password = ""
            self.rotate_sid(user)
            user.persist()
            do_hb = self.auto_heartbeat if start_hb is None else start_hb
            if do_hb and user.app.session.logged_in:
                user.start_heartbeat(self.heartbeat_interval)
            user.touch()
            return user

    def restore_from_disk(self, uid: str, web_sid: Optional[str] = None) -> Optional[WebUser]:
        """Attach a previously saved protocol session (uid.json) to a web session."""
        if not self.persist_sessions:
            return None
        path = _session_path(uid)
        if path is None:
            return None
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
            user.persist_sessions = True
            if self.auto_heartbeat:
                user.start_heartbeat(self.heartbeat_interval)
            user.touch()
            return user

    def put(self, user: WebUser) -> None:
        """Register / replace a WebUser in the store (e.g. after sms login)."""
        with self._lock:
            self.users[user.web_sid] = user

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_web_sessions": len(self.users),
                "ttl_sec": self.ttl_sec,
                "auto_heartbeat": self.auto_heartbeat,
                "persist_sessions": self.persist_sessions,
            }
