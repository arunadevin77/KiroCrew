"""End-to-end tests for a remote session launch: bootstrap, bridge, runtime seam.

The "guest" is a temporary HOME on this machine reached through ``sh -c``, so
the exact install/start/stop shell scripts that ``coder ssh`` would run in a
workspace run here, and the bridge process is the real
``python -m kiro_crew.remote_session.client`` the runtime spawns. The fake
engine asks for an access token the way kiro-cli does with Crew as the auth
owner, then echoes. Every process is stopped in a ``finally``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import client as client_mod

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

SRC = str(Path(remote_worker.__file__).resolve().parents[2])

ENGINE = r"""
import json, os, sys
out = sys.stdout
def send(msg):
    out.write(json.dumps(msg) + "\n"); out.flush()
send({"jsonrpc": "2.0", "id": 900, "method": "_kiro/auth/getAccessToken", "params": {}})
token = json.loads(sys.stdin.readline())["result"]["accessToken"]
send({"jsonrpc": "2.0", "method": "probe/env", "params": {
    "api_key": os.environ.get("KIRO_API_KEY", "absent"), "token_len": len(token)}})
for line in sys.stdin:
    out.write(line); out.flush()
"""


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


class LocalShell:
    """A guest reached through ``sh -c`` with its own HOME (and optionally PATH)."""

    def __init__(self, home: Path, path_prefix: str = "") -> None:
        self.home = home
        self.path = f"{path_prefix}:{os.environ['PATH']}" if path_prefix else os.environ["PATH"]

    def _env(self) -> dict:
        return {
            **os.environ,
            "HOME": str(self.home),
            "PATH": self.path,
            "KIRO_API_KEY": "must-not-leak",
        }

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        return await remote_worker.run_collect(["sh", "-c", command], stdin, self._env())

    def argv(self, command: str) -> list[str]:
        return [
            "env",
            f"HOME={self.home}",
            f"PATH={self.path}",
            "KIRO_API_KEY=must-not-leak",
            "sh",
            "-c",
            command,
        ]


@sync
async def test_install_writes_every_worker_file_in_one_round_trip(tmp_path):
    """Each ``coder ssh`` costs seconds on the start path, so install is one call."""
    shell = LocalShell(tmp_path / "guest")
    commands: list[str] = []
    real_run = shell.run

    async def counting_run(command, stdin=b""):
        commands.append(command)
        return await real_run(command, stdin)

    shell.run = counting_run
    launch = remote_worker.RemoteWorkerLaunch("sess-inst", shell, tmp_path / "secrets")
    await launch.install()
    assert len(commands) == 1
    here = Path(remote_worker.wire.__file__).resolve().parent
    guest = tmp_path / "guest" / ".kirocrew-worker"
    for name in remote_worker._WORKER_FILES:
        assert (guest / name).read_bytes() == (here / name).read_bytes()
        assert stat_mode(guest / name) == 0o600
        assert not (guest / f"{name}.tmp").exists()


@sync
async def test_a_preinstalled_worker_skips_install_and_recovers_if_its_files_are_gone(tmp_path):
    shell = LocalShell(tmp_path / "guest")
    commands: list[str] = []
    real_run = shell.run

    async def counting_run(command, stdin=b""):
        commands.append(command)
        return await real_run(command, stdin)

    shell.run = counting_run
    launch = remote_worker.RemoteWorkerLaunch("sess-pre", shell, tmp_path / "secrets")
    launch.preinstalled_digest = remote_worker.worker_digest()
    await launch.install()
    assert commands == []  # the pool's copy is trusted: no round trip
    # ...but the guest does not have it (a rebuilt home): the start fails,
    # the launch installs for real, and the one retry starts the worker.
    worker_id = launch.new_worker_id()
    port = await launch.start_worker(
        worker_id,
        [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
        remote_worker.wire.new_secret(),
    )
    assert port > 0
    assert len(commands) == 3  # failed start, install, start
    await launch.stop_worker(worker_id)


@sync
async def test_a_stale_digest_installs_as_usual(tmp_path):
    shell = LocalShell(tmp_path / "guest")
    launch = remote_worker.RemoteWorkerLaunch("sess-old", shell, tmp_path / "secrets")
    launch.preinstalled_digest = "0" * 64
    await launch.install()
    assert (tmp_path / "guest" / ".kirocrew-worker" / "worker.py").exists()


def test_install_payload_refuses_a_file_outside_the_worker_set():
    with pytest.raises(ValueError):
        remote_worker._install_payload({"../evil.py": b"x"})


def _old_guest_python_dir() -> str:
    """A bin dir holding a Python 3.9 ``python3``: guest images ship 3.9."""
    found = shutil.which("python3.9")
    if found:
        return str(Path(found).parent) if Path(found).name == "python3" else _shim(found)
    return ""


def _shim(interpreter: str) -> str:
    shim_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"py39-shim-{os.getuid()}"
    shim_dir.mkdir(mode=0o700, exist_ok=True)
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{interpreter}" "$@"\n')
    shim.chmod(0o700)
    return str(shim_dir)


def _launch(tmp_path: Path, sid: str = "sess-e2e") -> remote_worker.RemoteWorkerLaunch:
    return remote_worker.RemoteWorkerLaunch(
        sid,
        LocalShell(tmp_path / "guest"),
        tmp_path / "secrets",
        orphan_timeout=60,
        link_options={"keepalive": 0.5, "stall": 5, "lease": 20},
    )


async def _spawn_bridge(argv: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": SRC},
        limit=1024 * 1024,
    )


async def _readjson(proc) -> dict:
    return json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))


@sync
async def test_end_to_end_token_callback_and_echo(tmp_path):
    launch = _launch(tmp_path)
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    secret_file = Path(argv[argv.index("--secret-file") + 1])
    assert stat_mode(secret_file) == 0o600
    bridge = await _spawn_bridge(argv)
    try:
        # The engine's token request arrives exactly as a local engine's would.
        request = await _readjson(bridge)
        assert request["method"] == "_kiro/auth/getAccessToken"
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"accessToken": "t" * 40}}
        bridge.stdin.write(json.dumps(reply).encode() + b"\n")
        probe = await _readjson(bridge)
        assert probe["params"] == {"api_key": "absent", "token_len": 40}
        for size in (10, 1017, 4089, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            bridge.stdin.write(line)
            assert await asyncio.wait_for(bridge.stdout.readline(), 30) == line
        # The bridge consumed its secret file; the guest unlinked its copy.
        assert not secret_file.exists()
        assert not (tmp_path / "guest/.kirocrew-worker/sessions/sess-e2e.key").exists()
        bridge.stdin.close()
        assert await asyncio.wait_for(bridge.wait(), 30) == client_mod.EXIT_FINISHED
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()
        await launch.stop()


@sync
async def test_bridge_reports_lost_when_worker_is_gone(tmp_path):
    launch = _launch(tmp_path, "sess-lost")
    launch.link_options = {"keepalive": 0.2, "stall": 0.5, "lease": 2}
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    # The guest dies without warning (host reclaimed): kill the worker's whole
    # session, engine included, so nothing can answer the bridge's dials.
    port_file = tmp_path / "guest/.kirocrew-worker/sessions/sess-lost.port"
    pid = int(port_file.read_text().split()[1])
    try:
        os.killpg(pid, signal.SIGKILL)  # setsid made it a group leader (Linux)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)  # no setsid (macOS): its engine sees EOF and exits
    bridge = await _spawn_bridge(argv)
    try:
        code = await asyncio.wait_for(bridge.wait(), 30)
        assert code in (client_mod.EXIT_LOST, client_mod.EXIT_RETIRING)
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()


@pytest.mark.skipif(not _old_guest_python_dir(), reason="no python3.9 on this host")
@sync
async def test_worker_runs_on_the_oldest_guest_python(tmp_path):
    """The guest worker is stdlib-only and copied as-is, so it must run on the
    Python the guest image ships (3.9), not only on the gateway's."""
    launch = remote_worker.RemoteWorkerLaunch(
        "sess-py39",
        LocalShell(tmp_path / "guest", path_prefix=_old_guest_python_dir()),
        tmp_path / "secrets",
        orphan_timeout=60,
        link_options={"keepalive": 0.5, "stall": 5, "lease": 20},
    )
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    bridge = await _spawn_bridge(argv)
    try:
        request = await _readjson(bridge)
        assert request["method"] == "_kiro/auth/getAccessToken"
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"accessToken": "t" * 40}}
        bridge.stdin.write(json.dumps(reply).encode() + b"\n")
        assert (await _readjson(bridge))["params"]["api_key"] == "absent"
        line = json.dumps({"pad": "y" * 5000}).encode() + b"\n"
        bridge.stdin.write(line)
        assert await asyncio.wait_for(bridge.stdout.readline(), 30) == line
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()
        await launch.stop()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_secret_never_reaches_argv_or_commands(tmp_path):
    secret = b"\x11" * 32
    command = remote_worker._start_command("sess-1", ["kiro-cli", "acp"], 60)
    assert secret.hex() not in command
    assert "env -u KIRO_API_KEY kiro-cli acp" in command


