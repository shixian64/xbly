from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bbw_protocol.adapters.jd_chat import JdChatClient
from bbw_protocol.adapters.jd_realtime import JdRealtimeReceiver, websocket_base
from bbw_web.jd_live import live_messages


def event(mid='event-1', sender=9, recipient=42, **kwargs):
    return {'messageId': mid, 'fromUserId': sender, 'toUserId': recipient,
            'type': 'TEXT', 'content': 'test-only text', 'timestamp': 1792800000000, **kwargs}


class ReceiverBufferTests(unittest.TestCase):
    def setUp(self):
        self.receiver = JdRealtimeReceiver(uid='42', token='synthetic-secret', base_url='https://example.invalid')
        self.start = patch.object(self.receiver, '_start_locked').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.receiver.close)

    def accept(self, **kwargs):
        self.receiver._accept(json.dumps(event(**kwargs)))

    def test_own_c2c_message_is_allowlisted_and_cursor_replayable(self):
        self.accept(token='must-not-escape', debug={'Authorization': 'must-not-escape'})
        result = self.receiver.poll()
        self.assertEqual(len(result['items']), 1)
        self.assertNotIn('token', result['items'][0])
        self.assertNotIn('debug', result['items'][0])
        self.assertEqual(self.receiver.poll()['items'], result['items'])
        self.assertEqual(self.receiver.poll(result['cursor'])['items'], [])

    def test_other_accounts_groups_self_and_controls_are_not_messages(self):
        for raw in [event(sender=7, recipient=8), event(roomId='group'), event(sender=42),
                    event(type='ACK'), event(type='READ_RECEIPT'), event(mid='')]:
            self.receiver._accept(json.dumps(raw))
        self.assertEqual(self.receiver.poll()['items'], [])

    def test_malformed_and_oversized_frames_do_not_poison_buffer(self):
        for raw in ['{bad', 'x' * (self.receiver.MAX_FRAME_BYTES + 1), '[]', 'null',
                    json.dumps(event(timestamp=float('nan'))), json.dumps(event(timestamp={})),
                    json.dumps(event(mid='bad\x00id')), json.dumps(event(sender='９'))]:
            self.receiver._accept(raw)
        self.assertEqual(self.receiver.poll()['items'], [])

    def test_native_proxy_wrapper_is_accepted(self):
        self.receiver._accept(json.dumps({'data': event()}))
        self.assertEqual(len(self.receiver.poll()['items']), 1)

    def test_count_bound_reports_gap_and_pages_without_losing_remainder(self):
        self.receiver.MAX_EVENTS = 3
        for i in range(7):self.accept(mid=f'event-{i}')
        page = self.receiver.poll(limit=2)
        self.assertTrue(page['gap'])
        self.assertTrue(page['has_more'])
        self.assertEqual([x['messageId'] for x in page['items']], ['event-4', 'event-5'])
        self.assertEqual(self.receiver.poll(page['cursor'])['items'][0]['messageId'], 'event-6')

    def test_byte_bound_and_returned_data_are_isolated(self):
        self.receiver.MAX_BUFFER_BYTES = 600
        for i in range(8):self.accept(mid=f'event-{i}', content='x'*80)
        result = self.receiver.poll()
        self.assertLessEqual(self.receiver._bytes, 600)
        result['items'][0]['content'] = 'modified by a caller'
        self.assertNotEqual(self.receiver.poll()['items'][0]['content'], 'modified by a caller')

    def test_old_runtime_cursor_resets_without_selecting_another_account(self):
        self.accept()
        result = self.receiver.poll('0'*32 + ':1')
        self.assertTrue(result['gap'])
        self.assertEqual(len(result['items']), 1)

    def test_invalid_cursor_is_rejected_before_starting_connection(self):
        with self.assertRaises(ValueError):self.receiver.poll('not-a-cursor')
        self.start.assert_not_called()

    def test_close_erases_credentials_events_and_prevents_reactivation(self):
        self.accept()
        self.receiver.close()
        self.assertEqual(self.receiver._token, '')
        self.assertEqual(self.receiver._bytes, 0)
        self.assertEqual(self.receiver.poll()['items'], [])
        self.start.assert_not_called()

    def test_full_process_capacity_does_not_start_a_thread(self):
        from bbw_protocol.adapters import jd_realtime
        with patch.object(jd_realtime._SLOTS, 'acquire', return_value=False), patch.object(threading.Thread, 'start') as start:
            JdRealtimeReceiver._start_locked(self.receiver)
        start.assert_not_called()

    def test_idle_restart_reports_history_gap(self):
        old = Mock()
        old.is_alive.return_value = False
        self.receiver._thread = old
        with patch('bbw_protocol.adapters.jd_realtime.threading.Thread') as thread, patch(
            'bbw_protocol.adapters.jd_realtime._SLOTS.acquire', return_value=True
        ), patch('bbw_protocol.adapters.jd_realtime._SLOTS.release'):
            JdRealtimeReceiver._start_locked(self.receiver)
            thread.return_value.start.assert_called_once()
        self.assertTrue(self.receiver.poll()['gap'])
        self.receiver._thread = None

    def test_default_ws_host_changes_only_the_exact_apk_http_host(self):
        self.assertEqual(websocket_base('https://test.banghua.xin'), 'wss://testchat.banghua.xin')
        self.assertEqual(websocket_base('https://test.banghua.xin.example.invalid'), 'wss://test.banghua.xin.example.invalid')
        with self.assertRaises(ValueError):websocket_base('https://user:secret@example.invalid')


