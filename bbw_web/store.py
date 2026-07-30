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

import httpx

from bbw_web.providers import (
    ProviderApplication,
    ProviderAuthenticationRejected,
    ProviderHeartbeat,
    ProviderNativeBundle,
    ProviderRuntime,
    ProviderUnavailable,
    ProviderUpstreamInterrupted,
    RuntimeProvider,
)

# bbw_web owns multi-user paths; protocol core stays single-session oriented
REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "sessions"
WEB_META_DIR = Path(__file__).resolve().parent / "data"


def _default_runtime_provider() -> RuntimeProvider:
    """Resolve the legacy provider only when a store actually needs it."""
    from bbw_web.providers import LegacyBanghuaProvider

    return LegacyBanghuaProvider()


def close_web_runtime(app: Any, native: Any = None) -> None:
    """Close both persistent HTTP clients owned by one Web runtime."""

    try:
        app.client.close()
    except Exception:
        pass
    tim_rest = getattr(native, "tim_rest", None)
    close_tim = getattr(tim_rest, "close", None)
    if callable(close_tim):
        try:
            close_tim()
        except Exception:
            pass


class _RequestGateLease:
    """One idempotent reader/writer gate lease held by an HTTP request."""

    def __init__(self, gate: "RequestGate", *, write: bool) -> None:
        self._gate = gate
        self._write = write
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release(write=self._write)


