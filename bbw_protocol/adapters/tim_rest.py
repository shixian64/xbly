"""Tencent Cloud IM REST (server-side) fallback when browser TIM Web SDK cannot login.

Uses the SDKAppID plus an externally configured secret only inside the BFF/protocol process.
Docs: https://cloud.tencent.com/document/product/269/2282
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from .. import sign

REST_BASE = "https://console.tim.qq.com/v4"
DEFAULT_ADMIN = "administrator"
SUPPORTED_ELEMENT_TYPES = {
    "TIMTextElem",
    "TIMCustomElem",
    "TIMImageElem",
    "TIMSoundElem",
    "TIMVideoFileElem",
    "TIMFileElem",
    "TIMLocationElem",
    "TIMFaceElem",
}


@dataclass
class RestResult:
    ok: bool
    action: str
    error_code: int = 0
    error_info: str = ""
    data: Any = None
    raw: str = ""
    failure_kind: str = ""
    retryable: bool = False
    status_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "error_code": self.error_code,
            "error_info": self.error_info,
            "data": self.data,
        }


class TimRestClient:
    """TIM REST fallback for account probes and already-hosted C2C elements.

    The REST API can reference media that has already been uploaded, but it
    cannot turn a browser ``File`` or a local path into a TIM media object.
    Product Web uploads therefore stay on the official TIM Web SDK; these
    helpers are intentionally named ``send_remote_*`` to avoid false success.
    """

    def __init__(
        self,
        sdk_app_id: int = sign.TXIM_SDKAPPID,
        secret_key: str = sign.TXIM_SECRETKEY,
        admin_id: str = DEFAULT_ADMIN,
        timeout: int = 15,
    ):
        import httpx

        self.sdk_app_id = int(sdk_app_id)
        self.secret_key = secret_key
        self.admin_id = admin_id
        self.timeout = timeout
        self._ctx = ssl.create_default_context()
        self._http = httpx.Client(
            follow_redirects=True,
            verify=self._ctx,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=45.0,
            ),
            timeout=httpx.Timeout(float(timeout)),
        )

    def close(self) -> None:
        self._http.close()

    def _usersig(self, identifier: Optional[str] = None, expire: int = 86400) -> str:
        return sign.gen_user_sig(
            identifier or self.admin_id,
            sdkappid=self.sdk_app_id,
            secret_key=self.secret_key,
            expire=expire,
        )

    @staticmethod
    def _deadline_remaining(deadline: Any) -> Optional[float]:
        if deadline is None:
            return None
        remaining = getattr(deadline, "remaining", None)
        try:
            value = float(remaining() if callable(remaining) else float(deadline) - time.monotonic())
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    def _effective_timeout(self, timeout: Optional[float], deadline: Any) -> float:
        values = [float(self.timeout)]
        if timeout is not None:
            values.append(float(timeout))
        remaining = self._deadline_remaining(deadline)
        if remaining is not None:
            values.append(remaining)
        finite = [value for value in values if math.isfinite(value)]
        return min(finite) if finite else float(self.timeout)

    @staticmethod
    def _split_timeout(total: float) -> Any:
        import httpx

        normalized = max(0.05, float(total))
        return httpx.Timeout(
            connect=min(2.0, normalized),
            read=normalized,
            write=min(5.0, normalized),
            pool=min(1.0, normalized),
        )

    @staticmethod
    def _budget_kwargs(timeout: Optional[float], deadline: Any) -> Dict[str, Any]:
        output: Dict[str, Any] = {}
        if timeout is not None:
            output["timeout"] = timeout
        if deadline is not None:
            output["deadline"] = deadline
        return output

    def call(
        self,
        command: str,
        body: Dict[str, Any],
        *,
        admin: Optional[str] = None,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        import httpx

        identifier = admin or self.admin_id
        usersig = self._usersig(identifier)
        rnd = random.randint(0, 0xFFFFFFFF)
        url = (
            f"{REST_BASE}/{command}"
            f"?sdkappid={self.sdk_app_id}"
            f"&identifier={urllib.parse.quote(str(identifier))}"
            f"&usersig={urllib.parse.quote(usersig)}"
            f"&random={rnd}"
            f"&contenttype=json"
        )
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        effective_timeout = self._effective_timeout(timeout, deadline)
        if effective_timeout <= 0:
            return RestResult(
                ok=False,
                action=command,
                error_code=-1,
                error_info="interactive deadline exceeded",
                failure_kind="timeout",
                retryable=True,
            )
        try:
            response = self._http.post(
                url,
                content=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=self._split_timeout(effective_timeout),
            )
            raw = response.content.decode("utf-8", errors="replace")
        except httpx.TimeoutException:
            return RestResult(
                ok=False,
                action=command,
                error_code=-1,
                error_info="request timeout",
                failure_kind="timeout",
                retryable=True,
            )
        except httpx.TransportError:
            return RestResult(
                ok=False,
                action=command,
                error_code=-1,
                error_info="transport error",
                failure_kind="connection",
                retryable=True,
            )

        if response.status_code >= 400:
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            status = int(response.status_code)
            return RestResult(
                ok=False,
                action=command,
                error_code=int(data.get("ErrorCode") or status or -1),
                error_info=str(data.get("ErrorInfo") or f"HTTP {status}"),
                data=data,
                raw=raw,
                failure_kind=(
                    "rate_limited"
                    if status == 429
                    else "timeout"
                    if status in {408, 504}
                    else "upstream"
                    if status >= 500
                    else "contract"
                ),
                retryable=status in {408, 425, 429} or status >= 500,
                status_code=status,
            )

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return RestResult(
                ok=False,
                action=command,
                error_code=-1,
                error_info="non-json response",
                raw=raw[:500],
                failure_kind="contract",
            )

        code = int(data.get("ErrorCode") or 0)
        ok = str(data.get("ActionStatus") or "").upper() == "OK" or code == 0
        return RestResult(
            ok=ok,
            action=command,
            error_code=code,
            error_info=str(data.get("ErrorInfo") or ""),
            data=data,
            raw=raw,
        )

    def account_check(
        self,
        user_ids: List[str],
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        items = [{"UserID": str(u)} for u in user_ids if str(u).strip()]
        return self.call(
            "im_open_login_svc/account_check",
            {"CheckItem": items},
            **self._budget_kwargs(timeout, deadline),
        )

    def query_online(
        self,
        user_ids: List[str],
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        return self.call(
            "openim/query_online_status",
            {"To_Account": [str(u) for u in user_ids if str(u).strip()]},
            **self._budget_kwargs(timeout, deadline),
        )

    def recent_contacts(
        self,
        user_id: str,
        *,
        timestamp: int = 0,
        start_index: int = 0,
        top_timestamp: int = 0,
        top_start_index: int = 0,
        assist_flags: int = 7,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Return the server-owned recent C2C conversation list for one user."""

        account = str(user_id or "").strip()
        if not account:
            return RestResult(
                ok=False,
                action="recentcontact/get_list",
                error_code=-2,
                error_info="missing user account",
            )
        return self.call(
            "recentcontact/get_list",
            {
                "From_Account": account,
                "TimeStamp": max(0, int(timestamp)),
                "StartIndex": max(0, int(start_index)),
                "TopTimeStamp": max(0, int(top_timestamp)),
                "TopStartIndex": max(0, int(top_start_index)),
                "AssistFlags": max(0, int(assist_flags)),
            },
            **self._budget_kwargs(timeout, deadline),
        )

    def roaming_messages(
        self,
        from_account: str,
        to_account: str,
        *,
        min_time: int = 0,
        max_time: int = 0,
        max_count: int = 100,
        last_msg_key: str = "",
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Read one direction of C2C roaming history through the administrator API."""

        sender = str(from_account or "").strip()
        recipient = str(to_account or "").strip()
        if not sender or not recipient or sender == recipient:
            return RestResult(
                ok=False,
                action="openim/admin_getroammsg",
                error_code=-2,
                error_info="invalid from/to account",
            )
        return self.call(
            "openim/admin_getroammsg",
            {
                "From_Account": sender,
                "To_Account": recipient,
                "MaxCnt": max(1, min(int(max_count), 100)),
                "MinTime": max(0, int(min_time)),
                "MaxTime": max(0, int(max_time)),
                "LastMsgKey": str(last_msg_key or "")[:256],
            },
            **self._budget_kwargs(timeout, deadline),
        )

    def c2c_unread_counts(
        self,
        to_account: str,
        peer_accounts: List[str],
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Return per-peer C2C unread counts for one account."""

        account = str(to_account or "").strip()
        peers = list(
            dict.fromkeys(
                str(peer or "").strip()
                for peer in peer_accounts
                if str(peer or "").strip()
                and str(peer or "").strip() != account
            )
        )[:100]
        if not account or not peers:
            return RestResult(
                ok=False,
                action="openim/get_c2c_unread_msg_num",
                error_code=-2,
                error_info="missing target account or peer accounts",
            )
        return self.call(
            "openim/get_c2c_unread_msg_num",
            {
                "To_Account": account,
                "Peer_Account": peers,
            },
            **self._budget_kwargs(timeout, deadline),
        )

    def mark_c2c_read(
        self,
        report_account: str,
        peer_account: str,
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Mark one C2C conversation as read for ``report_account``."""

        account = str(report_account or "").strip()
        peer = str(peer_account or "").strip()
        if not account or not peer or account == peer:
            return RestResult(
                ok=False,
                action="openim/admin_set_msg_read",
                error_code=-2,
                error_info="invalid report/peer account",
            )
        return self.call(
            "openim/admin_set_msg_read",
            {
                "Report_Account": account,
                "Peer_Account": peer,
            },
            **self._budget_kwargs(timeout, deadline),
        )

    @staticmethod
    def _c2c_receipt_message(
        message: Mapping[str, Any],
        *,
        operator_account: str,
        peer_account: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one Tencent C2C message into the explicit receipt shape."""

        sender = str(
            message.get("From_Account")
            or message.get("from_account")
            or message.get("from")
            or ""
        ).strip()
        recipient = str(
            message.get("To_Account")
            or message.get("to_account")
            or message.get("to")
            or ""
        ).strip()
        if sender != peer_account or recipient != operator_account:
            return None

        need_receipt = message.get("IsNeedReadReceipt")
        if need_receipt is None:
            need_receipt = message.get("needReadReceipt")
        if need_receipt is None:
            need_receipt = message.get("need_read_receipt")
        if need_receipt is not None and str(need_receipt).strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            return None

        peer_read = message.get("IsPeerRead")
        if peer_read is None:
            peer_read = message.get("isPeerRead")
        if peer_read is None:
            peer_read = message.get("is_peer_read")
        if str(peer_read or "").strip().lower() in {"1", "true", "yes"}:
            return None

        def positive_int(*values: Any) -> int:
            for value in values:
                try:
                    parsed = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if parsed > 0:
                    return parsed
            return 0

        sequence = positive_int(
            message.get("MsgSeq"),
            message.get("sequence"),
            message.get("msg_sequence"),
        )
        random_value = positive_int(
            message.get("MsgRandom"),
            message.get("random"),
            message.get("message_random"),
        )
        message_time = positive_int(
            message.get("MsgTime"),
            message.get("MsgTimeStamp"),
            message.get("time"),
            message.get("timestamp"),
        )
        client_time = positive_int(
            message.get("MsgClientTime"),
            message.get("clientTime"),
            message.get("client_time"),
            message_time,
        )
        if not sequence or not random_value or not message_time:
            return None
        return {
            "From_Account": sender,
            "To_Account": recipient,
            "MsgSeq": sequence,
            "MsgRandom": random_value,
            "MsgTime": message_time,
            "MsgClientTime": client_time,
        }

    def mark_c2c_message_read_receipts(
        self,
        operator_account: str,
        peer_account: str,
        messages: List[Mapping[str, Any]],
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Send explicit per-message receipts used by TUIKit C2C messages."""

        operator = str(operator_account or "").strip()
        peer = str(peer_account or "").strip()
        action = "openim/c2c_msg_read_receipt"
        if not operator or not peer or operator == peer:
            return RestResult(
                ok=False,
                action=action,
                error_code=-2,
                error_info="invalid operator/peer account",
            )
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            item = self._c2c_receipt_message(
                message,
                operator_account=operator,
                peer_account=peer,
            )
            if not item:
                continue
            identity = (
                item["From_Account"],
                item["To_Account"],
                item["MsgSeq"],
                item["MsgRandom"],
                item["MsgTime"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(item)
        if not normalized:
            return RestResult(
                ok=True,
                action=action,
                data={"ActionStatus": "OK", "ErrorCode": 0, "receipt_count": 0},
            )
        return self.call(
            action,
            {
                "Operator_Account": operator,
                "Peer_Account": peer,
                "C2CMsgInfo": normalized,
            },
            **self._budget_kwargs(timeout, deadline),
        )

    def sync_c2c_message_read_receipts(
        self,
        operator_account: str,
        peer_account: str,
        *,
        messages: Optional[List[Mapping[str, Any]]] = None,
        max_messages: int = 300,
        batch_size: int = 30,
        retention_days: int = 180,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> RestResult:
        """Discover and send missing explicit C2C receipts for one conversation.

        Browser SDK messages may be supplied directly. REST/history mode falls
        back to administrator roaming history and therefore remains capable of
        updating TUIKit's per-message ``isPeerRead`` state.
        """

        operator = str(operator_account or "").strip()
        peer = str(peer_account or "").strip()
        action = "openim/c2c_msg_read_receipt"
        if not operator or not peer or operator == peer:
            return RestResult(
                ok=False,
                action=action,
                error_code=-2,
                error_info="invalid operator/peer account",
            )

        maximum = max(1, min(int(max_messages), 500))
        receipt_batch_size = max(1, min(int(batch_size), 30))
        candidates: List[Mapping[str, Any]] = []
        page_count = 0
        source = "browser"
        supplied = [item for item in (messages or []) if isinstance(item, Mapping)]
        if supplied:
            candidates.extend(supplied[-maximum:])
        else:
            source = "history"
            now_epoch = int(time.time())
            min_time = max(0, now_epoch - max(1, int(retention_days)) * 86400)
            max_time = now_epoch + 60
            last_msg_key = ""
            seen_pages = set()
            while len(candidates) < maximum:
                result = self.roaming_messages(
                    peer,
                    operator,
                    min_time=min_time,
                    max_time=max_time,
                    max_count=min(100, maximum - len(candidates)),
                    last_msg_key=last_msg_key,
                    **self._budget_kwargs(timeout, deadline),
                )
                page_count += 1
                if not result.ok:
                    return RestResult(
                        ok=False,
                        action=action,
                        error_code=result.error_code,
                        error_info=result.error_info or "failed to load C2C receipt messages",
                        data={
                            "stage": "history",
                            "source": source,
                            "page_count": page_count,
                            "upstream": result.data,
                        },
                    )
                data = result.data if isinstance(result.data, Mapping) else {}
                rows = data.get("MsgList")
                rows = rows if isinstance(rows, list) else []
                candidates.extend(item for item in rows if isinstance(item, Mapping))
                next_key = str(data.get("LastMsgKey") or "").strip()[:256]
                try:
                    next_time = int(data.get("LastMsgTime") or 0)
                except (TypeError, ValueError, OverflowError):
                    next_time = 0
                complete = str(data.get("Complete") or "0").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                }
                cursor = (next_key, next_time)
                if complete or not rows or not next_key or next_time <= 0 or cursor in seen_pages:
                    break
                seen_pages.add(cursor)
                last_msg_key = next_key
                max_time = min(max_time, next_time)

        normalized: List[Dict[str, Any]] = []
        seen_messages = set()
        for message in candidates:
            item = self._c2c_receipt_message(
                message,
                operator_account=operator,
                peer_account=peer,
            )
            if not item:
                continue
            identity = (
                item["From_Account"],
                item["To_Account"],
                item["MsgSeq"],
                item["MsgRandom"],
                item["MsgTime"],
            )
            if identity in seen_messages:
                continue
            seen_messages.add(identity)
            normalized.append(item)

        receipt_count = 0
        batch_count = 0
        for offset in range(0, len(normalized), receipt_batch_size):
            batch = normalized[offset : offset + receipt_batch_size]
            result = self.mark_c2c_message_read_receipts(
                operator,
                peer,
                batch,
                **self._budget_kwargs(timeout, deadline),
            )
            batch_count += 1
            if not result.ok:
                return RestResult(
                    ok=False,
                    action=action,
                    error_code=result.error_code,
                    error_info=result.error_info or "failed to send C2C message read receipts",
                    data={
                        "stage": "receipt",
                        "source": source,
                        "scanned_count": len(candidates),
                        "pending_count": len(normalized),
                        "receipt_count": receipt_count,
                        "page_count": page_count,
                        "batch_count": batch_count,
                        "upstream": result.data,
                    },
                )
            receipt_count += len(batch)

        return RestResult(
            ok=True,
            action=action,
            data={
                "ActionStatus": "OK",
                "ErrorCode": 0,
                "source": source,
                "scanned_count": len(candidates),
                "pending_count": len(normalized),
                "receipt_count": receipt_count,
                "page_count": page_count,
                "batch_count": batch_count,
            },
        )

    def revoke_c2c(self, from_account: str, to_account: str, msg_key: str) -> RestResult:
        """Recall one C2C message previously sent by ``from_account``.

        Tencent identifies a REST-sent/history message with ``MsgKey``.  Unlike
        the client SDK's default two-minute window, this administrator API has
        no fixed recall deadline, but the message must still be inside the
        application's roaming-storage lifetime.  The BFF supplies
        ``from_account`` from the authenticated session so a browser cannot
        recall a message on behalf of another user.
        """
        sender = str(from_account or "").strip()
        receiver = str(to_account or "").strip()
        key = str(msg_key or "").strip()
        if not sender or not receiver or not key:
            return RestResult(
                ok=False,
                action="openim/admin_msgwithdraw",
                error_code=-2,
                error_info="missing from/to account or MsgKey",
            )
        return self.call(
            "openim/admin_msgwithdraw",
            {
                "From_Account": sender,
                "To_Account": receiver,
                "MsgKey": key,
            },
        )

    @staticmethod
    def _remote_url(value: Any) -> str:
        url = str(value or "").strip()
        return url if url.startswith(("https://", "http://")) else ""

    @staticmethod
    def _cloud_data(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _invalid(error: str, code: int = -2) -> RestResult:
        return RestResult(
            ok=False,
            action="openim/sendmsg",
            error_code=code,
            error_info=error,
        )

    def _element_error(self, msg_type: str, content: Dict[str, Any]) -> str:
        if msg_type == "TIMTextElem" and not str(content.get("Text") or "").strip():
            return "empty TIMTextElem text"
        if msg_type == "TIMFaceElem" and not str(content.get("Data") or "").strip():
            return "empty TIMFaceElem data"
        if msg_type == "TIMImageElem":
            infos = content.get("ImageInfoArray")
            has_url = isinstance(infos, list) and any(
                isinstance(info, dict) and self._remote_url(info.get("URL") or info.get("url"))
                for info in infos
            )
            if not str(content.get("UUID") or "").strip() or not has_url:
                return "TIMImageElem requires uploaded UUID and http(s) ImageInfoArray"
        if msg_type == "TIMSoundElem":
            if not self._remote_url(content.get("Url") or content.get("URL")):
                return "TIMSoundElem requires an uploaded http(s) URL"
            if not str(content.get("UUID") or "").strip():
                return "TIMSoundElem requires UUID"
        if msg_type == "TIMVideoFileElem":
            if not self._remote_url(content.get("VideoUrl")) or not self._remote_url(
                content.get("ThumbUrl")
            ):
                return "TIMVideoFileElem requires uploaded video and thumbnail URLs"
            if not str(content.get("VideoUUID") or "").strip() or not str(
                content.get("ThumbUUID") or ""
            ).strip():
                return "TIMVideoFileElem requires video and thumbnail UUIDs"
        if msg_type == "TIMFileElem":
            if not self._remote_url(content.get("Url") or content.get("URL")):
                return "TIMFileElem requires an uploaded http(s) URL"
            if not str(content.get("UUID") or "").strip() or not str(
                content.get("FileName") or ""
            ).strip():
                return "TIMFileElem requires UUID and file name"
        if msg_type == "TIMCustomElem" and not any(
            str(content.get(key) or "") for key in ("Data", "Desc", "Ext")
        ):
            return "empty TIMCustomElem"
        return ""

    def send_elements(
        self,
        from_account: str,
        to_account: str,
        elements: List[Dict[str, Any]],
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
        offline_push_info: Optional[Dict[str, Any]] = None,
        idempotency_key: str | None = None,
    ) -> RestResult:
        """Send documented TIM ``MsgBody`` elements that need no local upload."""
        if not str(from_account or "").strip() or not str(to_account or "").strip():
            return self._invalid("missing account")
        if not isinstance(elements, list) or not elements:
            return self._invalid("empty MsgBody")
        body_elements: List[Dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                return self._invalid("invalid MsgBody element")
            msg_type = str(element.get("MsgType") or "").strip()
            content = element.get("MsgContent")
            if msg_type not in SUPPORTED_ELEMENT_TYPES:
                return self._invalid(f"unsupported TIM element: {msg_type or 'empty'}")
            if not isinstance(content, dict):
                return self._invalid(f"{msg_type} MsgContent must be an object")
            element_error = self._element_error(msg_type, content)
            if element_error:
                return self._invalid(element_error)
            body_elements.append({"MsgType": msg_type, "MsgContent": dict(content)})

        deduplication_key = str(idempotency_key or "").strip()
        if deduplication_key:
            digest = hashlib.sha256(deduplication_key.encode("utf-8")).digest()
            message_sequence = int.from_bytes(digest[:4], "big") or 1
            message_random = int.from_bytes(digest[4:8], "big") or 1
        else:
            message_sequence = None
            message_random = random.randint(0, 0xFFFFFFFF)
        body: Dict[str, Any] = {
            "SyncOtherMachine": int(sync_other_machine),
            "From_Account": str(from_account),
            "To_Account": str(to_account),
            "MsgRandom": message_random,
            "MsgBody": body_elements,
        }
        if message_sequence is not None:
            # TIM deduplicates C2C sends carrying the same MsgSeq.  Keeping both
            # values stable closes the worker crash window between an upstream
            # success and the local outbox acknowledgement.
            body["MsgSeq"] = message_sequence
        cloud_data = self._cloud_data(cloud_custom_data)
        if cloud_data:
            body["CloudCustomData"] = cloud_data
        if isinstance(offline_push_info, dict) and offline_push_info:
            body["OfflinePushInfo"] = dict(offline_push_info)
        return self.call("openim/sendmsg", body)

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
        idempotency_key: str | None = None,
    ) -> RestResult:
        """Send a C2C text message as from_account (admin API, appears from that user)."""
        text = str(text or "").strip()
        if not text:
            return RestResult(ok=False, action="openim/sendmsg", error_info="empty text")
        if not from_account or not to_account:
            return RestResult(ok=False, action="openim/sendmsg", error_info="missing account")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMTextElem",
                    "MsgContent": {"Text": text[:2000]},
                }
            ],
            cloud_custom_data=cloud_custom_data,
            sync_other_machine=sync_other_machine,
            idempotency_key=idempotency_key,
        )

    def send_face(
        self,
        from_account: str,
        to_account: str,
        index: int,
        data: str,
        *,
        sync_other_machine: int = 1,
    ) -> RestResult:
        face_data = str(data or "").strip()
        if not face_data:
            return self._invalid("empty TIMFaceElem data")
        return self.send_elements(
            from_account,
            to_account,
            [{"MsgType": "TIMFaceElem", "MsgContent": {"Index": int(index), "Data": face_data}}],
            sync_other_machine=sync_other_machine,
        )

    def send_remote_image(
        self,
        from_account: str,
        to_account: str,
        *,
        uuid: str,
        image_info: List[Dict[str, Any]],
        image_format: int = 1,
    ) -> RestResult:
        infos: List[Dict[str, Any]] = []
        for raw in image_info if isinstance(image_info, list) else []:
            if not isinstance(raw, dict):
                continue
            url = self._remote_url(raw.get("URL") or raw.get("url"))
            if not url:
                continue
            infos.append(
                {
                    "Type": int(raw.get("Type", raw.get("type", 0)) or 0),
                    "Size": int(raw.get("Size", raw.get("size", 0)) or 0),
                    "Width": int(raw.get("Width", raw.get("width", 0)) or 0),
                    "Height": int(raw.get("Height", raw.get("height", 0)) or 0),
                    "URL": url,
                }
            )
        if not str(uuid or "").strip() or not infos:
            return self._invalid("remote image requires UUID and an http(s) ImageInfoArray")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMImageElem",
                    "MsgContent": {
                        "UUID": str(uuid),
                        "ImageFormat": int(image_format),
                        "ImageInfoArray": infos,
                    },
                }
            ],
        )

    def send_remote_audio(
        self,
        from_account: str,
        to_account: str,
        *,
        url: str,
        uuid: str,
        duration: int,
        size: int = 0,
    ) -> RestResult:
        remote_url = self._remote_url(url)
        if not remote_url or not str(uuid or "").strip() or int(duration or 0) <= 0:
            return self._invalid("remote audio requires URL, UUID and positive duration")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMSoundElem",
                    "MsgContent": {
                        "Url": remote_url,
                        "UUID": str(uuid),
                        "Size": max(0, int(size or 0)),
                        "Second": int(duration),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

    def send_remote_video(
        self,
        from_account: str,
        to_account: str,
        *,
        video_url: str,
        video_uuid: str,
        duration: int,
        thumb_url: str,
        thumb_uuid: str,
        video_size: int = 0,
        thumb_size: int = 0,
        thumb_width: int = 0,
        thumb_height: int = 0,
        video_format: str = "mp4",
    ) -> RestResult:
        remote_video = self._remote_url(video_url)
        remote_thumb = self._remote_url(thumb_url)
        if not all((remote_video, remote_thumb, str(video_uuid or "").strip(), str(thumb_uuid or "").strip())):
            return self._invalid("remote video requires video/thumb URLs and UUIDs")
        if int(duration or 0) <= 0:
            return self._invalid("remote video requires positive duration")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMVideoFileElem",
                    "MsgContent": {
                        "VideoUrl": remote_video,
                        "VideoUUID": str(video_uuid),
                        "VideoSize": max(0, int(video_size or 0)),
                        "VideoSecond": int(duration),
                        "VideoFormat": str(video_format or "mp4"),
                        "ThumbUrl": remote_thumb,
                        "ThumbUUID": str(thumb_uuid),
                        "ThumbSize": max(0, int(thumb_size or 0)),
                        "ThumbWidth": max(0, int(thumb_width or 0)),
                        "ThumbHeight": max(0, int(thumb_height or 0)),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

    def send_remote_file(
        self,
        from_account: str,
        to_account: str,
        *,
        url: str,
        uuid: str,
        file_name: str,
        file_size: int = 0,
    ) -> RestResult:
        remote_url = self._remote_url(url)
        if not all((remote_url, str(uuid or "").strip(), str(file_name or "").strip())):
            return self._invalid("remote file requires URL, UUID and file name")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMFileElem",
                    "MsgContent": {
                        "Url": remote_url,
                        "UUID": str(uuid),
                        "FileSize": max(0, int(file_size or 0)),
                        "FileName": str(file_name),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

    def health(self, sample_uid: str = "1") -> Dict[str, Any]:
        """Quick connectivity check: admin UserSig + account_check."""
        r = self.account_check([sample_uid])
        return {
            "ok": r.ok or r.error_code in (0,),
            "sdk_app_id": self.sdk_app_id,
            "admin": self.admin_id,
            "error_code": r.error_code,
            "error_info": r.error_info,
            "note": "REST works with APK secret; use when browser TIM.login hangs.",
        }