class RuntimeLifecycleTests(unittest.TestCase):
    def test_token_rotation_scrubs_and_replaces_only_its_bound_receiver(self):
        session = SimpleNamespace(uid='42', token='first', raw_user={}, device_id='device')
        client = JdChatClient(session=session)
        self.addCleanup(client.close)
        with patch.object(JdRealtimeReceiver, '_start_locked'):
            client.poll_live()
            old = client._live_receiver
            old._accept(json.dumps(event()))
            session.token = 'second'
            result = client.poll_live()
            self.assertIsNot(old, client._live_receiver)
            self.assertEqual(old._token, '')
            self.assertEqual(result['items'], [])
            self.assertEqual(old._events, __import__('collections').deque())

    def test_logout_cannot_reopen_a_closed_runtime(self):
        client = JdChatClient(session=SimpleNamespace(uid='42', token='first', raw_user={}))
        client.close()
        with self.assertRaises(RuntimeError):client.poll_live()

    def test_real_local_websocket_receives_without_send_or_http_history(self):
        # A real loopback WebSocket, synthetic identity only; no challenge
        # upstream, database, production secret or user message is touched.
        async def scenario():
            from websockets.asyncio.server import serve
            received = []
            async def handler(ws):
                self.assertIn('token=synthetic', ws.request.path)
                self.assertEqual(ws.request.headers['Authorization'], 'Bearer synthetic')
                await ws.send(json.dumps(event()))
                try:received.append(await asyncio.wait_for(ws.recv(), timeout=2))
                except Exception:pass
            async with serve(handler, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                receiver = JdRealtimeReceiver(uid='42', token='synthetic', base_url=f'http://127.0.0.1:{port}')
                try:
                    for _ in range(100):
                        page = receiver.poll()
                        if page['items']:break
                        await asyncio.sleep(.01)
                    self.assertEqual(page['items'][0]['messageId'], 'event-1')
                    self.assertTrue(page['connected'])
                    await asyncio.to_thread(receiver.close)
                    self.assertFalse(receiver._thread.is_alive())
                    self.assertEqual(received, [])
                finally:await asyncio.to_thread(receiver.close)
        asyncio.run(scenario())


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        self.identity = SimpleNamespace(user_id='owner', external_account_id='account', upstream_uid='42')
        self.poll = Mock(return_value={'items': [event()], 'cursor': 'a'*32+':1', 'connected': True})
        self.user = SimpleNamespace(app=SimpleNamespace(session=SimpleNamespace(uid='42')),
                                    native=SimpleNamespace(jd_chat=SimpleNamespace(poll_live=self.poll)), lock=threading.RLock())
        self.persistence = SimpleNamespace(trusted_message_block_snapshot=Mock(return_value={'blacklist': [], 'blacklisted_by': []}),
                                           can_message_peer=Mock(return_value=True), archive_jd_live_messages=Mock(return_value={'ok': True}))

    def call(self, **kwargs):
        return live_messages(user=self.user, identity=self.identity, persistence=self.persistence, **kwargs)

    def test_commits_then_exposes_normalized_message_and_cursor_without_secrets(self):
        status, body = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(body['items'][0]['text'], 'test-only text')
        self.assertEqual(body['items'][0]['from'], '9')
        self.assertEqual(body['items'][0]['provider'], 'jd-chat')
        self.assertFalse(body['history_complete'])
        self.persistence.archive_jd_live_messages.assert_called_once()
        envelopes = self.persistence.archive_jd_live_messages.call_args.args[1]
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0]['path'], '/api/im/messages')

    def test_archive_failure_returns_neither_messages_nor_cursor_and_batch_is_retried(self):
        self.persistence.archive_jd_live_messages.side_effect = RuntimeError('secret URL must not escape')
        status, body = self.call()
        self.assertEqual(status, 503)
        self.assertNotIn('cursor', body)
        self.assertNotIn('items', body)
        self.assertNotIn('secret URL', json.dumps(body))
        self.persistence.archive_jd_live_messages.side_effect = None
        self.assertEqual(self.call()[0], 200)
        self.assertEqual(self.poll.call_args_list[0], self.poll.call_args_list[1])

    def test_unknown_or_revoked_session_cannot_start_a_receiver(self):
        self.assertEqual(live_messages(user=self.user, identity=None, persistence=self.persistence)[0], 401)
        self.poll.assert_not_called()

    def test_account_mismatch_cannot_leak_or_archive_other_runtime_messages(self):
        self.identity.upstream_uid = '7'
        self.assertEqual(self.call()[0], 401)
        self.poll.assert_not_called()

    def test_missing_policy_fails_closed_before_connecting(self):
        self.persistence.trusted_message_block_snapshot.return_value = None
        self.assertEqual(self.call()[0], 503)
        self.poll.assert_not_called()

    def test_blocked_or_ungranted_peer_is_not_returned_or_archived(self):
        self.persistence.can_message_peer.return_value = False
        status, body = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(body['items'], [])
        self.persistence.archive_jd_live_messages.assert_not_called()

    def test_foreign_pair_is_rejected_even_if_an_injected_adapter_returns_it(self):
        self.poll.return_value['items'] = [event(sender=7, recipient=8)]
        self.assertEqual(self.call()[1]['items'], [])
        self.persistence.can_message_peer.assert_not_called()

    def test_bad_cursor_is_rejected_before_connecting(self):
        self.assertEqual(self.call(cursor='bad')[0], 400)
        self.poll.assert_not_called()

    def test_more_than_16_different_peers_still_uses_one_bounded_archive_batch(self):
        self.poll.return_value['items'] = [event(mid=str(i), sender=i+100) for i in range(50)]
        self.assertEqual(self.call()[1]['count'], 50)
        self.assertEqual(len(self.persistence.archive_jd_live_messages.call_args.args[1]), 1)