class RequestGate:
    """Allow concurrent reads while keeping account mutations exclusive."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> _RequestGateLease:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        return _RequestGateLease(self, write=False)

    def acquire_write(self) -> _RequestGateLease:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        return _RequestGateLease(self, write=True)

    def _release(self, *, write: bool) -> None:
        with self._condition:
            if write:
                self._writer = False
            else:
                self._readers = max(0, self._readers - 1)
            self._condition.notify_all()


class _PeerRequestGateLease:
    def __init__(
        self,
        gate: "PeerRequestGate",
        entries: list[tuple[str, threading.RLock]],
    ) -> None:
        self._gate = gate
        self._entries = entries
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for _peer, lock in reversed(self._entries):
            lock.release()
        self._gate._release_entries(self._entries)


class PeerRequestGate:
    """Serialize mutations for the same peer without blocking account reads."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, tuple[threading.RLock, int]] = {}

    @staticmethod
    def _peers(values: List[str]) -> list[str]:
        return sorted(
            dict.fromkeys(
                str(value or "").strip()[:128]
                for value in values[:100]
                if str(value or "").strip()
            )
        )

    def acquire(self, peers: List[str]) -> _PeerRequestGateLease:
        keys = self._peers(peers)
        entries: list[tuple[str, threading.RLock]] = []
        with self._guard:
            for peer in keys:
                lock, references = self._entries.get(
                    peer,
                    (threading.RLock(), 0),
                )
                self._entries[peer] = (lock, references + 1)
                entries.append((peer, lock))
        acquired: list[tuple[str, threading.RLock]] = []
        try:
            for entry in entries:
                entry[1].acquire()
                acquired.append(entry)
        except BaseException:
            for _peer, lock in reversed(acquired):
                lock.release()
            self._release_entries(entries)
            raise
        return _PeerRequestGateLease(self, entries)

    def _release_entries(
        self,
        entries: list[tuple[str, threading.RLock]],
    ) -> None:
        with self._guard:
            for peer, lock in entries:
                current = self._entries.get(peer)
                if current is None or current[0] is not lock:
                    continue
                references = current[1] - 1
                if references <= 0:
                    self._entries.pop(peer, None)
                else:
                    self._entries[peer] = (lock, references)


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
    app: ProviderApplication
    native: ProviderNativeBundle
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    heartbeat: Optional[ProviderHeartbeat] = None
    label: str = ""  # optional display label
    persist_sessions: bool = False
    match_pool_online_list_enabled: bool = False
    nearby_custom_city_enabled: bool = False
    friend_message_peers: set[str] = field(default_factory=set, repr=False)
    match_message_peers: set[str] = field(default_factory=set, repr=False)
    match_history: list[Dict[str, Any]] = field(default_factory=list, repr=False)
    conversation_message_peers: set[str] = field(default_factory=set, repr=False)
    blocked_message_peers: set[str] = field(default_factory=set, repr=False)
    blocked_by_message_peers: set[str] = field(default_factory=set, repr=False)
    blocked_message_peers_snapshot_at: float = field(default=0.0, repr=False)
    blocked_by_message_peers_snapshot_at: float = field(default=0.0, repr=False)
    message_blocks_retry_at: float = field(default=0.0, repr=False)
    # Voice matching and Rong credentials are intentionally session-memory
    # only.  They must never be written into persisted browser sessions.
    voice_match_state: Dict[str, Any] = field(default_factory=dict, repr=False)
    voice_rong_credentials: Optional[Dict[str, str]] = field(default=None, repr=False)
    voice_rong_credentials_at: float = field(default=0.0, repr=False)
    pending_until: Optional[float] = field(default=None, repr=False)
    profile_cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]] = field(
        default_factory=dict,
        repr=False,
    )
    request_gate: RequestGate = field(default_factory=RequestGate, repr=False)
    peer_request_gate: PeerRequestGate = field(
        default_factory=PeerRequestGate,
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
        self.heartbeat = self.app.create_heartbeat(interval_sec=interval_sec)
        self.heartbeat.start()
        return self.heartbeat.status()

    def heartbeat_once(
        self,
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> Dict[str, Any]:
        if self.heartbeat is None:
            self.heartbeat = self.app.create_heartbeat()
        call_options: Dict[str, Any] = {}
        if timeout is not None:
            call_options["timeout"] = timeout
        if deadline is not None:
            call_options["deadline"] = deadline
        return self.heartbeat.once(**call_options)

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
            "capabilities": self.capabilities(),
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

    def capabilities(self) -> Dict[str, bool]:
        enabled = bool(self.match_pool_online_list_enabled)
        local_session = str(
            getattr(self, "authentication_source", "") or ""
        ) == "local"
        return {
            "match_pool_online_list": True,
            "proactive_private_message": enabled,
            "direct_im_credentials": enabled and not local_session,
            "nearby_custom_city": bool(self.nearby_custom_city_enabled),
        }


class SessionStore:
    """Thread-safe multi-tenant map: web_sid → WebUser."""

    def __init__(
        self,
        *,
        ttl_sec: float = 86400.0 * 7,
        auto_heartbeat: bool = True,
        heartbeat_interval: float = 55.0,
        upstream_auth_timeout_sec: float = 5.0,
        persist_sessions: bool = False,
        allow_weak_onekey: bool = False,
        pending_expire_callback: Optional[Callable[[WebUser], None]] = None,
        runtime_provider: Optional[RuntimeProvider] = None,
    ):
        self._lock = threading.RLock()
        self.users: Dict[str, WebUser] = {}
        self.ttl_sec = ttl_sec
        self.auto_heartbeat = auto_heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.upstream_auth_timeout_sec = max(
            1.0, float(upstream_auth_timeout_sec)
        )
        self.persist_sessions = bool(persist_sessions)
        self.allow_weak_onekey = bool(allow_weak_onekey)
        self.pending_expire_callback = pending_expire_callback
        self.runtime_provider = (
            runtime_provider
            if runtime_provider is not None
            else _default_runtime_provider()
        )
        if self.persist_sessions:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            WEB_META_DIR.mkdir(parents=True, exist_ok=True)

    def _new_sid(self) -> str:
        return secrets.token_urlsafe(24)

    def _register_runtime(
        self,
        runtime: ProviderRuntime,
        *,
        label: str = "",
    ) -> WebUser:
        sid = self._new_sid()
        user = WebUser(
            web_sid=sid,
            app=runtime.app,
            native=runtime.native,
            label=label or "",
            persist_sessions=self.persist_sessions,
        )
        self.users[sid] = user
        return user

    def create(self, label: str = "") -> WebUser:
        with self._lock:
            self.purge_expired()
            sid = self._new_sid()
            runtime = self.runtime_provider.create_runtime()
            # provisional device; re-seeded on login with phone
            try:
                runtime.app.set_device(seed=sid[:12])
            except Exception:
                close_web_runtime(runtime.app, runtime.native)
                raise
            user = WebUser(
                web_sid=sid,
                app=runtime.app,
                native=runtime.native,
                label=label or "",
                persist_sessions=self.persist_sessions,
            )
            self.users[sid] = user
            return user

    def send_sms(self, phone: str) -> Any:
        """Send one SMS through an isolated provider runtime."""
        runtime = self.runtime_provider.create_runtime()
        try:
            return runtime.app.auth.send_sms(phone)
        finally:
            close_web_runtime(runtime.app, runtime.native)

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
        else:
            close_web_runtime(u.app, u.native)
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
            try:
                # stable device profile per phone
                user.app.set_device(seed=phone)
                client = getattr(user.app, "client", None)
                previous_timeout = getattr(client, "timeout", None)
                timeout_changed = False
                if previous_timeout is not None:
                    try:
                        client.timeout = min(
                            float(previous_timeout),
                            self.upstream_auth_timeout_sec,
                        )
                        timeout_changed = True
                    except (TypeError, ValueError, OverflowError, AttributeError):
                        timeout_changed = False
                try:
                    r = user.app.auth.login_password(phone, password)
                finally:
                    if timeout_changed:
                        client.timeout = previous_timeout
                authenticated = bool(r.ok) and bool(user.app.session.logged_in)
            except httpx.TransportError as e:
                # 协议层已把超时/网络/代理故障折算为 status=-1（走
                # ProviderUnavailable → 允许本地密码回退）。能逃逸到这里
                # 的只剩未折算的传输异常——典型是 RemoteProtocolError：
                # 复用超过 keepalive_expiry 的陈旧 keep-alive 连接被上游
                # 或中间层先行关闭。这是可自愈的瞬态故障，包装为专用
                # 异常向 BFF 传递「可重试、但不触发本地密码回退」的信号，
                # 避免它落入泛化异常路径被呈现为形似密码错误的 400。
                try:
                    user.app.session.password = ""
                except Exception:
                    pass
                if created:
                    self.drop(user.web_sid)
                raise ProviderUpstreamInterrupted(
                    str(e) or "upstream connection interrupted"
                ) from e
            except Exception:
                # An arbitrary adapter exception is not evidence that the
                # upstream is unavailable.  Preserve its type for the BFF's
                # conservative generic-failure path and clean up a provisional
                # runtime before returning control to the caller.
                try:
                    user.app.session.password = ""
                except Exception:
                    pass
                if created:
                    self.drop(user.web_sid)
                raise
            if not authenticated:
                user.app.session.password = ""
                if created:
                    self.drop(user.web_sid)
                try:
                    upstream_status = int(getattr(r, "status", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    upstream_status = 0
                upstream_code = str(getattr(r, "code", "") or "")
                message = str(
                    getattr(r, "message", "")
                    or upstream_code
                    or str(getattr(r, "raw", "") or "")[:200]
                    or "login failed"
                )
                error_type = (
                    ProviderUnavailable
                    if upstream_status < 0 or 500 <= upstream_status < 600
                    else ProviderAuthenticationRejected
                )
                raise error_type(
                    message,
                    upstream_status=upstream_status,
                    upstream_code=upstream_code,
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
            user.app.set_device(seed=phone)
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
            runtime = self.runtime_provider.load_runtime(path)
            sess = runtime.app.session
            if not sess.logged_in:
                close_web_runtime(runtime.app, runtime.native)
                return None
            user = self.get(web_sid) if web_sid else None
            if not user:
                self.purge_expired()
                user = self._register_runtime(
                    runtime,
                    label=str(getattr(sess, "nickname", "") or uid),
                )
            else:
                user.stop_heartbeat()
                previous_app = user.app
                previous_native = user.native
                user.app = runtime.app
                user.native = runtime.native
                if previous_app is not runtime.app:
                    close_web_runtime(previous_app, previous_native)
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
            now = time.time()
            return {
                "active_web_sessions": len(self.users),
                "pending_logins": sum(
                    1
                    for user in self.users.values()
                    if user.pending_until is not None and user.pending_until > now
                ),
                "ttl_sec": self.ttl_sec,
                "auto_heartbeat": self.auto_heartbeat,
                "persist_sessions": self.persist_sessions,
            }
