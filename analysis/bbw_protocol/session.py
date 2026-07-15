"""Persistent login session."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "session.json"


@dataclass
class Session:
    uid: str = "0"
    token: str = "0"
    phone: str = ""
    password: str = ""
    nickname: str = ""
    user_role: str = ""
    rp_verify_time: str = "0"
    vip: str = "0"
    svip: str = "0"
    money: str = "0"
    portrait: str = ""
    user_sign: str = ""
    login_id: str = ""
    raw_user: Dict[str, Any] = field(default_factory=dict)
    path: str = str(DEFAULT_PATH)

    @property
    def logged_in(self) -> bool:
        return bool(self.uid and self.uid != "0" and self.token and self.token != "0")

    @property
    def is_realname(self) -> bool:
        return bool(self.rp_verify_time and self.rp_verify_time != "0")

    def update_from_user(self, user: Dict[str, Any]) -> None:
        self.raw_user = user or {}
        self.uid = str(user.get("id") or self.uid or "0")
        self.token = str(user.get("token") or self.token or "0")
        self.nickname = str(user.get("nickname") or "")
        self.user_role = str(user.get("user_role") or "")
        self.rp_verify_time = str(user.get("rp_verify_time") or "0")
        self.vip = str(user.get("vip") or "0")
        self.svip = str(user.get("svip") or "0")
        self.money = str(user.get("money") or "0")
        self.portrait = str(user.get("portrait") or "")
        self.user_sign = str(user.get("userSign") or user.get("user_sign") or "")
        self.login_id = str(user.get("login_id") or "")
        phone = user.get("phone")
        if phone and "*" not in str(phone):
            self.phone = str(phone)

    def save(self, path: Optional[str] = None) -> Path:
        p = Path(path or self.path)
        data = asdict(self)
        data.pop("path", None)
        # keep password only if user wants local CTF store
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path = str(p)
        return p

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Session":
        p = Path(path or DEFAULT_PATH)
        if not p.exists():
            return cls(path=str(p))
        data = json.loads(p.read_text(encoding="utf-8"))
        raw_user = data.pop("raw_user", {}) or {}
        sess = cls(path=str(p), **{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        sess.raw_user = raw_user
        return sess

    def summary(self) -> Dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "uid": self.uid,
            "nickname": self.nickname,
            "user_role": self.user_role,
            "rp_verify_time": self.rp_verify_time,
            "is_realname": self.is_realname,
            "vip": self.vip,
            "svip": self.svip,
            "money": self.money,
            "phone": self.phone,
            "token_prefix": (self.token or "")[:24],
        }
