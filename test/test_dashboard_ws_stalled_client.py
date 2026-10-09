"""A dashboard tab that stays connected but stops reading (D47, D48).

One normal tab and one stalled tab (a raw socket with a 4 KiB receive buffer that
completes the upgrade and never reads again) connect to the real ``/api/ws``
handler through aiohttp's test server, with the real token auth and the real
``WebSocketHub`` fan-out. The config dir, token secret and nonce store are pinned
under ``tmp_path`` as in ``test_chat_send_client_meta_survival``.

The stalled tab's frames stay in its transport's write buffer, and a send that has
to wait for that buffer to drain never completes. The hub must drop such a tab and
abort its connection, without touching a tab that reads.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import socket
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth, websocket_hub
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import _websocket_for
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG

FRAME = 64 * 1024


def _level_moves_on_this_loop() -> bool:
    """Whether the running loop's transports show a reader's progress in their
    write-buffer level. Not on the proactor loop Windows runs: there the level is
    the overlapped send in flight plus the bytes queued behind it, and it can read
    0 for a peer that stopped reading once the kernel holds every byte."""
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is None or not isinstance(asyncio.get_running_loop(), proactor)


@pytest.fixture
def ws_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"stalled-tab-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    return _make_state(tmp_path)


async def _stalled_socket(host: str, port: int, token: str, origin: str) -> socket.socket:
    """Open ``/api/ws`` on a raw socket that never reads after the upgrade."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await loop.sock_connect(sock, (host, port))
    # RFC 6455 asks for a 16-byte nonce; any fixed one is valid for this socket.
    key = base64.b64encode(b"stalled-tab-key!").decode()
    request = (
        f"GET /api/ws?token={token} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
    )
    await loop.sock_sendall(sock, request.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout=5)
        assert chunk, "the server closed the upgrade"
        head += chunk
    assert head.startswith(b"HTTP/1.1 101"), head[:80]
    return sock


async def _harness(state, body):
    from kiro_crew.dashboard.ws import api_ws

    app = _make_app(state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        normal = await client.ws_connect("/api/ws", params={"token": token})
        received: dict[int, float] = {}

        async def _read() -> None:
            async for msg in normal:
                data = json.loads(msg.data)
                if data.get("type") == "stall_probe":
                    received[data["data"]["n"]] = time.monotonic()

        reader = asyncio.ensure_future(_read())
        sock = await _stalled_socket(client.host, client.port, token, origin)
        async with asyncio.timeout(5):
            while len(state._ws_clients) < 2:
                await asyncio.sleep(0.01)
        local = sock.getsockname()
        stalled = next(
            ws
            for ws in state._ws_clients
            if ws._req.transport.get_extra_info("peername")[:2] == local[:2]
        )
        reading = next(ws for ws in state._ws_clients if ws is not stalled)
        try:
            await body(state, stalled, reading, received)
        finally:
            reader.cancel()
            await normal.close()
            sock.close()


async def _broadcast(state, start: int, count: int, stop=None) -> tuple[int, int]:
    """Broadcast up to ``count`` frames; return (next frame number, peak buffer)."""
    pad = "x" * FRAME
    peak = 0
    n = start
    for n in range(start, start + count):
        state.broadcast_ws("stall_probe", {"n": n, "pad": pad})
        await asyncio.sleep(0)
        if stop is not None:
            peak = max(peak, stop())
            if stop() < 0:
                return n + 1, peak
    return n + 1, peak


async def _wait_received(received: dict[int, float], n: int, ceiling: float = 10.0) -> None:
    """Wait until the reading tab has ``n`` frames; the ceiling only bounds a hang."""
    try:
        async with asyncio.timeout(ceiling):
            while len(received) < n:
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"the reading tab got {len(received)} of {n} frames within {ceiling:.0f} s")


async def _wait_until(done, what: str, ceiling: float = 10.0) -> None:
    """Poll ``done`` until it holds; the ceiling only bounds a hang."""
    try:
        async with asyncio.timeout(ceiling):
            while not done():
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"{what} did not happen within {ceiling:.0f} s")


class _Transport:
    """The write side of a fake tab: unsent bytes, and whether it was aborted."""

    def __init__(self, buffered: int) -> None:
        self.buffered = buffered
        self.aborted = False

    def get_write_buffer_size(self) -> int:
        return self.buffered

    def abort(self) -> None:
        self.aborted = True
        self.buffered = 0


def _patch_capable_tab(buffered: int) -> MagicMock:
    ws = MagicMock()
    ws.closed = False
    ws.send_str = AsyncMock()
    ws.get = MagicMock(side_effect=lambda key, default=None: key == SLOT_PATCH_WS_FLAG)
    ws._req.transport = _Transport(buffered)
    return ws


@pytest.mark.asyncio
async def test_a_stalled_log_subscriber_is_dropped_by_the_log_stream(ws_state):
    """The log stream sends outside the hub's fan-out, so it applies the same limit."""

    async def body(state, stalled, reading, received):
        transport = stalled._req.transport
        state._ws_log_subscribers.add(stalled)
        handler = updates._RingLogHandler(collections.deque(maxlen=8))
        handler.set_state(state)
        record = logging.LogRecord("stall", logging.INFO, __file__, 0, "x" * FRAME, None, None)
        peak = 0
        for _ in range(400):
            if stalled not in state._ws_clients:
                break
            handler.emit(record)
            for _ in range(3):
                await asyncio.sleep(0)
            peak = max(peak, transport.get_write_buffer_size())
        assert stalled not in state._ws_clients, (
            f"a log subscriber that reads nothing is still registered after 400 log frames of "
            f"{FRAME // 1024} KiB, with {transport.get_write_buffer_size()} bytes buffered for it"
        )
        assert stalled not in state._ws_log_subscribers
        assert peak <= websocket_hub.WS_MAX_BUFFERED_BYTES + 2 * FRAME, peak
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        assert reading in state._ws_clients

    await _harness(ws_state, body)


