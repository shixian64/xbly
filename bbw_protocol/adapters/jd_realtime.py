"""Bounded server-side JD receive-only connections; never expose provider tokens."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
import uuid
from collections import deque
from urllib.parse import quote, urlsplit, urlunsplit


# One connection per active Web runtime, with process-wide memory/thread bounds.
_SLOTS = threading.BoundedSemaphore(32)
_MESSAGE_TYPES = frozenset({'TEXT', 'IMAGE', 'AUDIO', 'VOICE', 'VIDEO', 'FILE'})


def websocket_base(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ''
    if host == 'test.banghua.xin':
        host = 'testchat.banghua.xin'
    if parsed.port:
        host += f':{parsed.port}'
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise ValueError('invalid JD base URL')
    return urlunsplit(('wss' if parsed.scheme == 'https' else 'ws', host, parsed.path.rstrip('/'), '', ''))


def websocket_headers(connect, token: str) -> dict:
    # websockets 13's legacy connect accepts **kwargs and only rejects an
    # unknown additional_headers during __aenter__, not at construction.
    # Select the header keyword before opening; never retry after a write.
    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        parameters = {}
    key = 'extra_headers' if 'extra_headers' in parameters else 'additional_headers'
    return {key: {'Authorization': f'Bearer {token}'}}


class JdRealtimeReceiver:
    """A bounded replay buffer, activated by authenticated browser polling.

    Only C2C messages involving the bound account enter the buffer. No read
    receipts, sends, credentials, control frames or raw exception text leave it.
    Idle browsers relinquish sockets; logout closes and erases the buffer.
    """

    MAX_FRAME_BYTES = 256 * 1024
    MAX_BUFFER_BYTES = 2 * 1024 * 1024
    MAX_EVENTS = 512
    IDLE_SECONDS = 90.0

    def __init__(self, *, uid: str, token: str, base_url: str, device_id: str = ''):
        self.uid = str(uid)
        self._token = str(token)
        self._base = websocket_base(base_url)
        self._epoch = uuid.uuid4().hex
        # Separate receive and short-send devices so opening a send cannot
        # replace this socket (or the user's native Android connection).
        seed = f'{self.uid}:{device_id}:{self._epoch}'
        self._device = 'web-live-' + hashlib.sha256(seed.encode()).hexdigest()[:24]
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None
        self._closed = False
        self._connected = False
        self._last_poll = time.monotonic()
        self._events = deque()
        self._bytes = 0
        self._sequence = 0
        self._interrupted = False

    def matches(self, uid: str, token: str) -> bool:
        return not self._closed and self.uid == str(uid) and self._token == str(token)

    def _accept(self, raw: str | bytes) -> None:
        if not isinstance(raw, (str, bytes)) or len(raw) > self.MAX_FRAME_BYTES:
            return
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        # A documented proxy may wrap one native ChatMessage in data. Do not
        # recursively traverse arbitrary payloads, groups, ACKs or credentials.
        if isinstance(value, dict) and isinstance(value.get('data'), dict) and not value.get('fromUserId'):
            value = value['data']
        if not isinstance(value, dict) or str(value.get('type') or '').upper() not in _MESSAGE_TYPES:
            return
        sender = str(value.get('fromUserId') or '')
        recipient = str(value.get('toUserId') or '')
        mid = str(value.get('messageId') or '')
        stamp = value.get('timestamp')
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not 0 < stamp < 10**14):
            return
        if (not sender.isascii() or not sender.isdecimal() or not recipient.isascii()
                or not recipient.isdecimal() or self.uid not in {sender, recipient}
                or len(sender) > 128 or len(recipient) > 128 or sender == '0' or recipient == '0'
                or sender == recipient or not mid or len(mid) > 512 or any(ord(c) < 32 for c in mid)
                or value.get('roomId') or value.get('groupId')):
            return
        # Allowlist native DTO fields. In particular never return token-bearing
        # extension fields, debug data or an arbitrary server envelope.
        item = {key: value[key] for key in (
            'messageId', 'fromUserId', 'toUserId', 'content', 'type', 'timestamp',
            'status', 'sequence', 'clientMessageId',
        ) if key in value}
        encoded = json.dumps(item, ensure_ascii=False, separators=(',', ':'))
        try:
            size = len(encoded.encode('utf-8'))
        except UnicodeEncodeError:
            return
        if size > self.MAX_FRAME_BYTES:
            return
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            self._events.append((self._sequence, encoded, size))
            self._bytes += size
            while len(self._events) > self.MAX_EVENTS or self._bytes > self.MAX_BUFFER_BYTES:
                self._bytes -= self._events.popleft()[2]

    def poll(self, cursor: str = '', *, limit: int = 50) -> dict:
        epoch, separator, raw_sequence = str(cursor or '').partition(':')
        if cursor and (len(cursor) > 100 or not separator or len(raw_sequence) > 18 or not raw_sequence.isascii() or not raw_sequence.isdecimal()):
            raise ValueError('invalid live cursor')
        with self._lock:
            if self._closed:
                return {'items': [], 'cursor': '', 'connected': False, 'gap': True}
            self._last_poll = time.monotonic()
            self._start_locked()
            after = int(raw_sequence) if cursor and epoch == self._epoch else 0
            reset = bool(cursor and (epoch != self._epoch or after > self._sequence))
            if after > self._sequence:
                after = 0
            gap = reset or bool(self._events and after < self._events[0][0] - 1)
            events = [entry for entry in self._events if entry[0] > after][:max(1, min(int(limit), 50))]
            last = events[-1][0] if events else after
            return {
                'items': [json.loads(entry[1]) for entry in events],
                'cursor': f'{self._epoch}:{last}', 'connected': self._connected,
                'gap': gap or self._interrupted, 'has_more': bool(self._events and self._events[-1][0] > last),
            }

    def _start_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not _SLOTS.acquire(blocking=False):
            return
        if self._thread is not None:
            # An idle receiver can restart, but messages while disconnected
            # aren't replayed by this transport. Never imply complete history.
            self._interrupted = True
        self._thread = threading.Thread(target=self._worker, name='jd-live-receiver', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            _SLOTS.release()
            self._thread = None
            raise

    def _worker(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception:
            with self._lock:
                self._interrupted = True
        finally:
            with self._lock:
                self._connected = False
                self._loop = self._task = None
            _SLOTS.release()

    def _active(self) -> bool:
        return not self._stop.is_set() and time.monotonic() - self._last_poll < self.IDLE_SECONDS

    async def _run(self) -> None:
        import websockets
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        delay = 1.0
        while self._active():
            try:
                url = self._base + '/ws/chat?token=' + quote(self._token, safe='') + '&deviceId=' + quote(self._device, safe='')
                options = dict(open_timeout=4, close_timeout=1, ping_interval=25, ping_timeout=10,
                               max_size=self.MAX_FRAME_BYTES, max_queue=16)
                connection = websockets.connect(url, **websocket_headers(websockets.connect, self._token), **options)
                async with connection as ws:
                    with self._lock:
                        self._connected = True
                    delay = 1.0
                    heartbeat_at = time.monotonic() + 240
                    while self._active():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw is not None:
                            self._accept(raw)
                        if time.monotonic() >= heartbeat_at:
                            # APK-compatible application heartbeat, not a message
                            # to another user and not a read acknowledgement.
                            await ws.send(json.dumps({'messageId': str(uuid.uuid4()), 'fromUserId': int(self.uid),
                                                      'toUserId': int(self.uid), 'content': '', 'type': 'HEARTBEAT',
                                                      'timestamp': int(time.time() * 1000)}))
                            heartbeat_at = time.monotonic() + 240
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log/return exception text: WebSocket URLs contain tokens.
                with self._lock:
                    self._interrupted = True
            finally:
                with self._lock:
                    self._connected = False
            # Back off without occupying the request thread or delaying logout.
            end = time.monotonic() + delay
            while self._active() and time.monotonic() < end:
                await asyncio.sleep(min(.25, max(0, end - time.monotonic())))
            delay = min(delay * 2, 30)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
            self._connected = False
            self._events.clear()
            self._bytes = 0
            self._token = ''
            loop, task, thread = self._loop, self._task, self._thread
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread():
            # Logout/shutdown must not wait two seconds per active receiver.
            # Cancellation continues asynchronously if the close handshake is slow.
            thread.join(timeout=.25)
