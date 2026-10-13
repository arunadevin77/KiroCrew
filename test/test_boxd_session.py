"""boxd session environments, without boxd.

A fake ``boxd`` executable (a Python script) implements the CLI calls the
provider makes. Its ``machine exec`` really runs the command as a local child
with stdin and stdout streamed, which is what the real CLI does, so the guest
worker is installed, started and dialled exactly as on a real machine. Every
call is logged for the assertions.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.boxd_session import launch as boxd_launch
from kiro_crew.boxd_session.cli import BoxdCLI, BoxdError
from kiro_crew.remote_session import wire
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.session_environment import SessionEnvironmentUnavailable

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

ENGINE = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"

FAKE_BOXD = r"""#!{python}
import json, os, subprocess, sys
state_path = os.environ["FAKE_BOXD_STATE"]
home = os.environ["FAKE_BOXD_HOME"]
def load():
    try:
        return json.load(open(state_path))
    except FileNotFoundError:
        return {{"machines": {{}}, "calls": []}}
state = load()
args = sys.argv[1:]
state["calls"].append(args)
def save():
    json.dump(state, open(state_path, "w"))
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
def fail(msg, code=1):
    save(); print("error: " + msg, file=sys.stderr); sys.exit(code)
if os.environ.get("FAKE_BOXD_SIGNED_OUT"):
    fail("not logged in; run boxd auth login")
m = state["machines"]
if args[:2] == ["machine", "list"]:
    out(list(m.values()))
if args[:2] == ["machine", "new"]:
    name = args[2]
    m[name] = {{"name": name, "status": "running", "args": args[3:]}}
    out({{"name": name, "id": "id-" + name}})
if args[:2] == ["machine", "get"]:
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    out(m[args[2]])
if args[:2] == ["machine", "remove"]:
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    del m[args[2]]; out({{"name": args[2], "status": "destroyed"}})
if args[:2] == ["machine", "hibernate"]:
    m[args[2]]["status"] = "hibernated"; out({{}})
if args[:3] == ["machine", "config", "set"]:
    m[args[3]][args[4]] = args[5]; out({{}})
if args[:2] == ["machine", "exec"]:
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    m[args[2]]["status"] = "running"  # an exec wakes a machine
    save()
    cmd = args[args.index("--") + 1:]
    env = {{"HOME": home, "PATH": os.environ["PATH"]}}
    sys.exit(subprocess.call(cmd, cwd=home, env=env))
