"""Fargate session environments, without AWS.

The guest bootstrap runs for real, as a local child process: the "SSM forward"
in these tests is a plain loopback connection to it, which is exactly what the
forward hands the gateway. ECS is faked at the ``cloud.aws`` chokepoint, so
every ECS call the provider makes is visible and none reaches AWS.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.fargate_session import bootstrap
from kiro_crew.fargate_session import launch as fargate_launch
from kiro_crew.fargate_session import tasks
from kiro_crew.fargate_session.shell import FargateShell, FargateWorkerLaunch
from kiro_crew.remote_session import wire
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.session_environment import SessionEnvironmentUnavailable

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

BOOTSTRAP = (
    Path(bootstrap.__file__).resolve().parents[1] / "remote_session" / "fargate_bootstrap.py"
)
SECRET = b"s" * 32


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _start_bootstrap(tmp_path: Path, *, compressed: bool = False):
    """The real bootstrap as a child, listening where a task's would."""
    home = tmp_path / "guest"
    home.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = {
        # HOME and PATH as a pair: python3 resolves to this interpreter, no shim.
        "HOME": str(home),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin",
        "KIROCREW_BOOTSTRAP_PORT": str(port),
        "KIROCREW_WORKDIR": str(home / "work"),
    }
    argv = bootstrap.container_command() if compressed else ["python3", str(BOOTSTRAP)]
    argv = [sys.executable, *argv[1:]]
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(home),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    for _ in range(200):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        await asyncio.sleep(0.05)

    async def connect():
        return await asyncio.open_connection("127.0.0.1", port, limit=64 * 1024 * 1024)

    return process, connect, home


async def _stop(process) -> None:
    if process.returncode is None:
        process.kill()
    await asyncio.wait_for(process.wait(), 10)


# ── The bootstrap protocol, against a real bootstrap ─────────────────────


@sync
async def test_the_compressed_container_command_runs_the_bootstrap(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path, compressed=True)
    try:
        await bootstrap.enroll(connect, SECRET)
        code, out, _ = await bootstrap.run(connect, SECRET, "echo hi; pwd")
        assert code == 0 and out.splitlines()[0] == b"hi"
        assert (home / "work").is_dir()  # the profile's workdir exists before any session
    finally:
        await _stop(process)


@sync
async def test_enrolment_happens_once_and_nothing_runs_without_the_secret(tmp_path):
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:
        # Before enrolment no request is accepted at all.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, SECRET, "true")
        await bootstrap.enroll(connect, SECRET)
        # A second principal cannot take over the enrolled bootstrap.
        with pytest.raises(bootstrap.BootstrapError, match="already enrolled"):
            await bootstrap.enroll(connect, b"x" * 32)
        # And cannot run anything with a secret it does not hold.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, b"x" * 32, "touch pwned")
        assert not (tmp_path / "guest" / "pwned").exists()
        code, out, _ = await bootstrap.run(connect, SECRET, "cat", stdin=b"payload")
        assert (code, out) == (0, b"payload")
        code, _out, err = await bootstrap.run(connect, SECRET, "echo boom >&2; exit 3")
        assert code == 3 and "boom" in err
    finally:
        await _stop(process)


@sync
async def test_a_reply_corrupted_in_the_tunnel_is_never_acted_on(tmp_path):
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:
        await bootstrap.enroll(connect, SECRET)
        flips = {"left": 1}

        async def corrupting():
            reader, writer = await connect()
            original = reader.read

            async def read(n=-1):
                data = await original(n)
                # Flip one byte of the RES payload once, as a bad tunnel might.
                if flips["left"] and data.startswith(b"RES "):
                    flips["left"] -= 1
                    data = data[:6] + bytes([data[6] ^ 1]) + data[7:]
                return data

            reader.read = read
            return reader, writer

        code, out, _ = await bootstrap.run(corrupting, SECRET, "echo clean")
        assert (code, out) == (0, b"clean\n") and flips["left"] == 0  # retried, verified
    finally:
        await _stop(process)