class ProductionDispatchTests(unittest.TestCase):
    def setUp(self):
        from bbw_web import api
        self.api = api
        self.fixture = LiveApiTests()
        self.fixture.setUp()
        self.persistence = self.fixture.persistence
        self.persistence.require_identity = Mock(return_value=self.fixture.identity)
        self.persistence.rate_limit = Mock(return_value=True)
        self.store = SimpleNamespace(get=Mock(return_value=self.fixture.user), drop=Mock())
        self.addCleanup(patch.stopall)
        patch.object(api.legacy, 'STORE', self.store).start()

    def request(self, query=b''):
        from starlette.requests import Request
        scope = dict(type='http', method='GET', path='/api/im/live', query_string=query,
                     headers=[(b'cookie', b'bbw_sid=synthetic-session')], scheme='http',
                     server=('localhost', 80), client=('127.0.0.1', 1234),
                     app=SimpleNamespace(state=SimpleNamespace(
                         settings=SimpleNamespace(max_request_body_bytes=1024), persistence=self.persistence)))
        return self.api._legacy_dispatch_sync(Request(scope), b'')

    def test_live_route_uses_normal_durable_auth_and_separate_rate_bucket(self):
        response = self.request(b'uid=999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(json.loads(response.body)['items'][0]['to'], '42')
        self.persistence.require_identity.assert_called_once_with('synthetic-session')
        self.assertTrue(self.persistence.rate_limit.call_args.args[0].startswith('jd-live:'))

    def test_revoked_session_drops_runtime_before_receiving_anything(self):
        self.persistence.require_identity.return_value = None
        self.store.get.return_value = None
        response = self.request()
        self.assertEqual(response.status_code, 401)
        self.store.drop.assert_called_once_with('synthetic-session')
        self.fixture.poll.assert_not_called()

    def test_rate_limit_precedes_socket_and_archive(self):
        self.persistence.rate_limit.return_value = False
        response = self.request()
        self.assertEqual(response.status_code, 429)
        self.fixture.poll.assert_not_called()
        self.persistence.archive_jd_live_messages.assert_not_called()


if __name__ == '__main__':unittest.main()