fail("unknown command %r" % args, 2)
"""


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = tmp_path / "guest"
    home.mkdir()
    binary = tmp_path / "bin" / "boxd"
    binary.parent.mkdir()
    binary.write_text(FAKE_BOXD.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "boxd-state.json"
    monkeypatch.setenv("FAKE_BOXD_STATE", str(state))
    monkeypatch.setenv("FAKE_BOXD_HOME", str(home))
    # HOME and PATH as a pair for the guest: python3 is this interpreter.
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin")

    def read():
        return json.loads(state.read_text()) if state.exists() else {"machines": {}, "calls": []}

    return SimpleNamespace(bin=str(binary), home=home, read=read)


PROFILE = {"id": "bx-dev", "name": "boxd dev", "snapshot": "", "vcpu": 1, "workdir": ""}


def _envs(tmp_path, fake, *, days=7, **profile):
    prof = {**PROFILE, "workdir": str(fake.home / "work"), **profile}
    section = SimpleNamespace(boxd_profiles=[prof], archived_retention_days=days)
    (tmp_path / "data").mkdir(exist_ok=True)
    return boxd_launch.BoxdSessionEnvironments(section, tmp_path / "data", cli=BoxdCLI(fake.bin))


@sync
async def test_a_session_gets_a_machine_and_its_worker_is_dialled_through_exec(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    prepared = await envs.prepare("dashboard:chat-1", "bx-dev")
    row = envs.registry.get("dashboard:chat-1")
    assert row is not None and row.machine.startswith("kc-") and len(row.machine) == 19
    assert prepared.workspace == row.machine and prepared.profile_id == "bx-dev"
    assert (fake.home / "work").is_dir()
    created = fake.read()["machines"][row.machine]
    assert created["args"] == ["--json", "--vcpu", "1"]
    launch = prepared.launch
    await launch.install()
    assert (fake.home / ".kirocrew-worker" / "worker.py").exists()
    worker_id = launch.new_worker_id()
    secret = wire.new_secret()
    port = await launch.start_worker(worker_id, [sys.executable, "-u", "-c", ENGINE], secret)
    link = RemoteLink(
        worker_id, secret, launch.dial(port), keepalive_seconds=1, stall_seconds=5, lease_seconds=20
    )
    try:
        link.start()
        await link.wait_connected(20)
        for size in (10, 1017, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            await link.write(line)
            assert await asyncio.wait_for(link.stdout.readline(), 30) == line
    finally:
        await link.aclose()
        with contextlib.suppress(Exception):
            await launch.stop_worker(worker_id)
    # The session secret never appears in any boxd argv.
    assert not any(secret.hex() in " ".join(call) for call in fake.read()["calls"])
    again = await envs.prepare("dashboard:chat-1", "bx-dev")
    assert again.workspace == row.machine
    assert sum(1 for c in fake.read()["calls"] if c[:2] == ["machine", "new"]) == 1


@sync
async def test_a_snapshot_profile_starts_from_it_and_keeps_its_size(tmp_path, fake):
    envs = _envs(tmp_path, fake, snapshot="my-workspace", vcpu=0)
    await envs.prepare("dashboard:chat-2", "bx-dev")
    name = envs.registry.get("dashboard:chat-2").machine
    assert fake.read()["machines"][name]["args"] == ["--json", "--from-snapshot", "my-workspace"]


@sync
async def test_a_machine_deleted_outside_is_replaced_by_a_fresh_one(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    first = await envs.prepare("dashboard:chat-3", "bx-dev")
    old = envs.registry.get("dashboard:chat-3").machine
    # Reattaching its own machine is not a new workspace.
    assert first.created and not (await envs.prepare("dashboard:chat-3", "bx-dev")).created
    await envs.cli().remove(old)
    fresh = await envs.prepare("dashboard:chat-3", "bx-dev")
    new = envs.registry.get("dashboard:chat-3").machine
    assert new != old and new in fake.read()["machines"]
    # A replacement is: the session is told its workspace is empty.
    assert fresh.created


@sync
async def test_a_signed_out_gateway_says_how_to_sign_in(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_BOXD_SIGNED_OUT", "1")
    envs = _envs(tmp_path, fake)
    with pytest.raises(SessionEnvironmentUnavailable, match="boxd auth login"):
        await envs.prepare("dashboard:chat-4", "bx-dev")


@sync
async def test_archiving_hibernates_with_a_backstop_then_the_sweep_deletes(tmp_path, fake):
    envs = _envs(tmp_path, fake, days=3)
    await envs.prepare("dashboard:chat-5", "bx-dev")
    name = envs.registry.get("dashboard:chat-5").machine
    assert await envs.archive_session("dashboard:chat-5", 3) is True
    machine = fake.read()["machines"][name]
    assert machine["status"] == "hibernated" and machine["delete-after"] == "3d"
    assert await envs.sweep_due() == 0
    # Reopening before the deadline clears both the stamp and boxd's own timer.
    await envs.prepare("dashboard:chat-5", "bx-dev")
    assert envs.registry.get("dashboard:chat-5").deletion_due_at == ""
    assert fake.read()["machines"][name]["delete-after"] == "off"
    await envs.archive_session("dashboard:chat-5", 3)
    later = datetime.now(timezone.utc) + timedelta(days=4)
    assert await envs.sweep_due(now=later) == 1
    assert name not in fake.read()["machines"] and envs.registry.get("dashboard:chat-5") is None
    # At 0 days it goes now.
    await envs.prepare("dashboard:chat-6", "bx-dev")
    gone = envs.registry.get("dashboard:chat-6").machine
    assert await envs.archive_session("dashboard:chat-6", 0) is True
    assert gone not in fake.read()["machines"]
    assert await envs.archive_session("dashboard:local", 3) is False


@sync
async def test_files_health_and_terminal_reach_the_machine_without_waking_it(
    tmp_path, fake, monkeypatch
):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    envs = _envs(tmp_path, fake)
    home = tmp_path / "data"
    monkeypatch.setattr(paths, "data_home", lambda: home)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
        "dashboard:chat-7", "bx-dev"
    )
    with pytest.raises(session_browse.WorkspaceNotRunning):
        await session_browse.browse_session("dashboard:chat-7", "list", ".")
    assert not any(c[:2] == ["machine", "new"] for c in fake.read()["calls"])
    prepared = await envs.prepare("dashboard:chat-7", "bx-dev")
    (fake.home / "work" / "notes.txt").write_text("hi\n")
    await prepared.launch.install()
    listing = await session_browse.browse_session("dashboard:chat-7", "list", ".")
    assert "notes.txt" in json.dumps(listing)
    assert (await session_browse.session_health("dashboard:chat-7"))["state"] == "running"
    # A hibernated machine reads as stopped, with no exec that would wake it.
    await envs.cli().hibernate(prepared.workspace)
    execs = sum(c[:2] == ["machine", "exec"] for c in fake.read()["calls"])
    assert (await session_browse.session_health("dashboard:chat-7"))["state"] == "stopped"
    assert sum(c[:2] == ["machine", "exec"] for c in fake.read()["calls"]) == execs
    argv = await session_browse.remote_terminal_argv("dashboard:chat-7")
    assert argv == [fake.bin, "machine", "connect", prepared.workspace]


@sync
async def test_a_boxd_choice_is_prepared_by_the_boxd_provider(monkeypatch, tmp_path):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(
        coder_url="", profiles=[], fargate_profiles=[], sprites_profiles=[], boxd_profiles=[PROFILE]
    )
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:chat-8", "bx-dev"
    )
    seen = []

    async def prepare(self, key, profile_id):
        seen.append((key, profile_id))
        return "prepared"

    monkeypatch.setattr(boxd_launch.BoxdSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-8") == "prepared"
    assert seen == [("dashboard:chat-8", "bx-dev")]


def test_names_are_validated_before_any_argv_is_built():
    from kiro_crew.boxd_session.cli import check_machine_name
    from kiro_crew.boxd_session.shell import BoxdShell

    for bad in ("--json", "kc-XYZ", "other", "kc-0123456789abcdef; rm -rf /"):
        with pytest.raises(BoxdError):
            check_machine_name(bad)
    with pytest.raises(BoxdError):
        BoxdShell("/bin/boxd", "-y")
    assert BoxdShell("/b", "kc-0123456789abcdef").argv("echo hi") == [
        "/b",
        "machine",
        "exec",
        "kc-0123456789abcdef",
        "--",
        "sh",
        "-c",
        "echo hi",
    ]


def test_boxd_profiles_validate_and_share_one_id_space():
    from kiro_crew.config import session_environments as se

    good = {**PROFILE, "workdir": "/home/boxd/work"}
    assert se.validate_boxd_profiles([good]) == [good]
    with pytest.raises(se.SessionEnvironmentsError, match="vcpu"):
        se.validate_boxd_profiles([{**good, "vcpu": 3}])
    with pytest.raises(se.SessionEnvironmentsError, match="own size"):
        se.validate_boxd_profiles([{**good, "snapshot": "snap", "vcpu": 2}])
    with pytest.raises(se.SessionEnvironmentsError, match="snapshot"):
        se.validate_boxd_profiles([{**good, "snapshot": "--from-snapshot=x"}])
    with pytest.raises(se.SessionEnvironmentsError, match=r"^boxd_profiles\[0\]\.name"):
        se.validate_boxd_profiles([{**good, "name": ""}])
    with pytest.raises(se.SessionEnvironmentsError, match="both a Coder and a boxd"):
        se.validate_session_environments(
            {
                "coder_url": "https://c.example.com",
                "profiles": [{"id": "bx-dev", "name": "c", "template": "t", "workdir": "/w"}],
                "boxd_profiles": [good],
            }
        )
    assert se.load_boxd_profiles({"boxd_profiles": [good, {"id": "x"}]}) == [good]


@sync
async def test_the_readiness_check_lists_once_and_touches_no_machine(tmp_path, fake, monkeypatch):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    section = SimpleNamespace(boxd_profiles=[PROFILE, {**PROFILE, "id": "bx-two"}])
    assert await handlers._check_boxd(section) == [
        {"id": "bx-dev", "ok": True},
        {"id": "bx-two", "ok": True},
    ]
    assert fake.read()["calls"] == [["machine", "list", "--json"]]
    monkeypatch.setenv("FAKE_BOXD_SIGNED_OUT", "1")
    rows = await handlers._check_boxd(section)
    assert rows[0]["ok"] is False and "boxd auth login" in rows[0]["error"]
    assert await handlers._check_boxd(SimpleNamespace(boxd_profiles=[])) == []