@sync
async def test_a_cr_terminated_line_is_still_a_line(tmp_path):
    """The SSM forward has turned a write's final LF into CR; that must not wedge."""
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:
        reader, writer = await connect()
        lines = bootstrap._Lines(reader)
        hello = await lines.line()
        writer.write(b"ENROLL " + SECRET.hex().encode() + b"\r")
        await writer.drain()
        answer = await lines.line()
        writer.close()
        assert hello.startswith(b"KCB1 ") and answer.startswith(b"ENROLLED ")
    finally:
        await _stop(process)


# ── Guest workers through the bootstrap: the real wire, end to end ────────


ENGINE = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"


@sync
async def test_a_guest_worker_is_installed_started_and_dialled_through_the_bootstrap(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path)
    link = None
    worker_id = ""
    try:
        await bootstrap.enroll(connect, SECRET)
        launch = FargateWorkerLaunch("fg-test", FargateShell(connect, SECRET), tmp_path / "secrets")
        await launch.install()
        assert (home / ".kirocrew-worker" / "worker.py").exists()
        worker_id = launch.new_worker_id()
        secret = wire.new_secret()
        port = await launch.start_worker(worker_id, ["python3", "-u", "-c", ENGINE], secret)
        link = RemoteLink(
            worker_id,
            secret,
            launch.dial(port),
            keepalive_seconds=1,
            stall_seconds=5,
            lease_seconds=20,
        )
        link.start()
        await link.wait_connected(20)
        for size in (10, 1017, 4089, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            await link.write(line)
            assert await asyncio.wait_for(link.stdout.readline(), 30) == line
    finally:
        if link is not None:
            await link.aclose()
        if worker_id:
            with contextlib.suppress(Exception):
                await launch.stop_worker(worker_id)
        await _stop(process)


# ── ECS calls, faked at the aws chokepoint ───────────────────────────────


PROFILE = {
    "id": "aws-dev",
    "name": "AWS development",
    "aws_profile": "sandbox",
    "region": "us-east-1",
    "cluster": "dev",
    "task_definition": "session-dev:1",
    "subnets": ["subnet-12345678"],
    "security_groups": ["sg-12345678"],
    "assign_public_ip": False,
    "workdir": "/workspace",
}


class FakeECS:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.entry_point: list | None = None
        self.tasks: dict[str, dict] = {}
        self.seq = 0

    def __call__(self, args, profile="", region="", *, action, timeout=60):
        self.calls.append(list(args))
        assert (profile, region) == ("sandbox", "us-east-1")
        op = args[1]
        if op == "describe-task-definition":
            container = {"name": "workspace", "essential": True}
            if self.entry_point:
                container["entryPoint"] = self.entry_point
            return {"taskDefinition": {"containerDefinitions": [container]}}
        if op == "run-task":
            self.seq += 1
            task_id = f"task{self.seq:04d}abcdef"
            arn = f"arn:aws:ecs:us-east-1:123:task/dev/{task_id}"
            tag = args[args.index("--tags") + 1].split("value=")[1]
            self.tasks[arn] = {
                "taskArn": arn,
                "lastStatus": "RUNNING",
                "containers": [
                    {
                        "runtimeId": f"{task_id}-1",
                        "managedAgents": [{"name": "ExecuteCommandAgent", "lastStatus": "RUNNING"}],
                    }
                ],
                "tags": [{"key": tasks.SESSION_TAG, "value": tag}],
            }
            return {"tasks": [{"taskArn": arn}]}
        if op == "describe-tasks":
            arn = args[args.index("--tasks") + 1]
            return {"tasks": [self.tasks[arn]] if arn in self.tasks else []}
        if op == "stop-task":
            arn = args[args.index("--task") + 1]
            if arn in self.tasks:
                self.tasks[arn]["lastStatus"] = "STOPPED"
            return {"task": {"taskArn": arn}}
        raise AssertionError(f"unexpected aws call {args}")

    def ops(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def ecs(monkeypatch):
    from kiro_crew.cloud import aws

    fake = FakeECS()
    monkeypatch.setattr(aws, "checked_json", fake)
    return fake


@sync
async def test_run_task_replaces_only_the_command_and_carries_no_secret(ecs):
    container = await tasks.task_container(PROFILE)
    task = await tasks.run_task(
        PROFILE,
        "dashboard:chat-9",
        container=container,
        command=bootstrap.container_command(),
        environment={"KIROCREW_WORKDIR": "/workspace"},
    )
    args = ecs.calls[-1]
    assert "--enable-execute-command" in args and args[args.index("--launch-type") + 1] == "FARGATE"
    overrides = json.loads(args[args.index("--overrides") + 1])
    entry = overrides["containerOverrides"][0]
    assert entry["name"] == "workspace" and entry["command"][:2] == ["python3", "-c"]
    network = json.loads(args[args.index("--network-configuration") + 1])["awsvpcConfiguration"]
    assert network == {
        "subnets": ["subnet-12345678"],
        "securityGroups": ["sg-12345678"],
        "assignPublicIp": "DISABLED",
    }
    # The session key never goes to AWS, only its hash.
    assert "chat-9" not in " ".join(args)
    assert len(json.dumps(overrides)) < 8192  # RunTask's override cap
    running = await tasks.wait_running(PROFILE, task, sleep=lambda s: asyncio.sleep(0))
    assert running.ssm_target == f"ecs:dev_{task.task_id}_{task.task_id}-1"


@sync
async def test_an_image_with_an_entry_point_is_refused_before_anything_runs(ecs):
    ecs.entry_point = ["/docker-entrypoint.sh"]
    with pytest.raises(SessionEnvironmentUnavailable, match="entryPoint"):
        await tasks.task_container(PROFILE)
    assert "run-task" not in ecs.ops()


@sync
async def test_a_task_that_stops_while_starting_says_why(ecs):
    task = await tasks.run_task(
        PROFILE, "k", container="workspace", command=["true"], environment={}
    )
    ecs.tasks[task.arn].update(lastStatus="STOPPED", stoppedReason="CannotPullContainerError")
    with pytest.raises(SessionEnvironmentUnavailable, match="CannotPullContainerError"):
        await tasks.wait_running(PROFILE, task, sleep=lambda s: asyncio.sleep(0))


# ── The provider: launch, reuse, refuse, archive ─────────────────────────


@pytest.fixture
def provider(tmp_path, ecs, monkeypatch):
    """FargateSessionEnvironments whose 'SSM forward' reaches a local bootstrap."""
    state: dict = {"processes": [], "connect": None}

    class LocalForward:
        def __init__(self, target, remote_port, **_kw):
            self.target = target
            self.remote_port = remote_port

        async def connect(self):
            return await state["connect"]()

        async def stop(self):
            pass

    async def fresh_bootstrap():
        process, connect, _home = await _start_bootstrap(tmp_path / f"b{len(state['processes'])}")
        state["processes"].append(process)
        state["connect"] = connect

    monkeypatch.setattr(fargate_launch, "PortForward", LocalForward)
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {})
    monkeypatch.setattr(fargate_launch, "_ENROLL_GAP_SECS", 0.05)
    original_run = tasks.run_task

    async def run_task(*args, **kwargs):
        task = await original_run(*args, **kwargs)
        await fresh_bootstrap()  # a new task boots a new bootstrap
        return task

    monkeypatch.setattr(tasks, "run_task", run_task)
    (tmp_path / "home").mkdir(exist_ok=True)
    envs = fargate_launch.FargateSessionEnvironments(
        SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path / "home"
    )
    yield envs, state
    for process in state["processes"]:
        if process.returncode is None:
            process.kill()


@pytest.mark.asyncio
async def test_a_session_gets_a_task_and_keeps_it_while_it_lives(provider, ecs, tmp_path):
    envs, state = provider
    try:
        first = await envs.prepare("dashboard:chat-1", "aws-dev")
        assert ecs.ops().count("run-task") == 1
        row = envs.registry.get("dashboard:chat-1")
        assert row and row.runtime_id
        secret_file = tmp_path / "home" / "fargate" / "secrets" / f"{row.task_id}.key"
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
        assert first.workdir == "/workspace" and first.profile_id == "aws-dev"
        # The launch really reaches the task's bootstrap.
        assert await first.launch.shell.run("echo ok") == b"ok\n"
        again = await envs.prepare("dashboard:chat-1", "aws-dev")
        assert ecs.ops().count("run-task") == 1  # reused, not relaunched
        assert await again.launch.shell.run("echo still") == b"still\n"
    finally:
        for p in state["processes"]:
            p.kill()


@pytest.mark.asyncio
async def test_a_task_no_longer_provably_ours_is_replaced_not_adopted(provider, ecs):
    envs, state = provider
    try:
        await envs.prepare("dashboard:chat-2", "aws-dev")
        old = envs.registry.get("dashboard:chat-2")
        ecs.tasks[old.arn]["tags"] = [{"key": tasks.SESSION_TAG, "value": "someone-else"}]
        await envs.prepare("dashboard:chat-2", "aws-dev")
        new = envs.registry.get("dashboard:chat-2")
        assert new.arn != old.arn
        assert ecs.tasks[old.arn]["lastStatus"] == "STOPPED"
    finally:
        for p in state["processes"]:
            p.kill()


@pytest.mark.asyncio
async def test_a_task_someone_else_enrolled_first_is_stopped(provider, ecs, monkeypatch):
    envs, state = provider
    original = bootstrap.enroll

    async def raced(connect, secret):
        await original(connect, b"z" * 32)  # another principal got there first
        await original(connect, secret)

    monkeypatch.setattr(bootstrap, "enroll", raced)
    try:
        with pytest.raises(SessionEnvironmentUnavailable, match="someone else"):
            await envs.prepare("dashboard:chat-3", "aws-dev")
        assert envs.registry.get("dashboard:chat-3") is None
        assert all(t["lastStatus"] == "STOPPED" for t in ecs.tasks.values())
    finally:
        for p in state["processes"]:
            p.kill()


@pytest.mark.asyncio
async def test_archiving_stops_the_task_at_once(provider, ecs, tmp_path):
    envs, state = provider
    try:
        await envs.prepare("dashboard:chat-4", "aws-dev")
        row = envs.registry.get("dashboard:chat-4")
        assert await envs.archive_session("dashboard:chat-4") is True
        assert ecs.tasks[row.arn]["lastStatus"] == "STOPPED"
        assert envs.registry.get("dashboard:chat-4") is None
        assert not (tmp_path / "home" / "fargate" / "secrets" / f"{row.task_id}.key").exists()
        assert await envs.archive_session("dashboard:local") is False
    finally:
        for p in state["processes"]:
            p.kill()


# ── Wiring: listing, config, dispatch ────────────────────────────────────


def test_profile_ids_may_not_name_both_a_coder_and_a_fargate_profile():
    from kiro_crew.config.session_environments import (
        SessionEnvironmentsError,
        validate_session_environments,
    )

    coder = {"id": "aws-dev", "name": "c", "template": "t", "workdir": "/w"}
    with pytest.raises(SessionEnvironmentsError, match="both a Coder and a Fargate"):
        validate_session_environments(
            {
                "coder_url": "https://c.example.com",
                "profiles": [coder],
                "fargate_profiles": [PROFILE],
            }
        )


def test_fargate_profiles_are_offered_without_a_coder_url(monkeypatch, tmp_path):
    from kiro_crew.config import loader, paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(coder_url="", profiles=[], fargate_profiles=[PROFILE])
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(session_environments=section)),
    )
    assert handlers._profiles() == [
        {"id": "aws-dev", "name": "AWS development", "provider": "fargate", "workdir": "/workspace"}
    ]


