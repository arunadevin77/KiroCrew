"""Tests for the standalone guest-side remote MCP relay (`remote_mcp_relay`).

The relay is a leaf artifact copied into a Session Environments guest, so these
tests drive it the way a guest does: as a child process talking to a loopback
listener this test owns (`port=0`, never a live gateway socket) with a synthetic
capability file under `tmp_path`. Every spawned child is killed in a `finally`,
and the argument/bounds paths run in-process so the number of children stays
small.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import remote_mcp_relay

_RELAY_PATH = str(Path(remote_mcp_relay.__file__).resolve())


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        await platform_compat.kill_process_tree_async(
            process.pid,
            platform_compat.SIGKILL,
        )
    except (OSError, ProcessLookupError):
        pass
    await asyncio.wait_for(process.wait(), timeout=10.0)


async def _spawn_relay(argv: list[str], cwd: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(
            platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
        ),
    )


def _write_capability(tmp_path: Path, token: str) -> Path:
    cap_file = tmp_path / "capability"
    cap_file.write_text(token + "\n", encoding="utf-8")
    platform_compat.restrict_to_owner(cap_file)
    return cap_file


@pytest.mark.asyncio
async def test_relay_authenticates_from_file_and_copies_literal_bytes(tmp_path: Path) -> None:
    """The auth frame carries the file's token and payload bytes pass through unchanged."""
    bearer = "relay-secret-canary"
    observed_auth: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async def echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            auth = json.loads(await reader.readline())
            if not observed_auth.done():
                observed_auth.set_result(auth)
            while line := await reader.readline():
                writer.write(line)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    cap_file = _write_capability(tmp_path, bearer)
    argv = [sys.executable, _RELAY_PATH, "--port", str(port), "--cap-file", str(cap_file)]
    process = await _spawn_relay(argv, tmp_path)
    request = b'{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n'
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input=request), timeout=10.0)

        assert process.returncode == 0
        assert stdout == request
        assert stderr == b""
        assert await asyncio.wait_for(observed_auth, timeout=10.0) == {
            "version": 1,
            "token": bearer,
        }
        assert bearer not in " ".join(argv)
    finally:
        await _stop_process(process)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_relay_forwards_before_its_stdin_reaches_eof(tmp_path: Path) -> None:
    """Forwarding is a byte stream, not a request/response batch closed by EOF."""

    async def echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readline()
            while line := await reader.readline():
                writer.write(line)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    cap_file = _write_capability(tmp_path, "streaming-relay-canary")
    process = await _spawn_relay(
        [sys.executable, _RELAY_PATH, "--port", str(port), "--cap-file", str(cap_file)],
        tmp_path,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    request = b'{"jsonrpc":"2.0","id":10,"method":"tools/list"}\n'
    try:
        process.stdin.write(request)
        await process.stdin.drain()

        assert await asyncio.wait_for(process.stdout.readline(), timeout=5.0) == request
        assert process.returncode is None
    finally:
        process.stdin.close()
        await _stop_process(process)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unsupported_relay_answers_jsonrpc_error_without_a_socket(tmp_path: Path) -> None:
    """A server the host cannot reach fails as a protocol error, not a broken pipe."""
    argv = [sys.executable, _RELAY_PATH, "--unsupported-code", "remote_mcp_http_unavailable"]
    process = await _spawn_relay(argv, tmp_path)
    request = (
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}).encode()
        + b"\n"
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input=request), timeout=10.0)
        response = json.loads(stdout)

        assert process.returncode == 0
        assert stderr == b""
        assert response["id"] == 3
        assert response["error"]["code"] == remote_mcp_relay._JSONRPC_TRANSPORT_UNAVAILABLE
        assert response["error"]["data"]["code"] == "remote_mcp_http_unavailable"
    finally:
        await _stop_process(process)


def test_capability_file_input_is_bounded(tmp_path: Path) -> None:
    """Oversized, empty and over-long-token capability files are refused, not truncated."""
    good = tmp_path / "good"
    good.write_text("  token-with-surrounding-space\n", encoding="utf-8")
    assert remote_mcp_relay._read_token(good) == "token-with-surrounding-space"

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"a" * (remote_mcp_relay._TOKEN_FILE_LIMIT + 1))
    with pytest.raises(ValueError):
        remote_mcp_relay._read_token(oversized)

    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        remote_mcp_relay._read_token(empty)

    long_token = tmp_path / "long-token"
    long_token.write_bytes(b"b" * (remote_mcp_relay._TOKEN_TEXT_LIMIT + 1))
    with pytest.raises(ValueError):
        remote_mcp_relay._read_token(long_token)


def test_unsupported_response_is_only_minted_for_requests_carrying_an_id() -> None:
    """A notification gets no reply; a request gets the transport-unavailable error."""
    assert remote_mcp_relay._unsupported_response({"method": "notify"}, "code") is None
    assert remote_mcp_relay._unsupported_response(["not", "an", "object"], "code") is None

    response = remote_mcp_relay._unsupported_response({"id": 7, "method": "tools/list"}, "code-x")

    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": remote_mcp_relay._JSONRPC_TRANSPORT_UNAVAILABLE,
            "message": "Remote MCP transport unavailable",
            "data": {"code": "code-x"},
        },
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--port", "1234"],
        ["--cap-file", "cap"],
        ["--unsupported-code", "x", "--port", "1234"],
        ["--port", "0", "--cap-file", "cap"],
        ["--port", "65536", "--cap-file", "cap"],
    ],
)
def test_argument_combinations_that_cannot_relay_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        remote_mcp_relay._parse_args(argv)


def test_proxy_failure_exits_nonzero_and_echoes_no_secret(tmp_path: Path, capfd) -> None:
    """A failed connection reports an exit code, never the token or an exception."""
    bearer = "unreachable-port-canary"
    cap_file = _write_capability(tmp_path, bearer)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    finally:
        listener.close()

    assert remote_mcp_relay._run_proxy(port, cap_file) == 1

    captured = capfd.readouterr()
    assert captured.err == ""
    assert bearer not in captured.err
    assert bearer not in captured.out


def test_oversized_capability_file_fails_before_any_connection(
    tmp_path: Path, capfd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounds are checked before the socket, so a bad file cannot reach the listener."""
    cap_file = tmp_path / "capability"
    cap_file.write_bytes(b"c" * (remote_mcp_relay._TOKEN_FILE_LIMIT + 1))

    def _refuse(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("connection attempted despite an oversized capability file")

    monkeypatch.setattr(socket, "create_connection", _refuse)

    assert remote_mcp_relay._run_proxy(65535, cap_file) == 1

    assert capfd.readouterr().err == ""


def test_unsupported_line_limit_bounds_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an oversized line without first consuming its unbounded remainder."""
    stream = io.TextIOWrapper(io.BytesIO(b"x" * 64))
    monkeypatch.setattr(remote_mcp_relay, "_JSON_LINE_LIMIT", 16)
    monkeypatch.setattr(remote_mcp_relay.sys, "stdin", stream)
    try:
        assert remote_mcp_relay._run_unsupported("unavailable") == 1
        assert stream.buffer.tell() == 17
    finally:
        stream.close()
