"""Authenticated JD live polling: provider event -> durable archive -> browser."""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from bbw_web import bff_server as legacy

LOGGER = logging.getLogger(__name__)


def live_messages(*, user, identity, persistence, cursor: str = '') -> tuple[int, dict]:
    """Called after the normal durable-session and API-rate-limit checks.

    The cursor is session-buffer-local, never a user selector. Only an existing
    authorized C2C relationship is exposed; all payloads are server-normalized.
    Archive failure returns no cursor so the same buffered batch can be retried.
    """
    def failure(code, message, status=503):
        return status, {'ok': False, 'code': code, 'error': message, 'retryable': status == 503}

    if identity is None or user is None:
        return failure('AUTH_REQUIRED', '请先登录', 401)
    account = str(identity.upstream_uid or '')
    if account != str(getattr(user.app.session, 'uid', '') or ''):
        return failure('JD_LIVE_IDENTITY_MISMATCH', '消息身份已变化，请重新登录', 401)
    if cursor and not re.fullmatch(r'[0-9a-f]{32}:[0-9]{1,18}', cursor):
        return failure('INVALID_LIVE_CURSOR', '实时消息游标无效', 400)
    try:
        # Do not widen messaging permission when the provider is degraded.
        # Complete owner-bound block snapshots must already exist.
        if persistence.trusted_message_block_snapshot(identity) is None:
            return failure('MESSAGE_POLICY_NOT_READY', '私聊安全策略尚未就绪')
        client = getattr(user.native, 'jd_chat', None)
        poll = getattr(client, 'poll_live', None)
        if not callable(poll):
            return failure('JD_LIVE_UNAVAILABLE', '实时消息接收暂不可用')
        with user.lock:
            batch = poll(cursor, limit=50)
            groups = defaultdict(list)
            allowed = {}
            for raw in batch.get('items', [])[:50]:
                sender = str(raw.get('fromUserId') or '')
                recipient = str(raw.get('toUserId') or '')
                if account not in {sender, recipient} or sender == recipient:
                    continue
                peer = recipient if sender == account else sender
                if not peer or peer == legacy.SYSTEM_CUSTOMER_SERVICE_UID:
                    continue
                if peer not in allowed:
                    allowed[peer] = persistence.can_message_peer(identity, peer)
                if not allowed[peer]:
                    continue
                groups[peer].append(raw)
            envelopes = []
            items = []
            for peer, rows in groups.items():
                payload = legacy._jd_message_envelope({'ok': True, 'data': rows}, account, peer)
                items.extend(payload['items'])
            if items:
                envelopes = [{'path': '/api/im/messages', 'query': {}, 'response_data': {'ok': True, 'items': items}}]
                # Synchronous DB commit, not an in-memory/best-effort queue.
                # Replay is safe because archive ingestion is idempotent.
                result = persistence.archive_jd_live_messages(identity, envelopes)
                if result.get('ok') is not True:
                    raise RuntimeError('JD live archive did not commit')
            return 200, {
                'ok': True, 'items': items, 'count': len(items), 'provider': 'jd-chat',
                'cursor': str(batch.get('cursor') or ''),
                'connected': batch.get('connected') is True,
                'gap': batch.get('gap') is True, 'has_more': batch.get('has_more') is True,
                'history_complete': False,
            }
    except Exception:
        # Raw errors may contain provider URLs/tokens; do not reflect/log them.
        LOGGER.warning('JD live poll or archive unavailable')
        return failure('JD_LIVE_UNAVAILABLE', '实时消息接收或归档暂不可用，请稍后重试')