@sync
async def test_a_fargate_choice_is_prepared_by_the_fargate_provider(monkeypatch, tmp_path):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(coder_url="", profiles=[], fargate_profiles=[PROFILE])
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:chat-5", "aws-dev"
    )
    seen = []

    async def prepare(self, key, profile_id):
        seen.append((key, profile_id))
        return "prepared"

    monkeypatch.setattr(fargate_launch.FargateSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-5") == "prepared"
    assert seen == [("dashboard:chat-5", "aws-dev")]


@pytest.mark.asyncio
async def test_the_files_panel_reads_a_fargate_task_through_its_bootstrap(
    provider, ecs, monkeypatch, tmp_path
):
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    envs, state = provider
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "data_home", lambda: home)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
        "dashboard:chat-6", "aws-dev"
    )
    try:
        prepared = await envs.prepare("dashboard:chat-6", "aws-dev")
        # The task's workdir, as the profile names it, holds a file to list.
        workdir = tmp_path / "b0" / "guest" / "work"  # the first task's bootstrap home
        (workdir / "notes.txt").write_text("hi\n")
        monkeypatch.setitem(PROFILE, "workdir", str(workdir))
        await prepared.launch.install()
        listing = await session_browse.browse_session("dashboard:chat-6", "list", ".")
        assert "notes.txt" in json.dumps(listing)
        health = await session_browse.session_health("dashboard:chat-6")
        assert health["state"] == "running" and health["workspace"].startswith("task ")
        with pytest.raises(SessionEnvironmentUnavailable, match="terminal"):
            await session_browse.remote_terminal_argv("dashboard:chat-6")
    finally:
        for p in state["processes"]:
            p.kill()