def test_hostile_values_are_rejected_or_quoted():
    with pytest.raises(ValueError):
        remote_worker._start_command("x; rm -rf ~", ["kiro-cli"], 60)
    command = remote_worker._start_command("ok", ["kiro-cli", "$(touch /tmp/p)"], 60)
    assert "'$(touch /tmp/p)'" in command
    with pytest.raises(Exception):
        remote_worker.CoderShell("coder", "bad name; id")


def test_coder_dial_never_autostarts():
    shell = remote_worker.CoderShell("coder", "my-ws")
    assert shell.argv("true")[:4] == ["coder", "ssh", "--disable-autostart", "my-ws"]


def test_private_secret_dir_is_enforced(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(remote_worker.RemoteWorkerError):
        remote_worker._write_private(shared, "k", b"x")


# -- runtime seam ---------------------------------------------------------------------


_WS = {"server": {"name": "workspace", "command": "bridge", "args": [], "env": []}, "workdir": "/w"}


def _runtime(backend=ACP_BACKEND_KAS, workspace=_WS):
    return SimpleNamespace(acp_backend=backend, _process=None, _remote_workspace=workspace)


def test_remote_workspace_unmounts_every_builtin_tool():
    rt = _runtime()
    servers = [{"name": "kirocrew-core"}, {"name": "workspace"}]
    agents = [
        {"id": "a", "prompt": "p", "tools": ["read", "write", "shell", "@kirocrew-core", "@x/y"]},
        {"id": "b", "prompt": "", "tools": "*"},
        {"id": "c", "tools": ["*", "read"]},
    ]
    out = AcpRuntime._remote_workspace_agents(rt, agents, servers)
    # The whole-server core grant is narrowed to its non-local-only tools (see
    # test_remote_session_local_only_tools.py); everything else is as granted.
    core = [t for t in out[1]["tools"] if t.startswith("@kirocrew-core/")]
    assert core and "@kirocrew-core/file_send" not in core
    assert out[0]["tools"] == [*core, "@x/y", "@workspace"]
    assert out[1]["tools"] == [*core, "@workspace"]
    assert out[2]["tools"] == [*core, "@workspace"]
    assert all("/w" in a["prompt"] for a in out)
    assert agents[0]["tools"][0] == "read"  # the caller's projection is not mutated


def test_local_session_is_untouched():
    rt = _runtime(workspace=None)
    agents = [{"id": "a", "tools": ["read"]}]
    assert AcpRuntime._remote_workspace_agents(rt, agents, []) is agents
    assert AcpRuntime._remote_session_servers(rt, [{"name": "s"}]) == [{"name": "s"}]


def test_workspace_server_is_added_once():
    rt = _runtime()
    servers = AcpRuntime._remote_session_servers(rt, [{"name": "workspace"}, {"name": "core"}])
    assert [s["name"] for s in servers] == ["core", "workspace"]
    assert servers[-1]["command"] == "bridge"


def test_remote_workspace_needs_the_kas_backend():
    with pytest.raises(AcpRuntimeError, match="KAS"):
        AcpRuntime.check_remote_workspace_supported(_runtime(backend="kiro"))
    AcpRuntime.check_remote_workspace_supported(_runtime())


@sync
async def test_end_to_end_mcp_tools_over_the_tunnel(tmp_path):
    """The real bridge, as the local engine would launch it, reaching the real
    guest tool server through the worker: list tools, write, read, run, refuse."""
    launch = _launch(tmp_path, "sess-mcp")
    root = str(tmp_path / "guest" / "project")
    server = await launch.workspace_server(root)
    assert server["name"] == "workspace" and server["env"] == []
    # The dial runs in this (gateway) process behind a loopback relay: the bridge
    # is a sandboxed MCP child that cannot use the gateway's coder login.
    # The bridge only pipes to a gateway relay, which holds the link and the dial:
    # the bridge is a sandboxed MCP child that cannot use the gateway's coder login.
    assert "--pipe" in server["args"] and "ssh" not in " ".join(server["args"])
    assert launch.relay is not None and launch.relay.port > 0
    assert launch.session_id.startswith("sess-mcp-") and launch.session_id != launch.binding_id
    bridge = await _spawn_bridge([server["command"], *server["args"]])
    ident = 0

    async def call(method, params=None):
        nonlocal ident
        ident += 1
        msg = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}
        bridge.stdin.write(json.dumps(msg).encode() + b"\n")
        return await _readjson(bridge)

    async def tool(name, **arguments):
        reply = await call("tools/call", {"name": name, "arguments": arguments})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    try:
        assert (await call("initialize", {"protocolVersion": "2024-11-05"}))["result"][
            "serverInfo"
        ]["name"] == "kirocrew-workspace"
        names = {t["name"] for t in (await call("tools/list"))["result"]["tools"]}
        assert {"read_file", "write_file", "edit_file", "run_command", "grep"} <= names
        assert (await tool("write_file", path="src/a.txt", content="hello\nworld\n"))[1] is False
        assert (await tool("read_file", path="src/a.txt"))[0] == "hello\nworld\n"
        assert (await tool("edit_file", path="src/a.txt", old_str="world", new_str="crew"))[
            1
        ] is False
        assert "src/a.txt:2: crew" in (await tool("grep", pattern="cr.w"))[0]
        text, err = await tool("run_command", command="cat src/a.txt; echo key=$KIRO_API_KEY")
        assert not err and "hello\ncrew" in text and "key=\n" in text and "[exit 0]" in text
        assert (await tool("read_file", path="../../etc/passwd"))[1] is True
        assert (await tool("write_file", path="/etc/x", content="no"))[1] is True
        os.symlink("/etc", os.path.join(root, "out"))
        assert (await tool("read_file", path="out/hosts"))[1] is True
    finally:
        bridge.stdin.close()
        try:
            await asyncio.wait_for(bridge.wait(), 20)
        except asyncio.TimeoutError:
            bridge.kill()
            await bridge.wait()
        await launch.stop()