@pytest.mark.asyncio
async def test_a_stalled_tab_does_not_delay_a_reading_tab(ws_state):
    """Control: one fan-out task per send, so a stalled tab holds only its own sends."""

    async def body(state, stalled, reading, received):
        # How many frames fill the stalled tab's socket buffers depends on the
        # host, so send until writing to it is paused rather than a fixed count.
        n = 0
        try:
            async with asyncio.timeout(20):
                while not stalled._req.protocol._paused and n < 2000:
                    n, _ = await _broadcast(state, n, 1)
        except TimeoutError:
            pass
        assert stalled._req.protocol._paused, (
            f"precondition: writing to the stalled tab never paused after {n} frames of "
            f"{FRAME // 1024} KiB ({stalled._req.transport.get_write_buffer_size()} bytes buffered)"
        )
        await _wait_received(received, n)
        # The reading tab got every frame while writing to the stalled tab is
        # paused, so no frame waited on the stalled tab's buffer.
        assert stalled in state._ws_clients, "a tab under the limit must stay registered"
        # Under the selector loops the stalled tab's frames show in its level. The
        # proactor loop's level can read 0 by now, once the kernel holds them all.
        if _level_moves_on_this_loop():
            assert stalled._req.transport.get_write_buffer_size() > 0
        assert reading in state._ws_clients

    await _harness(ws_state, body)


@pytest.mark.asyncio
async def test_a_stalled_tab_is_dropped_and_its_buffer_released(ws_state):
    """D48: the server must not keep queueing frames for a tab that stopped reading."""

    async def body(state, stalled, reading, received):
        transport = stalled._req.transport
        baseline_tasks = len(state._background_tasks)
        n, _ = await _broadcast(state, 0, 40)
        await _wait_received(received, n)
        under_limit = transport.get_write_buffer_size()
        # Control: a tab whose buffer is still under the limit is left alone.
        assert stalled in state._ws_clients, f"dropped at {under_limit} bytes buffered"
        assert not transport.is_closing()

        def buffered() -> int:
            return -1 if stalled not in state._ws_clients else transport.get_write_buffer_size()

        n, peak = await _broadcast(state, n, 400, stop=buffered)
        assert stalled not in state._ws_clients, (
            f"a tab that reads nothing is still registered after {n} frames of "
            f"{FRAME // 1024} KiB, with {transport.get_write_buffer_size()} bytes buffered "
            f"for it (after 40 frames: {under_limit} bytes)"
        )
        assert stalled not in state._owner_ws_clients
        assert peak <= websocket_hub.WS_MAX_BUFFERED_BYTES + 2 * FRAME, peak
        # abort() discards the buffer and ends every send waiting on its drain.
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        await _wait_until(
            lambda: len(state._background_tasks) <= baseline_tasks,
            "the dropped tab's waiting send tasks finishing",
        )
        # Control: the reading tab is untouched and got every frame.
        await _wait_received(received, n)
        assert reading in state._ws_clients

    await _harness(ws_state, body)


@pytest.mark.asyncio
async def test_deliver_ws_owners_returns_while_an_owner_tab_is_stalled(ws_state, monkeypatch):
    """D47: one owner tab that stopped reading must not hold the awaited delivery."""
    monkeypatch.setattr(websocket_hub, "WS_OWNER_SEND_TIMEOUT_S", 1.0)

    async def body(state, stalled, reading, received):
        transport = stalled._req.transport
        n = 0
        async with asyncio.timeout(10):
            # Past the kernel's buffers, so the transport pauses writing to it,
            # and under the drop limit, so only the send timeout can end it.
            while transport.get_write_buffer_size() <= 256 * 1024:
                n, _ = await _broadcast(state, n, 1)
        await _wait_until(
            lambda: stalled._req.protocol._paused, "the transport pausing writes to the stalled tab"
        )
        assert stalled in state._owner_ws_clients, "precondition: the stalled tab is an owner"
        assert stalled._req.protocol._paused, "precondition: writing to it is paused"
        pad = "x" * FRAME
        # The writer waits for a drain only on the send that crosses its byte
        # limit, so deliver until one does; on a paused socket that send never ends.
        for attempt in range(20):
            frame = 1000 + attempt
            try:
                delivered = await asyncio.wait_for(
                    state.deliver_ws_owners("stall_probe", {"n": frame, "pad": pad}), timeout=8
                )
            except TimeoutError:
                pytest.fail(
                    f"deliver_ws_owners call {attempt + 1} did not return within 8 s while one "
                    f"owner tab stopped reading (buffered={transport.get_write_buffer_size()} "
                    f"bytes); the reading tab had that frame: {frame in received}"
                )
            await _wait_received(received, n + attempt + 1)
            assert frame in received, "the reading owner tab must still get the frame"
            if stalled not in state._owner_ws_clients:
                break
        assert stalled not in state._owner_ws_clients, "the stalled owner tab was never dropped"
        assert stalled not in state._ws_clients
        assert delivered == 1, "a timed-out tab counts as not delivered"
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        assert reading in state._owner_ws_clients

    await _harness(ws_state, body)