@pytest.mark.asyncio
async def test_the_readiness_check_reads_each_fargate_task_definition(ecs, monkeypatch):
    from kiro_crew.cloud import ssm
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: True)
    section = SimpleNamespace(fargate_profiles=[PROFILE, {**PROFILE, "id": "with-entry"}])
    original = ecs.__call__

    def per_profile(args, profile="", region="", *, action, timeout=60):
        ecs.entry_point = ["/init"] if "with-entry" in json.dumps(args) else None
        return original(args, profile, region, action=action, timeout=timeout)

    monkeypatch.setattr(
        "kiro_crew.cloud.aws.checked_json",
        lambda *a, **k: per_profile(*a, **k),
    )
    # Distinguish the two profiles by their task definition name.
    section.fargate_profiles[1]["task_definition"] = "with-entry:1"
    result = await handlers._check_fargate(section)
    assert result[0] == {"id": "aws-dev", "ok": True}
    assert result[1]["ok"] is False and "entryPoint" in result[1]["error"]
    assert "run-task" not in ecs.ops()  # a check never starts a task
    monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
    result = await handlers._check_fargate(SimpleNamespace(fargate_profiles=[PROFILE]))
    assert result == [
        {
            "id": "aws-dev",
            "ok": False,
            "error": "the AWS Session Manager plugin is not installed on the gateway",
        }
    ]