async def _mcp_initialize(bridge) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    bridge.stdin.write(json.dumps(msg).encode() + b"\n")
    return await asyncio.wait_for(_readjson(bridge), 30)


async def _end(bridge) -> None:
    bridge.stdin.close()
    try:
        await asyncio.wait_for(bridge.wait(), 20)
    except asyncio.TimeoutError:
        bridge.kill()
        await bridge.wait()


@sync
async def test_the_server_command_works_every_time_it_is_launched(tmp_path):
    # Regression: the engine launches its stdio MCP server more than once per
    # runtime (a tools probe, a reload). The old bridge consumed a one-use secret
    # on its first launch, so every later launch died at once and the agent saw
    # "workspace: Connection closed". Each launch must get a working stream, at
    # the same time and one after another, each on its own guest worker.
    launch = _launch(tmp_path, "sess-relaunch")
    server = await launch.workspace_server(str(tmp_path / "guest" / "p"))
    argv = [server["command"], *server["args"]]
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        spare_id = (await launch.relay._spare)[0]
        first, second = await _spawn_bridge(argv), await _spawn_bridge(argv)
        for bridge in (first, second):
            assert (await _mcp_initialize(bridge))["result"]["serverInfo"][
                "name"
            ] == "kirocrew-workspace"
        # Two connected workers plus one pre-started for the next launch, and the
        # first launch used the worker started ahead of it.
        await launch.relay._spare
        assert len(list(sessions.glob("*.port"))) == 3
        assert "gateway attached" in (sessions / f"{spare_id}.log").read_text()
        await _end(first)
        await _end(second)
        third = await _spawn_bridge(argv)
        assert (await _mcp_initialize(third))["result"]["serverInfo"][
            "name"
        ] == "kirocrew-workspace"
        await _end(third)
    finally:
        await launch.relay.close()
    # A finished connection stops its own worker, and the token goes with the relay.
    for _ in range(50):
        if not list(sessions.glob("*.port")):
            break
        await asyncio.sleep(0.1)
    assert not list(sessions.glob("*.port"))
    assert not Path(server["args"][-1]).exists()


