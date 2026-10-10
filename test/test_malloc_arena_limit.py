"""The gateway caps glibc malloc arenas in-process, honouring operator overrides."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cli, platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_M_ARENA_MAX = -8


class _Recorder:
    def __init__(self, result: int = 1) -> None:
        self.calls: list[tuple[int, int]] = []
        self.result = result

    def __call__(self, param: int, value: int) -> int:
        self.calls.append((param, value))
        return self.result


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)


def test_default_cap_is_applied(linux: None) -> None:
    rec = _Recorder()
    assert (
        platform_compat.limit_malloc_arenas(environ={}, mallopt=rec)
        == platform_compat.GATEWAY_MALLOC_ARENA_MAX
    )
    assert rec.calls == [(_M_ARENA_MAX, platform_compat.GATEWAY_MALLOC_ARENA_MAX)]


@pytest.mark.parametrize(
    "env",
    [
        {"MALLOC_ARENA_MAX": "8"},
        {"GLIBC_TUNABLES": "glibc.malloc.arena_max=6"},
        {"KIROCREW_MALLOC_ARENA_MAX": "0"},
        {"KIROCREW_MALLOC_ARENA_MAX": "-1"},
    ],
)
def test_operator_setting_wins_or_opts_out(linux: None, env: dict[str, str]) -> None:
    rec = _Recorder()
    assert platform_compat.limit_malloc_arenas(environ=env, mallopt=rec) == 0
    assert rec.calls == []


def test_override_value_replaces_default(linux: None) -> None:
    rec = _Recorder()
    env = {"KIROCREW_MALLOC_ARENA_MAX": "6"}
    assert platform_compat.limit_malloc_arenas(environ=env, mallopt=rec) == 6
    assert rec.calls == [(_M_ARENA_MAX, 6)]


def test_garbage_override_falls_back_to_default(linux: None) -> None:
    rec = _Recorder()
    env = {"KIROCREW_MALLOC_ARENA_MAX": "lots"}
    cap = platform_compat.limit_malloc_arenas(environ=env, mallopt=rec)
    assert cap == platform_compat.GATEWAY_MALLOC_ARENA_MAX


def test_rejected_or_raising_mallopt_is_harmless(linux: None) -> None:
    assert platform_compat.limit_malloc_arenas(environ={}, mallopt=_Recorder(0)) == 0

    def boom(_p: int, _v: int) -> int:
        raise OSError("no libc")

    assert platform_compat.limit_malloc_arenas(environ={}, mallopt=boom) == 0


def test_no_glibc_is_a_noop(linux: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "_glibc_mallopt", lambda: None)
    assert platform_compat.limit_malloc_arenas(environ={}) == 0


def test_non_linux_never_touches_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    rec = _Recorder()
    assert platform_compat.limit_malloc_arenas(environ={}, mallopt=rec) == 0
    assert rec.calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="glibc arenas are Linux-only")
def test_real_glibc_caps_arena_count(tmp_path: Path) -> None:
    """End to end in a child: 16 allocating threads stay within the cap."""
    if platform_compat._glibc_mallopt() is None:
        pytest.skip("not glibc")
    code = textwrap.dedent("""
        import os, threading
        from kiro_crew import platform_compat
        os.environ.pop("MALLOC_ARENA_MAX", None)
        cap = platform_compat.limit_malloc_arenas(environ={})
        assert cap == platform_compat.GATEWAY_MALLOC_ARENA_MAX
        print(cap)
        go = threading.Barrier(16)
        keep = []
        def work():
            go.wait()
            keep.append([bytearray(4096) for _ in range(2000)])
        ts = [threading.Thread(target=work) for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        heap = 64 * 1024 * 1024
        n = 0
        for line in open("/proc/self/maps"):
            parts = line.split()
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            if len(parts) == 5 and lo % heap == 0 and hi - lo <= heap:
                n += 1
        print(n)
        """)
    env = {k: v for k, v in __import__("os").environ.items() if k != "MALLOC_ARENA_MAX"}
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
        env=env,
        **UTF8_TEXT,
    )
    assert res.returncode == 0, res.stderr
    # Main arena lives in brk; at most cap-1 non-main arenas may exist, each
    # starting on a fresh 64 MiB-aligned heap. Uncapped this is 16.
    cap, heaps = (int(x) for x in res.stdout.split()[-2:])
    assert heaps <= cap < 16


def test_gateway_command_caps_arenas_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Stop(Exception):
        pass

    def fake_limit() -> int:
        calls.append("limit")
        return 2

    def fake_reexec(_argv: list[str]) -> None:
        calls.append("reexec")
        raise _Stop

    monkeypatch.setattr(cli.platform_compat, "limit_malloc_arenas", fake_limit)
    monkeypatch.setattr(cli, "maybe_reexec", fake_reexec)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway"])
    with pytest.raises(_Stop):
        cli.main()
    assert calls == ["limit", "reexec"]


def test_non_gateway_command_leaves_allocator_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(cli.platform_compat, "limit_malloc_arenas", lambda: called.append(1))
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert called == []