def test_the_bootstrap_exits_once_no_gateway_has_used_it(monkeypatch):
    """A gateway that died for good cannot stop the task; the bootstrap stops itself."""
    from kiro_crew.remote_session import fargate_bootstrap as boot

    now = [1000.0]
    monkeypatch.setitem(boot._activity, "last", 1000.0)
    monkeypatch.setitem(boot._activity, "pipes", 0)
    monkeypatch.setattr(boot, "_running", set())

    # An open worker pipe (a live gateway) and a running command both count as use.
    boot._activity["pipes"] = 1
    assert boot._idle_for(5000.0) == 0.0
    boot._activity["pipes"] = 0
    boot._running.add(4242)
    assert boot._idle_for(5000.0) == 0.0
    boot._running.clear()
    assert boot._idle_for(1600.0) == 600.0

    exited: list[float] = []

    def sleep(_secs):
        now[0] += 30.0

    boot._idle_watch(
        900.0, exit_fn=lambda: exited.append(now[0]), sleep=sleep, clock=lambda: now[0]
    )
    assert exited and exited[0] - 1000.0 > 900.0


def test_authenticated_use_resets_the_bootstrap_idle_clock(monkeypatch):
    from kiro_crew.remote_session import fargate_bootstrap as boot

    monkeypatch.setitem(boot._activity, "last", 0.0)
    boot._touch()
    assert boot._idle_for(boot._activity["last"] + 1.0) == 1.0