@sync
async def test_the_relay_refuses_a_connection_without_the_token(tmp_path):
    launch = _launch(tmp_path, "sess-token")
    await launch.workspace_server(str(tmp_path / "guest" / "p"))
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        await launch.relay._spare
        before = sorted(sessions.glob("*.port"))
        reader, writer = await asyncio.open_connection("127.0.0.1", launch.relay.port)
        writer.write(b"not-the-token\n")
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
        # Nothing was started for it, and the pre-started spare was not spent.
        assert sorted(sessions.glob("*.port")) == before and len(before) == 1
    finally:
        await launch.relay.close()


@sync
async def test_relay_is_loopback_one_at_a_time_and_logs_dial_failure(caplog):
    import sys

    from kiro_crew.remote_session.relay import DialRelay

    relay = DialRelay(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('auth failed: not signed in'); sys.exit(3)",
        ],
        name="t",
        min_dial_gap=0.0,
    )
    port = await relay.start()
    try:
        assert relay._server.sockets[0].getsockname()[0] == "127.0.0.1"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
        for _ in range(50):
            if "auth failed: not signed in" in caplog.text:
                break
            await asyncio.sleep(0.05)
        assert "dial exited 3: auth failed: not signed in" in caplog.text
    finally:
        await relay.close()


@sync
async def test_git_proxy_runs_in_the_guest_and_answers_through_the_gateway(tmp_path):
    """Install, worker, real RemoteLink, channel: the whole path, upstream faked."""
    import urllib.error
    import urllib.request

    from test_remote_git_proxy import HttpBackendUpstream

    launch = _launch(tmp_path, "sess-git")
    await launch.install()
    upstream = HttpBackendUpstream(tmp_path, status=404)

    async def credential(host):
        return "Basic eC1hY2Nlc3MtdG9rZW46dA=="

    try:
        await launch.start_git_proxy(
            "dashboard:chat-git",
            credential_source=credential,
            egress_permitted=lambda key, host: True,
            http_session=upstream,
        )
        port_file = tmp_path / "guest" / ".kirocrew-worker" / "git-proxy.port"
        for _ in range(200):
            if port_file.exists():
                break
            await asyncio.sleep(0.05)
        port = int(port_file.read_text().strip())
        url = f"http://127.0.0.1:{port}/github.com/acme/w/info/refs?service=git-upload-pack"

        def fetch() -> int:
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        assert await asyncio.wait_for(asyncio.to_thread(fetch), 60) == 404
        assert upstream.calls[0]["url"] == (
            "https://github.com/acme/w/info/refs?service=git-upload-pack"
        )
    finally:
        await launch.stop_git_proxy()
    assert launch.git_proxy is None
    # The proxy is gone with its link: its port file goes with it.
    for _ in range(100):
        if not port_file.exists():
            break
        await asyncio.sleep(0.05)
    assert not port_file.exists()


@sync
async def test_an_older_relay_closing_does_not_stop_the_live_git_proxy(tmp_path):
    """A start can make more than one relay; only the current one owns the proxy.

    Seen live: the first relay of a start idled out 16 minutes in and stopped the
    proxy while the session was still working through the second relay.
    """
    launch = _launch(tmp_path, "sess-relays")
    await launch.install()
    stops: list[str] = []

    async def stop() -> None:
        stops.append("stop")

    launch.stop_git_proxy = stop  # observe, do not reach the guest
    first = await launch.workspace_server(str(tmp_path / "guest"))
    first_relay = launch.relay
    await launch.workspace_server(str(tmp_path / "guest"))
    second_relay = launch.relay
    try:
        assert first and first_relay is not second_relay
        await first_relay.close()
        assert stops == []  # the old relay is gone; the proxy stays
        await second_relay.close()
        assert stops == ["stop"]
    finally:
        await first_relay.close()
        await second_relay.close()
